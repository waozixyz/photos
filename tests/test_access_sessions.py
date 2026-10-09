import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from photos import access_sessions as access
from photos.library import LibraryError


class AccessSessionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.library = SimpleNamespace(root=Path(temporary.name)/'library')
        self.other = SimpleNamespace(root=Path(temporary.name)/'other')
        self.registry = access.Registry()
        self.addCleanup(self.registry.close)
        self.patched = patch.object(access,'registry',self.registry)
        self.patched.start(); self.addCleanup(self.patched.stop)
        self.stamp = '42'*32

    def test_independent_scope_root_and_credential_stamp_with_scoped_revocation(self):
        hidden = access.grant(self.library,access.HIDDEN)
        app = access.grant(self.library,access.APP,self.stamp)
        other = access.grant(self.other,access.HIDDEN)
        self.assertGreater(access.deadline(self.library,access.HIDDEN,hidden),time.monotonic())
        self.assertFalse(access.deadline(self.other,access.HIDDEN,hidden))
        self.assertFalse(access.deadline(self.library,access.APP,hidden,self.stamp))
        self.assertGreater(access.deadline(self.library,access.HIDDEN,hidden),0)
        access.lock(self.other,access.HIDDEN,hidden)
        self.assertGreater(access.deadline(self.library,access.HIDDEN,hidden),0)
        access.revoke(self.library,access.HIDDEN)
        self.assertFalse(access.deadline(self.library,access.HIDDEN,hidden))
        self.assertGreater(access.deadline(self.other,access.HIDDEN,other),0)
        self.assertGreater(access.deadline(self.library,access.APP,app,self.stamp),0)
        self.assertFalse(access.deadline(self.library,access.APP,app,'43'*32))
        self.assertFalse(access.deadline(self.library,access.APP,app,self.stamp))
        self.assertFalse(self.library.root.exists(), 'Sessions must never create library files')

    def test_actual_monotonic_expiry_and_renewal_cannot_resurrect_an_expired_token(self):
        short = access.Registry(session_ms=120); self.addCleanup(short.close)
        with patch.object(access,'registry',short):
            token = access.grant(self.library,access.APP,self.stamp)
            first = access.deadline(self.library,access.APP,token,self.stamp)
            time.sleep(.06)
            second = access.deadline(self.library,access.APP,token,self.stamp,renew=True)
            self.assertGreater(second,first)
            time.sleep(.08)
            self.assertGreater(access.deadline(self.library,access.APP,token,self.stamp),0)
            time.sleep(.07)
            self.assertFalse(access.deadline(self.library,access.APP,token,self.stamp,renew=True))
            self.assertFalse(access.deadline(self.library,access.APP,token,self.stamp))

    def test_native_failed_attempt_window_is_scoped_bounded_and_expires(self):
        short = access.Registry(attempt_ms=120); self.addCleanup(short.close)
        with patch.object(access,'registry',short):
            access.check_attempts(self.library,access.HIDDEN)
            for _ in range(5): access.failed_attempt(self.library,access.HIDDEN)
            with self.assertRaisesRegex(LibraryError,'Too many attempts'):
                access.check_attempts(self.library,access.HIDDEN)
            access.check_attempts(self.library,access.APP)
            access.check_attempts(self.other,access.HIDDEN)
            time.sleep(.15)
            access.check_attempts(self.library,access.HIDDEN)
            for _ in range(5): access.failed_attempt(self.library,access.HIDDEN)
            access.clear_attempts(self.library,access.HIDDEN)
            access.check_attempts(self.library,access.HIDDEN)

    def test_process_restart_and_missing_helper_never_accept_old_tokens_or_fall_back(self):
        token = access.grant(self.library,access.HIDDEN)
        self.registry.child.kill(); self.registry.child.wait(timeout=3)
        self.assertFalse(access.deadline(self.library,access.HIDDEN,token))
        newer = access.grant(self.library,access.HIDDEN)
        self.assertNotEqual(token,newer)
        self.registry.close()
        with patch.object(access,'program',side_effect=LibraryError('Unavailable native sessions')):
            self.assertFalse(access.deadline(self.library,access.HIDDEN,newer))
            with self.assertRaisesRegex(LibraryError,'Unavailable'):
                access.grant(self.library,access.HIDDEN)

    def test_full_registry_preserves_existing_access_and_recovers_after_scoped_lock(self):
        tokens = [access.grant(self.library,access.HIDDEN) for _ in range(256)]
        self.assertEqual(len(set(tokens)),256)
        with self.assertRaisesRegex(LibraryError,'capacity'):
            access.grant(self.library,access.HIDDEN)
        self.assertGreater(access.deadline(self.library,access.HIDDEN,tokens[0]),0)
        access.lock(self.library,access.HIDDEN,tokens[-1])
        new = access.grant(self.library,access.HIDDEN)
        self.assertNotIn(new,tokens)
        self.assertGreater(access.deadline(self.library,access.HIDDEN,tokens[0]),0)

    def test_parallel_requests_have_distinct_tokens_and_exact_responses(self):
        with ThreadPoolExecutor(max_workers=12) as jobs:
            tokens = list(jobs.map(lambda _:access.grant(self.library,access.HIDDEN),range(72)))
            self.assertEqual(len(set(tokens)),72)
            self.assertTrue(all(jobs.map(lambda t:access.deadline(self.library,access.HIDDEN,t)>0,tokens)))
        self.assertFalse(access.deadline(self.library,access.HIDDEN,'x'*64))
        self.assertFalse(access.deadline(self.library,access.HIDDEN,None))
        self.assertGreater(access.deadline(self.library,access.HIDDEN,tokens[0]),0)

    def test_malformed_binary_modes_flags_scopes_and_partial_frames_emit_no_token(self):
        base = bytes([1,1,1,0])+b'r'*32+b'\0'*64
        frames = [base[:50], bytes([2])+base[1:], base[:1]+bytes([0])+base[2:],
                  base[:2]+bytes([0])+base[3:], base[:3]+bytes([1])+base[4:],
                  base[:4]+b'\0'*32+base[36:], base[:36]+b't'*32+base[68:],
                  base[:68]+b's'*32, base[:2]+bytes([2])+base[3:]]
        environment = {k:v for k,v in os.environ.items() if k not in
            {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
        for frame in frames:
            result = subprocess.run([str(access.program())],input=frame,capture_output=True,env=environment,timeout=3)
            self.assertFalse(result.stdout); self.assertFalse(result.stderr)
        for duration in ['0','600001','-1','x']:
            result = subprocess.run([str(access.program()),duration],input=base,capture_output=True,env=environment,timeout=3)
            self.assertNotEqual(result.returncode,0); self.assertFalse(result.stdout)

    def test_tokens_and_attempts_are_owned_by_native_helper_with_private_process_limits(self):
        access.grant(self.library,access.HIDDEN)
        self.assertFalse(hasattr(access,'sessions'))
        limits = Path('/proc',str(self.registry.child.pid),'limits').read_text()
        core = next(row for row in limits.splitlines() if row.startswith('Max core file size'))
        self.assertEqual(core.split()[4:6],['0','0'])
        command = Path('/proc',str(self.registry.child.pid),'cmdline').read_bytes()
        self.assertNotIn(str(self.library.root).encode(),command)
        self.assertNotIn(self.stamp.encode(),command)

    def test_private_pipe_eof_ends_helper_without_retaining_or_printing_sessions(self):
        base = bytes([1,1,1,0])+b'r'*32+b'\0'*64
        child = subprocess.Popen([str(access.program())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            child.stdin.write(base); child.stdin.flush()
            response = child.stdout.read(41)
            self.assertEqual(len(response),41); self.assertEqual(response[0],1)
            child.stdin.close(); child.wait(timeout=3)
            self.assertEqual(child.returncode,0)
            self.assertFalse(child.stdout.read()); self.assertFalse(child.stderr.read())
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=3)
            child.stdout.close(); child.stderr.close()


if __name__ == '__main__':
    unittest.main()
