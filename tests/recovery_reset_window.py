"""Actual locked-window credential recovery on synthetic private-display data."""
import ast
import ctypes as c
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('PHOTOS_SERVICE_PACKAGE', str(ROOT)))
from photos import app_lock, recovery, vault
from photos.library import Library, digest_file, LibraryError
from photos.sync import Client
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_RECOVERY_PORTABLE') == '1'
hosted = os.environ.get('PHOTOS_RECOVERY_HOSTED') == '1'
assert not hosted or portable
suffix = 'hosted' if hosted else ('portable' if portable else 'native')
output = ROOT/'build/screenshots'; output.mkdir(exist_ok=True)


def wait(check, reason):
    end = time.monotonic()+20
    while time.monotonic() < end:
        value = check()
        if value: return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as temporary:
    folder = Path(temporary); source = folder/'source/Family'; source.mkdir(parents=True)
    originals = {}
    for i in range(3):
        path = source/f'Photo {i}.png'; Image.new('RGB', (300, 200), (60+i*60, 150, 100)).save(path)
        originals[path] = path.read_bytes()
    library = Library(folder/'library'); library.import_folders([source.parent])
    config = dict(root=str(library.root), node='http://127.0.0.1:1', node_id='42'*32)
    client = Client(library, config)
    keys = client.identity_path.read_bytes(); device = client.device_path.read_bytes()
    backup = folder/'fixture.photos-recovery'; proof = 'synthetic recovery file password'
    recovery.save(library, config, {}, str(backup), proof)
    old_hidden = 'synthetic old Hidden password'
    vault.authenticate(library, old_hidden, setup=True)
    hidden_settings = (library.root/'private/hidden-password.json').read_bytes()
    library.edit(digest_file(next(iter(originals))), 'hidden_set', '1')
    records = [tuple(row) for row in library.db.execute('SELECT * FROM photos ORDER BY id')]
    app_lock.route(library, dict(action='app_lock_pin', value='0482'))
    new_app = 'synthetic new app password'; new_hidden = 'synthetic new Hidden password'
    calls = []; original_process = service_module.process

    def record(request, config):
        result = original_process(request, config)
        # Secrets are never retained in harness logs or failure reports.
        calls.append((dict(action=request.get('action')), result))
        return result

    service_module.process = record
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder)}
    if portable:
        flags = [] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *flags], cwd=ROOT, text=True).strip())
        command = [os.environ.get('PHOTOS_PLAYER', str(kryon/'build/bin/zx')),
                   os.environ.get('PHOTOS_BUNDLE', str(ROOT/'build/photos-portable.zx'))]
    else: command = [os.environ.get('PHOTOS_BINARY', str(ROOT/'build/photos-desktop'))]
    connection = None
    if hosted:
        x = c.CDLL('libX11.so.6')
        x.XOpenDisplay.restype = c.c_void_p
        x.XDefaultRootWindow.argtypes = [c.c_void_p]; x.XDefaultRootWindow.restype = c.c_ulong
        x.XCreateSimpleWindow.argtypes = [c.c_void_p,c.c_ulong,c.c_int,c.c_int,c.c_uint,c.c_uint,c.c_uint,c.c_ulong,c.c_ulong]
        x.XCreateSimpleWindow.restype = c.c_ulong
        x.XMapWindow.argtypes = [c.c_void_p,c.c_ulong]
        x.XInternAtom.argtypes = [c.c_void_p,c.c_char_p,c.c_int]; x.XInternAtom.restype = c.c_ulong
        x.XChangeProperty.argtypes = [c.c_void_p,c.c_ulong,c.c_ulong,c.c_ulong,c.c_int,c.c_int,c.c_void_p,c.c_int]
        x.XQueryTree.argtypes = [c.c_void_p,c.c_ulong,c.POINTER(c.c_ulong),c.POINTER(c.c_ulong),c.POINTER(c.POINTER(c.c_ulong)),c.POINTER(c.c_uint)]
        x.XFree.argtypes = [c.c_void_p]; x.XFlush.argtypes = [c.c_void_p]
        x.XDestroyWindow.argtypes = [c.c_void_p,c.c_ulong]; x.XCloseDisplay.argtypes = [c.c_void_p]
        connection = x.XOpenDisplay(None); assert connection
        parent = x.XCreateSimpleWindow(connection,x.XDefaultRootWindow(connection),0,0,1180,880,0,0,0)
        container = x.XCreateSimpleWindow(connection,parent,0,0,1180,880,0,0,0)
        pid = (c.c_ulong*1)(os.getpid())
        x.XChangeProperty(connection,parent,x.XInternAtom(connection,b'_NET_WM_PID',0),6,32,0,pid,1)
        x.XMapWindow(connection,parent); x.XMapWindow(connection,container); x.XFlush(connection)
        loader = Path(os.environ.get('PHOTOS_APP_START', Path.home()/'.local/lib/harmony/libapp-start.so'))
        assert loader.is_file()
        env.update(HARMONY_APP_HOST_PID=str(os.getpid()),HARMONY_APP_CONTAINER=str(container),LD_PRELOAD=str(loader),SDL_VIDEODRIVER='x11')
    with Server(config, folder/'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever, daemon=True).start()
        child = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        window = None

        def xdo(*args): return subprocess.check_output(['xdotool', *map(str,args)], text=True).strip()
        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            result = subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode == 0 else None
        def state():
            raw = subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
            return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}
        def control(label, px, py):
            if not portable:
                def ready():
                    current = state()
                    return next((c for c in current.get('controls',[]) if c['label'] == label and c['control'] != '41' and c['enabled']), None)
                item = wait(ready, 'Missing recovery control: '+label)
                px = item['x']+item['width']/2; py = item['y']+item['height']/2
            xdo('mousemove','--window',window,int(px),int(py)); xdo('mousedown',1); time.sleep(.12)
            xdo('mouseup',1); time.sleep(.35)
        def text(value, py=168):
            xdo('mousemove','--window',window,400,py); xdo('click',1)
            xdo('keydown','ctrl'); time.sleep(.12); xdo('keydown','a'); time.sleep(.12)
            xdo('keyup','a'); xdo('keyup','ctrl'); time.sleep(.12)
            xdo('type','--clearmodifiers','--delay','20','--',value); time.sleep(.2)
        def result(action, since=0): return next((r for q,r in reversed(calls[since:]) if q['action'] == action), None)
        def shoot(name): subprocess.run(['import','-window',window,str(output/(name+'-'+suffix+'.png'))],check=True,timeout=5)
        def proof_and_new(recovery_password, replacement):
            text(recovery_password); control('Continue',114,234)
            text(replacement); control('Continue',114,234)
        def unlock():
            since = len(calls); text(new_app,158); control('Unlock Photos',108,226)
            response = wait(lambda: result('app_lock_unlock',since),'New app password was not checked')
            assert response.get('app_lock_authorized') == 1, response.get('error')
            time.sleep(.5)
        def locked():
            if not portable:
                def rendered():
                    current = state()
                    return current if current.get('app_locked') and current.get('nodes') == 0 else None
                current = wait(rendered, 'Recovery response did not relock the rendered gallery')
                assert not {'Photos','Albums','Hidden','Import','Export'}.intersection(c['label'] for c in current['controls'])
                for secret in [proof,new_app,new_hidden]: assert secret not in json.dumps(current)
        try:
            window = wait(locate,'Recovery test did not open')
            if hosted:
                root_window = c.c_ulong(); parent_window = c.c_ulong(); children = c.POINTER(c.c_ulong)(); count = c.c_uint()
                assert x.XQueryTree(connection,int(window),c.byref(root_window),c.byref(parent_window),c.byref(children),c.byref(count))
                if children: x.XFree(children)
                assert parent_window.value == container, 'Photos opened outside Harmony container'
            xdo('windowsize',window,1180,880); xdo('windowmove',window,0,0); xdo('windowfocus',window)
            wait(lambda: result('query') and result('query')['app_locked'] == 1,'Initial app lock missing'); time.sleep(.5); locked()
            control('Recover app password',132,360)
            text(str(backup)); control('Continue',114,234)
            proof_and_new('incorrect recovery fixture password',new_app)
            text('mismatched new fixture password'); control('Recover password',114,234)
            assert result('recovery_reset_app') is None
            text(new_app); since = len(calls); control('Recover password',114,234)
            failed = wait(lambda: result('recovery_reset_app',since),'Wrong proof was not checked')
            assert failed['recovery_reset'] == 0 and 'Incorrect recovery password' in failed['error']; locked()
            proof_and_new(proof,new_app); text(new_app)
            if not portable: assert new_app not in json.dumps(state())
            shoot('recovery-reset-confirm'); since = len(calls); control('Recover password',114,234)
            recovered = wait(lambda: result('recovery_reset_app',since),'App recovery did not finish')
            assert recovered['recovery_reset'] == 1 and recovered['app_locked'] == 1
            assert not recovered['photos'] and not recovered['albums']; locked()
            assert (library.root/'private/hidden-password.json').read_bytes() == hidden_settings
            with_error = app_lock.route(library,dict(action='app_lock_unlock',value='0482')); assert with_error['app_locked'] == 1
            control('Recover Hidden password',374,360); text(str(backup)); control('Cancel',298,234); locked()
            unlock(); control('Local sync',95,372)
            control('Recover Hidden password',915,597)
            text(str(backup)); control('Continue',114,234)
            proof_and_new(proof,new_hidden); text(new_hidden); since = len(calls); control('Recover password',114,234)
            recovered = wait(lambda: result('recovery_reset_hidden',since),'Hidden recovery did not finish')
            assert recovered['recovery_reset'] == 1 and recovered['app_locked'] == 1; locked()
            unlock(); control('Hidden',95,277); control('Unlock Hidden',300,312)
            text(new_hidden,244); since = len(calls); control('Save',250,339)
            opened = wait(lambda: result('vault_unlock',since),'Recovered Hidden password was not checked')
            assert opened['vault_unlocked'] == 1 and opened['count'] == 1
            assert client.identity_path.read_bytes() == keys and client.device_path.read_bytes() == device
            assert [tuple(row) for row in library.db.execute('SELECT * FROM photos ORDER BY id')] == records
            for path,data in originals.items(): assert path.read_bytes() == data
            shoot('recovery-reset-complete')
            print('Private Xvfb '+suffix+': wrong-proof retry, masked confirmation, PIN-to-password recovery, scoped Hidden reset, relock, cancellation and original/key preservation passed')
        except Exception:
            if window is not None and child.poll() is None: shoot('recovery-reset-failed')
            print('Synthetic recovery responses:',[(q['action'],r.get('error')) for q,r in calls])
            raise
        finally:
            if child.poll() is None: child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            child.stderr.close(); service.shutdown(); library.db.close()
            if connection is not None:
                x.XDestroyWindow(connection,parent); x.XCloseDisplay(connection)
