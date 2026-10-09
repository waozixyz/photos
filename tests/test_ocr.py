from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from photos import ocr, vault
from photos.library import Library, LibraryError, digest_file
from photos.worker import process


def text_image(path):
    image = Image.new('RGB', (1000, 400), 'white')
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 64)
    draw = ImageDraw.Draw(image)
    draw.text((80, 90), 'HELLO PHOTOS', font=font, fill='black')
    draw.text((80, 200), 'LOCAL 12345', font=font, fill='black')
    image.save(path)


class TextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root/'source'; self.source.mkdir()
        self.path = self.source/'Text.png'; text_image(self.path)
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source]); self.id = digest_file(self.path)
        self.config = dict(root=str(self.library.root))
        with ocr._guard: ocr._cache.clear()

    def request(self, action='detect_text', **values):
        return process(dict(action=action, id=self.id, value='eng', **values), self.config)

    def actual(self):
        try: ocr.prepare('eng')
        except LibraryError as error: self.skipTest(str(error))

    def test_native_text_boxes_cache_and_unchanged_originals(self):
        self.actual(); before = self.path.read_bytes(); row = self.library.photo(self.id)
        with patch('photos.ocr.subprocess.run', wraps=subprocess.run) as command:
            first = self.request(); second = self.request()
            self.assertEqual(command.call_count, 1)
        self.assertEqual(first['text_words'], second['text_words'])
        text = ' '.join(w['text'] for w in first['text_words'])
        for word in ['HELLO', 'PHOTOS', 'LOCAL', '12345']: self.assertIn(word, text)
        self.assertEqual((first['text_width'], first['text_height']), (1000, 400))
        for word in first['text_words']:
            self.assertGreater(word['width'], 0); self.assertGreater(word['height'], 0)
            self.assertLessEqual(word['x']+word['width'], 10000)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.library.original(row).read_bytes(), before)
        self.assertEqual(self.library.photo(self.id), row)
        self.assertFalse((self.library.root/'private/ocr').exists())
        with patch('photos.ocr.scan', side_effect=AssertionError('Query must not detect text')):
            self.assertNotIn('text_words', process(dict(action='query'), self.config))

    def test_native_exif_orientation_blank_and_large_image(self):
        self.actual()
        camera = self.source/'Camera.jpg'
        exif = Image.Exif(); exif[274] = 6
        with Image.open(self.path) as source:
            source.transpose(Image.Transpose.ROTATE_90).save(camera, exif=exif, quality=100)
        blank = self.source/'Blank.png'; Image.new('RGB', (5000, 100), 'white').save(blank)
        self.library.import_folders([self.source])
        result = process(dict(action='detect_text', id=digest_file(camera)), self.config)
        self.assertEqual((result['text_width'],result['text_height']),(1000,400))
        self.assertIn('HELLO',' '.join(w['text'] for w in result['text_words']))
        result = process(dict(action='detect_text', id=digest_file(blank)), self.config)
        self.assertEqual(result['text_width'],4096); self.assertEqual(result['text_words'],[])
        alpha=self.source/'Alpha.png'
        with Image.open(self.path) as image:
            image=image.convert('RGBA');pixels=image.load()
            for y in range(image.height):
                for x in range(image.width):
                    if pixels[x,y][:3]==(255,255,255):pixels[x,y]=(0,0,0,0)
            image.save(alpha)
        self.library.import_folders([self.source])
        result=process(dict(action='detect_text',id=digest_file(alpha)),self.config)
        self.assertIn('HELLO',' '.join(w['text'] for w in result['text_words']))

    def test_locked_hidden_trash_video_and_expiring_scan_are_denied(self):
        self.library.edit(self.id,'hidden_set','1')
        with patch('photos.ocr.scan', side_effect=AssertionError('Locked scan must not run')):
            self.assertIn('error',self.request(view='hidden'))
        token = vault.authenticate(self.library, 'Synthetic OCR password', setup=True)
        def expire(library, photo, language):
            vault.lock(library, token)
            return dict(text_words=[dict(text='SECRET')],text_width=1000,text_height=400)
        with patch('photos.ocr.scan',side_effect=expire):
            with self.assertRaisesRegex(LibraryError,'Hidden is locked'):
                self.request(view='hidden',vault_token=token)
        self.library.edit(self.id,'hidden_set','0');self.library.edit(self.id,'trash')
        with self.assertRaisesRegex(LibraryError,'Restore'): self.request()
        self.library.edit(self.id,'restore')
        self.library.db.execute('UPDATE photos SET kind="video" WHERE id=?',(self.id,));self.library.db.commit()
        with self.assertRaisesRegex(LibraryError,'Choose a photo'): self.request()

    def test_concurrent_hide_or_trash_and_cache_hits_require_fresh_access(self):
        self.actual(); self.request()
        self.library.edit(self.id,'hidden_set','1')
        self.assertIn('error', self.request())
        self.assertIn('error', self.request('text_check'))
        self.library.edit(self.id,'hidden_set','0')
        def trash(library,photo,language):
            self.library.edit(self.id,'trash')
            return dict(text_words=[dict(text='DO NOT RETURN')],text_width=1000,text_height=400)
        with patch('photos.ocr.scan',side_effect=trash):
            with self.assertRaisesRegex(LibraryError,'Restore'): self.request()

    def test_hidden_results_never_enter_cache_and_checks_return_no_text(self):
        self.actual(); self.library.edit(self.id,'hidden_set','1')
        token = vault.authenticate(self.library, 'Synthetic hidden OCR password', setup=True)
        result=self.request(view='hidden',vault_token=token);self.assertTrue(result['text_words'])
        self.assertEqual(len(ocr._cache),0)
        check=self.request('text_check',view='hidden',vault_token=token)
        self.assertNotIn('text_words',check);self.assertEqual(check['text_checked'],1)
        vault.lock(self.library,token)
        self.assertIn('error',self.request('text_check',view='hidden',vault_token=token))

    def test_language_integrity_parser_limits_and_process_failure(self):
        for value in ['', None, [], '../eng', 'eng;rm', 'eng\0', 'eng+eng+eng+eng+eng']:
            with self.assertRaises(LibraryError): ocr.prepare(value)
        self.actual()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory); (path/'eng.traineddata').write_bytes(b'corrupt')
            with patch('photos.ocr.models',return_value=path):
                with self.assertRaisesRegex(LibraryError,'integrity'): ocr.prepare('eng')
        row=b'5\t1\t0\t0\t1\t1\t1\t1\t9\t9\t90\tText\n'
        self.assertEqual(ocr.parse(b'100\t100\n'+row)['text_words'][0]['text'],'Text')
        for output in [b'garbage',b'0\t10\n',b'5000\t10\n',b'100\t100\n'+row.replace(b'90',b'nan'),
                       b'100\t100\n'+row.replace(b'9\t9',b'999\t9'), b'100\t100\n'+row*1025]:
            with self.assertRaises(LibraryError): ocr.parse(output)
        for error in [OSError(),subprocess.TimeoutExpired('synthetic',50)]:
            with patch('photos.ocr.subprocess.run',side_effect=error):
                with self.assertRaisesRegex(LibraryError,'could not finish'): self.request()


if __name__ == '__main__': unittest.main()
