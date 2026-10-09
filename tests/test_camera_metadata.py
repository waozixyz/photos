"""Native camera and provider readers, using only synthetic originals."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, TiffImagePlugin

from codec_fixtures import heic
from photos import importing
from photos.library import Library, LibraryError, digest_file, packed
from photos.sync import Client


def camera_exif(latitude='S', longitude='W', degrees=25, minutes=18, seconds=0):
    exif = Image.Exif()
    exif[306] = '2020:01:02 03:04:05'
    exif[34665] = {36867: '2024:06:15 12:30:00'}
    rational = TiffImagePlugin.IFDRational
    exif[34853] = {1: latitude, 2: (rational(degrees), rational(minutes), rational(seconds)),
                   3: longitude, 4: (rational(57), rational(36), rational(0))}
    return exif


class CameraMetadataTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root/'Camera'; self.source.mkdir()
        self.path = self.source/'旅行.jpg'
        Image.new('RGB', (18, 12), 'green').save(self.path, exif=camera_exif())
        self.original = self.path.read_bytes()
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)

    def test_jpeg_png_webp_and_tiff_keep_camera_time_gps_and_source_bytes(self):
        originals = {}
        for extension in ['jpg', 'png', 'webp', 'tiff']:
            path = self.source/('Photo.'+extension)
            Image.new('RGB', (18, 12), 'blue').save(path, exif=camera_exif().tobytes())
            originals[path] = path.read_bytes()
        with importing.Reader() as reader:
            child = reader.child
            for path in originals:
                fields = importing.camera(path, 1600000000, reader)
                self.assertEqual((fields['width'], fields['height'], fields['taken']), (18, 12, 1718454600))
                self.assertAlmostEqual(fields['gps']['latitude'], -25.3)
                self.assertAlmostEqual(fields['gps']['longitude'], -57.6)
                self.assertIs(reader.child, child)
        self.assertIsNotNone(child.poll()); self.assertTrue(child.stdout.closed)
        result = self.library.import_folders([self.source])
        self.assertEqual((result['added'], result['errors'], result['warning']), (5, 0, 0))
        for path, original in originals.items():
            photo = self.library.photo(digest_file(path))
            self.assertEqual(photo['taken'], 1718454600)
            self.assertEqual(json.loads(photo['metadata'])['geoDataExif']['latitude'], -25.3)
            self.assertEqual(self.library.original(photo).read_bytes(), original)
            self.assertEqual(path.read_bytes(), original)

    def test_heic_camera_metadata_and_zero_or_dateline_coordinates(self):
        path = self.source/'Phone.heic'
        exif = camera_exif(); rational = TiffImagePlugin.IFDRational
        exif[34853] = {1: 'N', 2: (rational(0), rational(0), rational(0)),
                       3: 'W', 4: (rational(180), rational(0), rational(0))}
        heic(path, exif=exif.tobytes())
        original = path.read_bytes()
        with importing.Reader() as reader:
            fields = importing.camera(path, 1600000000, reader)
        self.assertEqual((fields['width'], fields['height'], fields['taken']), (96, 64, 1718454600))
        self.assertEqual(fields['gps'], dict(latitude=0, longitude=-180))
        self.assertEqual(self.library.import_file(path, self.source), 'added')
        photo = self.library.photo(digest_file(path))
        self.assertEqual(json.loads(photo['metadata'])['geoDataExif'], dict(latitude=0, longitude=-180))
        self.assertEqual(self.library.original(photo).read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)

    def test_provider_precedence_unicode_encodings_exact_stamp_and_unknown_fields(self):
        metadata = {'description': '日本語\n\0', 'unknown': {'huge': 10**400, 'null': None},
                    'photoTakenTime': {'timestamp': '1600000000.125'}}
        fallback = self.path.with_name(self.path.name+'.json')
        fallback.write_text('{"description":"adjacent"}')
        folder = self.source/'.meta'; folder.mkdir(); cli = folder/(self.path.name+'.json')
        cli.write_text('{"description":"CLI"}')
        folder = self.source/'metadata'; folder.mkdir(); desktop = folder/(self.path.name+'.json')
        for encoding in ['utf-8-sig', 'utf-16', 'utf-16-be', 'utf-32', 'utf-32-be']:
            with self.subTest(encoding=encoding), importing.Reader() as reader:
                desktop.write_bytes(packed(metadata).encode(encoding))
                os.utime(desktop, ns=(1791540000123456789, 1791540000123456789))
                value, stamp = importing.sidecar(self.path, reader)
                self.assertEqual(value, metadata)
                self.assertEqual(stamp, f'{desktop}:{desktop.stat().st_size}:1791540000123456789')
                self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], 1600000000.125)
        desktop.unlink()
        with importing.Reader() as reader:
            self.assertEqual(importing.sidecar(self.path, reader)[0]['description'], 'CLI')
            cli.unlink()
            self.assertEqual(importing.sidecar(self.path, reader)[0]['description'], 'adjacent')
            fallback.unlink()
            self.assertEqual(importing.sidecar(self.path, reader), ({}, ''))
            self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], 1718454600)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_capture_override_invalid_values_and_duplicate_keys_match_imported_metadata(self):
        sidecar = self.path.with_name(self.path.name+'.json')
        values = [({}, 1718454600), ({'photoTakenTime': {}}, 1718454600),
                  ({'photoTakenTime': None}, 1700000000),
                  ({'photoTakenTime': {'timestamp': 'bad'}}, 1700000000),
                  ({'photoTakenTime': {'timestamp': 0}}, 1700000000),
                  ({'photoTakenTime': {'timestamp': False}}, 1700000000),
                  ({'photoTakenTime': {'timestamp': 10**400}}, 1700000000),
                  ({'photoTakenTime': {'timestamp': '1600000000.25\t'}}, 1600000000.25)]
        with importing.Reader() as reader:
            for value, expected in values:
                sidecar.write_text(packed(value))
                self.assertEqual(importing.sidecar(self.path, reader)[0], value)
                self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], expected)
            for raw in ['{"photoTakenTime":null,"photoTakenTime":{"timestamp":"1600000000"}}',
                        '{"photoTakenTime":{"timestamp":"bad","timestamp":"1600000000"}}']:
                sidecar.write_text(raw)
                self.assertEqual(importing.sidecar(self.path, reader)[0], json.loads(raw))
                self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], 1600000000)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_sidecars_warn_without_dropping_or_modifying_originals(self):
        sidecar = self.path.with_name(self.path.name+'.json')
        for value in [b'', b'{', b'[]', b'{"bad":"\xff"}', b'{}'+b' '*(4*1024*1024)]:
            with self.subTest(value=value[:16]):
                sidecar.write_bytes(value)
                self.assertEqual(self.library.import_file(self.path, self.source), 'warning')
                photo = self.library.photo(digest_file(self.path))
                self.assertEqual(self.library.original(photo).read_bytes(), self.original)
                self.assertEqual(self.path.read_bytes(), self.original)
                self.library.db.execute('DELETE FROM sources'); self.library.db.commit()
        sidecar.unlink(); sidecar.symlink_to(self.path)
        with importing.Reader() as reader:
            with self.assertRaises(LibraryError): importing.sidecar(self.path, reader)
        sidecar.unlink(); os.mkfifo(sidecar)
        with importing.Reader() as reader:
            with self.assertRaises(LibraryError): importing.sidecar(self.path, reader)

    def test_crashed_reader_restarts_and_rejects_nonregular_originals(self):
        with importing.Reader() as reader:
            child = reader.child; child.kill(); child.wait(timeout=2)
            fields = importing.camera(self.path, 1700000000, reader)
            self.assertEqual(fields['taken'], 1718454600)
            self.assertIsNot(child, reader.child); self.assertTrue(child.stdout.closed)
            second = reader.child
            link = self.source/'Link.jpg'; link.symlink_to(self.path)
            pipe = self.source/'Pipe.jpg'; os.mkfifo(pipe)
            for path in [link, pipe, self.source]:
                with self.assertRaises(LibraryError): importing.camera(path, 1700000000, reader)
            self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], 1718454600)
            self.assertIs(reader.child, second)
        self.assertIsNone(reader.child); self.assertIsNotNone(second.poll())
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_malformed_camera_bounds_and_gps_do_not_poison_the_next_read(self):
        exif = camera_exif(minutes=60)
        invalid = self.source/'InvalidGPS.jpg'
        Image.new('RGB', (18, 12), 'red').save(invalid, exif=exif)
        png = self.source/'Checksum.png'
        Image.new('RGB', (18, 12), 'red').save(png, exif=camera_exif())
        raw = bytearray(png.read_bytes()); at = raw.index(b'eXIf')
        raw[at+4] ^= 1; png.write_bytes(raw)
        broken = self.source/'Broken.tiff'
        with importing.Reader() as reader:
            self.assertIsNone(importing.camera(invalid, 1700000000, reader)['gps'])
            fields = importing.camera(png, 1700000000, reader)
            self.assertEqual(fields['taken'], 1700000000); self.assertIsNone(fields['gps'])
            for raw in [b'II*\0'+struct.pack('<I', 0xffffffff),
                        b'MM\0*'+struct.pack('>I', 8)+b'\xff\xff',
                        b'II+\0\x08\0\0\0'+struct.pack('<Q', 2**63), b'not a camera file']:
                broken.write_bytes(raw)
                fields = importing.camera(broken, 1700000000, reader)
                self.assertEqual(fields['taken'], 1700000000); self.assertIsNone(fields['gps'])
            self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], 1718454600)

    def test_timed_out_reader_is_reaped_and_the_next_request_can_retry(self):
        with importing.Reader() as reader:
            child = reader.child
            with patch('photos.importing.select.select', return_value=([], [], [])):
                with self.assertRaisesRegex(LibraryError, 'timed out'):
                    importing.camera(self.path, 1700000000, reader)
            self.assertIsNotNone(child.poll())
            self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], 1718454600)
            self.assertTrue(child.stdout.closed)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_import_batch_owns_one_reader_and_closes_it_after_failed_discovery(self):
        actual = importing.Reader; readers = []
        def opened():
            reader = actual(); readers.append(reader); return reader
        with patch('photos.importing.Reader', side_effect=opened):
            self.assertEqual(self.library.import_folders([self.source])['added'], 1)
        self.assertEqual(len(readers), 1); self.assertIsNone(readers[0].child)
        with patch('photos.importing.Reader', side_effect=opened), \
             patch('photos.importing.walk', side_effect=LibraryError('Discovery interrupted')):
            with self.assertRaises(LibraryError): self.library.import_folders([self.source])
        self.assertIsNone(readers[-1].child); self.assertIsNone(self.library.import_reader)

    def test_historic_camera_dates_import_export_and_encrypted_restore(self):
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a'*64))
        for date, timestamp in [('1960:01:02 03:04:05', -315521755),
                                ('1970:01:01 00:00:00', 0), ('1969:12:31 23:59:59', -1)]:
            with self.subTest(date=date):
                exif = camera_exif(); exif[34665] = {36867: date}
                Image.new('RGB', (18, 12), 'green').save(self.path, exif=exif)
                original = self.path.read_bytes()
                self.assertEqual(self.library.import_file(self.path, self.source), 'added')
                identity = digest_file(self.path); photo = self.library.photo(identity)
                self.assertEqual(photo['taken'], timestamp)
                record = client.record(photo, dict(chunks=[]))
                self.library.db.execute('DELETE FROM photos WHERE id=?', (identity,)); self.library.db.commit()
                client.accept(record); self.library.db.commit()
                self.assertEqual(self.library.photo(identity)['taken'], timestamp)
                exported = self.library.export(identity, self.root/'export')
                self.assertEqual(Path(exported).read_bytes(), original)
                self.assertEqual(self.path.read_bytes(), original)

    def test_exif_subseconds_offsets_and_modified_date_fallback(self):
        with importing.Reader() as reader:
            for offset, fraction, expected in [('+02:00', '123456', 1718447400.123456),
                                               ('-03:30', '5', 1718467200.5),
                                               ('bad', 'invalid', 1718454600)]:
                with self.subTest(offset=offset):
                    exif = camera_exif()
                    exif[34665] = {36867: '2024:06:15 12:30:00', 36881: offset, 37521: fraction}
                    Image.new('RGB', (18, 12), 'green').save(self.path, exif=exif)
                    self.assertAlmostEqual(importing.camera(self.path, 1700000000, reader)['taken'], expected)
            exif = Image.Exif(); exif[306] = '2024:06:15 12:30:00'
            exif[34665] = {36880: '+02:00', 37520: '125'}
            Image.new('RGB', (18, 12), 'green').save(self.path, exif=exif)
            self.assertEqual(importing.camera(self.path, 1700000000, reader)['taken'], 1718447400.125)

    def test_bigtiff_and_both_byte_orders_bound_inline_dimensions(self):
        with importing.Reader() as reader:
            for big in [False, True]:
                for little in [False, True]:
                    order = '<' if little else '>'; marker = b'II' if little else b'MM'
                    path = self.source/'Synthetic.dng'
                    if big:
                        raw = marker+struct.pack(order+'HHHQ', 43, 8, 0, 16)+struct.pack(order+'Q', 2)
                        for tag, value in [(256, 96), (257, 64)]:
                            raw += struct.pack(order+'HHQ', tag, 4, 1)+struct.pack(order+'I', value)+b'\0'*4
                        raw += b'\0'*8
                    else:
                        raw = marker+struct.pack(order+'HIH', 42, 8, 2)
                        for tag, value in [(256, 96), (257, 64)]: raw += struct.pack(order+'HHII', tag, 4, 1, value)
                        raw += b'\0'*4
                    path.write_bytes(raw)
                    fields = importing.camera(path, 1700000000, reader)
                    self.assertEqual((fields['width'], fields['height']), (96, 64))
                    self.assertEqual(fields['taken'], 1700000000); self.assertIsNone(fields['gps'])
                    self.assertEqual(path.read_bytes(), raw)

    def test_album_definition_native_read_preserves_unicode_and_later_edits(self):
        sidecar = self.source/'.photos-album.json'
        metadata = dict(name='日本語', description='é\nAlbum description', pinned=True, archived=False)
        sidecar.write_bytes(packed(metadata).encode('utf-16'))
        original = sidecar.read_bytes()
        result = self.library.import_folders([self.source])
        self.assertEqual((result['added'], result['warning']), (1, 0))
        album = self.library.db.execute('SELECT * FROM albums WHERE name=?', ('日本語',)).fetchone()
        self.assertEqual((album['description'], album['pinned']), ('é\nAlbum description', 1))
        self.library.edit_album('日本語', 'description', 'Keep later description')
        self.assertEqual(self.library.import_folders([self.source])['unchanged'], 1)
        self.assertEqual(self.library.db.execute('SELECT description FROM albums WHERE name=?', ('日本語',)).fetchone()[0], 'Keep later description')
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), self.original)
        with importing.Reader() as reader:
            sidecar.unlink()
            self.assertIsNone(importing.album_metadata(sidecar, reader))
            sidecar.write_bytes(b'{}'+b' '*16384)
            with self.assertRaises(LibraryError): importing.album_metadata(sidecar, reader)
            sidecar.unlink(); os.mkfifo(sidecar)
            with self.assertRaises(LibraryError): importing.album_metadata(sidecar, reader)
        self.assertEqual(self.library.import_folders([self.source])['warning'], 1)


if __name__ == '__main__': unittest.main()
