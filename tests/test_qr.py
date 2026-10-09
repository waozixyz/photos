import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from photos import qr, vault, live
from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from qr_fixtures import qr_image, CONTENTS, FIXTURES


class QRTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root / 'source'; self.source.mkdir()
        self.path = self.source / 'Codes.png'; qr_image(self.path)
        self.library = Library(self.root / 'library'); self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source]); self.id = digest_file(self.path)
        self.config = dict(root=str(self.library.root))

    def request(self, action='detect_qr', **values):
        return process(dict(action=action, id=self.id, **values), self.config)

    def test_native_multiple_unicode_positions_and_no_index_or_original_mutation(self):
        before = self.path.read_bytes(); row = self.library.photo(self.id)
        with patch('photos.qr.subprocess.run', wraps=subprocess.run) as command:
            first = self.request(); second = self.request()
        self.assertEqual(command.call_count, 2)  # No payload cache, including ordinary photos.
        self.assertEqual(first['qr_codes'], second['qr_codes'])
        self.assertCountEqual([code['text'] for code in first['qr_codes']], CONTENTS)
        self.assertEqual((first['qr_width'], first['qr_height']), (1200, 560))
        self.assertEqual(first['qr_unsupported'], 0)
        for code in first['qr_codes']:
            self.assertGreater(code['width'], 0); self.assertLessEqual(code['x']+code['width'], 10000)
            self.assertGreater(code['height'], 0); self.assertLessEqual(code['y']+code['height'], 10000)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.library.original(row).read_bytes(), before)
        self.assertEqual(self.library.photo(self.id), row)
        self.assertFalse((self.library.root/'private/qr').exists())
        with patch('photos.qr.scan', side_effect=AssertionError('Query must not scan QR codes')):
            self.assertNotIn('qr_codes', process(dict(action='query'), self.config))
        check = self.request('qr_check')
        self.assertEqual(check['qr_checked'], 1); self.assertNotIn('qr_codes', check)

    def test_exif_orientation_alpha_blank_large_and_binary_payload(self):
        exif = Image.Exif(); exif[274] = 6
        camera = self.source / 'Camera.jpg'
        with Image.open(self.path) as image:
            image.transpose(Image.Transpose.ROTATE_90).save(camera, exif=exif, quality=100)
        alpha = self.source / 'Alpha.png'
        with Image.open(self.path) as image:
            image = image.convert('RGBA')
            image.putalpha(image.convert('L').point(lambda value: 0 if value == 255 else 255))
            image.save(alpha)
        blank = self.source/'Blank.png'; Image.new('RGB',(5000,100),'white').save(blank)
        binary = self.source/'Binary.png'; binary.write_bytes((FIXTURES/'qr-binary.png').read_bytes())
        self.library.import_folders([self.source])
        for path in [camera, alpha]:
            result = process(dict(action='detect_qr', id=digest_file(path)), self.config)
            self.assertEqual((result['qr_width'],result['qr_height']),(1200,560))
            self.assertCountEqual([code['text'] for code in result['qr_codes']], CONTENTS)
        result = process(dict(action='detect_qr',id=digest_file(blank)),self.config)
        self.assertEqual(result['qr_width'],4096);self.assertEqual(result['qr_codes'],[])
        result = process(dict(action='detect_qr',id=digest_file(binary)),self.config)
        self.assertEqual(result['qr_codes'],[]);self.assertEqual(result['qr_unsupported'],1)

    def test_locked_hidden_trash_video_and_slow_scan_are_denied(self):
        self.library.edit(self.id,'hidden_set','1')
        with patch('photos.qr.scan',side_effect=AssertionError('Locked scanning must not run')):
            self.assertIn('error',self.request(view='hidden'))
        token = vault.authenticate(self.library,'Synthetic QR password',setup=True)
        self.assertEqual(len(self.request(view='hidden',vault_token=token)['qr_codes']),3)
        def expire(library,photo):
            vault.lock(library,token)
            return dict(qr_codes=[dict(text='SECRET')])
        with patch('photos.qr.scan',side_effect=expire):
            with self.assertRaisesRegex(LibraryError,'Hidden is locked'):
                self.request(view='hidden',vault_token=token)
        self.library.edit(self.id,'hidden_set','0');self.library.edit(self.id,'trash')
        with self.assertRaisesRegex(LibraryError,'Restore'):self.request()
        self.library.edit(self.id,'restore')
        self.library.db.execute('UPDATE photos SET kind="video" WHERE id=?',(self.id,));self.library.db.commit()
        with self.assertRaisesRegex(LibraryError,'Choose a photo'):self.request()

    def test_live_component_and_concurrent_privacy_changes_revoke_scanning(self):
        path = self.source / 'Motion.mp4'; path.write_bytes(b'Synthetic video record')
        self.library.import_folders([self.source]);motion_id=digest_file(path)
        self.assertTrue(live.pair(self.library,self.id,motion_id))
        self.assertEqual(len(self.request()['qr_codes']),3)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(motion_id,));self.library.db.commit()
        with self.assertRaisesRegex(LibraryError,'Hidden is locked'):self.request('qr_check')
        self.library.db.execute('UPDATE photos SET hidden=0 WHERE id=?',(motion_id,));self.library.db.commit()
        def trash(library,photo):
            self.library.edit(self.id,'trash')
            return dict(qr_codes=[dict(text='MUST NOT RETURN')])
        with patch('photos.qr.scan',side_effect=trash):
            with self.assertRaisesRegex(LibraryError,'Restore'):self.request()

    def test_materialization_rechecks_access_and_uses_only_existing_pinned_client(self):
        self.library.original(self.library.photo(self.id)).unlink()
        with self.assertRaisesRegex(LibraryError,'Download'):self.request()
        def restore(client,photo_id):
            self.library.original(self.library.photo(photo_id)).write_bytes(self.path.read_bytes())
            self.library.edit(photo_id,'hidden_set','1')
        with patch('photos.worker.Client.materialize',restore), patch('photos.qr.scan',side_effect=AssertionError('Revoked original must not scan')):
            with self.assertRaisesRegex(LibraryError,'Hidden is locked'):
                process(dict(action='detect_qr',id=self.id),{**self.config,'node':'http://127.0.0.1:1234','node_id':'1'*64})

    def test_parser_limits_and_malformed_results(self):
        base = dict(qr_width=100,qr_height=100,qr_unsupported=0,
            qr_codes=[dict(text='Line 1\nLine 2\t"\\',x=0,y=0,width=50,height=50)])
        self.assertEqual(qr.parse(json.dumps(base).encode())['qr_codes'][0]['text'],base['qr_codes'][0]['text'])
        invalid = [None, [], dict(base,qr_width=True), dict(base,qr_width=5000),dict(base,qr_unsupported=-1),dict(base,qr_codes=None),dict(base,qr_codes=base['qr_codes']*33)]
        for change in [dict(text='\0bad'),dict(text='\x01bad'),dict(text='\x7fbad'),dict(text='a'*8193),dict(text='\ud800'),dict(x=-1),dict(width=101),dict(height=0),dict(y=True)]:
            invalid.append({**base,'qr_codes':[{**base['qr_codes'][0],**change}]})
        invalid.append({**base,'qr_codes':[{**base['qr_codes'][0],'text':'a'*8192}]*5})
        for value in invalid:
            with self.assertRaises(LibraryError):qr.parse(json.dumps(value).encode())
        for value in [b'garbage',b'\xff',b' '*262145]:
            with self.assertRaises(LibraryError):qr.parse(value)

    def test_process_bounds_no_display_shell_or_network_and_failures(self):
        run = subprocess.run
        def inspect(args, **kwargs):
            self.assertNotIn('shell',kwargs)
            self.assertEqual(args[:4],['prlimit','--as=1610612736','--cpu=20','--'])
            for key in ['DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER']:
                self.assertNotIn(key,kwargs['env'])
            return run(args,**kwargs)
        with patch('photos.qr.subprocess.run',inspect),patch('photos.worker.Client.materialize',side_effect=AssertionError('Offline scan must stay offline')):
            self.assertEqual(len(self.request()['qr_codes']),3)
        for error in [OSError(),subprocess.TimeoutExpired('synthetic',25)]:
            with patch('photos.qr.subprocess.run',side_effect=error):
                with self.assertRaisesRegex(LibraryError,'could not finish'):self.request()
        for code in [3,5]:
            with patch('photos.qr.subprocess.run',return_value=subprocess.CompletedProcess([],code,b'',b'')):
                with self.assertRaises(LibraryError):self.request()
        with patch('photos.qr._slots.acquire',return_value=False):
            with self.assertRaisesRegex(LibraryError,'busy'):self.request()


if __name__ == '__main__':unittest.main()
