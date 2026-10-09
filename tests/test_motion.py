import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from motion_fixtures import jpeg, heif
from test_live import movie
from photos import editing, live, motion
from photos.export import Export
from photos.library import Library, LibraryError, digest_file, packed
from photos.sync import Client
from photos.worker import process


class MotionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source/Camera'; self.source.mkdir(parents=True)
        clip = self.root/'clip.mp4'; movie(clip); self.clip = clip.read_bytes()
        self.path = jpeg(self.source/'MomentMP.jpg', self.clip)
        self.original = self.path.read_bytes()
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        result = self.library.import_folders([self.source.parent])
        self.assertEqual(result['errors'], 0); self.assertEqual(result['warning'], 0)
        self.photo_id = digest_file(self.path)
        self.config = dict(root=str(self.library.root))

    def request(self, **request):
        return process({**request, 'gallery_format':2}, self.config)

    def test_modern_legacy_namespace_padding_gainmap_and_exact_motion_bytes(self):
        for modern, prefix, padding, gainmap in [(True,'Camera',0,b''),(False,'GCamera',0,b''),
                (True,'Anything',32,b'Gain map pixels'),(True,'GCamera',0,b'')]:
            with self.subTest(modern=modern,prefix=prefix,padding=padding):
                path = jpeg(self.root/'fixture.jpg',self.clip,modern=modern,prefix=prefix,padding=padding,gainmap=gainmap)
                output = self.root/'extracted.mp4'; output.unlink(missing_ok=True)
                value = motion.read(path,output)
                self.assertEqual(value['length'],len(self.clip))
                self.assertEqual(value['offset'],path.stat().st_size-len(self.clip))
                self.assertEqual(output.read_bytes(),self.clip)
                with self.assertRaises(LibraryError): motion.read(path,output)

    def test_gallery_playback_properties_and_original_are_one_item(self):
        result = self.request()
        self.assertEqual(result['count'],1); self.assertEqual(result['total'],1)
        self.assertEqual(result['photos'][0]['kind'],'live')
        self.assertEqual(self.library.query(search='type:live')['count'],1)
        self.assertEqual(self.library.details(self.photo_id)['size'],len(self.original))
        reply = self.request(action='playback_check',id=self.photo_id)
        self.assertEqual(Path(reply['playback_path']).read_bytes(),self.clip)
        with patch.object(motion,'read',side_effect=AssertionError('Validated cache should be reused')):
            self.assertEqual(self.request(action='playback_check',id=self.photo_id)['playback_path'],reply['playback_path'])
        for action,value in [('caption','Moment'),('favorite_set','1'),('location','1, 2'),('taken','2024-06-01T00:00:00Z')]:
            self.library.edit(self.photo_id,action,value)
        self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.details(self.photo_id)['caption'],'Moment')
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(self.library.original(self.library.photo(self.photo_id)).read_bytes(),self.original)

    def test_heic_avif_mpvd_containers_decode_and_play_exact_motion(self):
        for name,codec in [('MomentMP.heic',1),('MomentMP.avif',4)]:
            with self.subTest(name=name):
                path = heif(self.source/name,self.clip,codec)
                original = path.read_bytes()
                self.assertEqual(motion.read(path)['length'],len(self.clip))
                self.library.import_folders([self.source.parent]); photo_id = digest_file(path)
                self.assertTrue(self.library.details(photo_id)['live'])
                from photos import decoding
                with Image.open(decoding.display(self.library,self.library.photo(photo_id))) as decoded:
                    self.assertEqual(decoded.size,(64,96))
                result = self.request(action='playback_check',id=photo_id)
                self.assertEqual(Path(result['playback_path']).read_bytes(),self.clip)
                self.assertEqual(path.read_bytes(),original)
                bad = self.root/name; bad.write_bytes(original+b'Extra trailing bytes')
                self.assertIsNone(motion.read(bad))

    def test_stale_flags_invalid_offsets_xml_entities_and_truncated_movies_remain_stills(self):
        cases = [dict(flag=0),dict(flag=2),dict(length=0),dict(length=len(self.clip)+1),
                 dict(modern=False,length=9999999999),dict(xml='<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>'),
                 dict(xml='<broken'),dict(xml='<x xmlns:Camera="wrong" Camera:MotionPhoto="1"/>')]
        for index, change in enumerate(cases):
            path = jpeg(self.source/f'Bad {index}.jpg',self.clip,**change)
            self.assertIsNone(motion.read(path))
        for clip in [self.clip[:-5],b'xxxxftyp'+self.clip,b'\0\0\0\x08ftyp'+b'x'*32]:
            path = jpeg(self.root/'bad.jpg',clip)
            self.assertIsNone(motion.read(path))
        self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.query(search='type:live')['count'],1)
        self.assertEqual(self.library.query()['total'],1+len(cases))

    def test_hidden_trash_concurrent_revocation_and_cache_integrity(self):
        first = self.request(action='playback_check',id=self.photo_id)
        Path(first['playback_path']).write_bytes(b'Tampered cache')
        second = self.request(action='playback_check',id=self.photo_id)
        self.assertEqual(Path(second['playback_path']).read_bytes(),self.clip)
        self.library.edit(self.photo_id,'hidden_set','1')
        self.assertEqual(self.request()['photos'],[])
        self.assertIn('error',self.request(action='playback_check',id=self.photo_id))
        self.library.edit(self.photo_id,'hidden_set','0'); self.library.edit(self.photo_id,'trash')
        with self.assertRaises(LibraryError): self.request(action='playback_check',id=self.photo_id)
        self.library.edit(self.photo_id,'restore')
        motion._cache.clear()
        native = motion.read
        def hide(*args):
            result = native(*args)
            other = Library(self.library.root)
            try: other.edit(self.photo_id,'hidden_set','1')
            finally: other.db.close()
            return result
        with patch.object(motion,'read',hide):
            with self.assertRaises(LibraryError): motion.extract(self.library,self.photo_id,lambda p:not p['hidden'])
        self.assertEqual(list((self.library.root/'private/motion').glob('.motion-*')),[])
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_export_and_encrypted_metadata_restore_keep_single_combined_original(self):
        selected = self.root/'selected'; self.library.export(self.photo_id,selected)
        mirror = self.root/'mirror'
        with Export(self.library,str(mirror)) as export: export.run()
        for folder in [selected,mirror]:
            peer = Library(self.root/('restored-'+folder.name)); self.addCleanup(peer.db.close)
            peer.import_folders([folder])
            self.assertEqual(peer.query(search='type:live')['count'],1)
            self.assertEqual(peer.query()['total'],1)
            self.assertEqual(peer.original(peer.photo(self.photo_id)).read_bytes(),self.original)
        sender = Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64))
        receiver = Client(peer,dict(node='http://127.0.0.1:18080',node_id='a'*64),sender.identity_path)
        record = sender.record(self.library.photo(self.photo_id),manifest={})
        self.assertNotIn('photosMotion',json.dumps(record))
        receiver.accept(record); peer.db.commit()
        self.assertEqual(motion.descriptor(peer.photo(self.photo_id)),motion.descriptor(self.library.photo(self.photo_id)))

    def test_separation_saves_motion_retains_original_metadata_and_rescan_state(self):
        self.library.edit(self.photo_id,'caption','Keep this moment')
        self.request(action='live_separate',id=self.photo_id)
        motion_id = digest_file(self.root/'clip.mp4')
        self.assertEqual(self.library.query()['count'],2)
        self.assertEqual(self.library.query(search='type:live')['count'],0)
        self.assertEqual(self.library.photo(motion_id)['caption'],'Keep this moment')
        self.assertEqual(self.library.original(self.library.photo(motion_id)).read_bytes(),self.clip)
        self.assertEqual(self.library.original(self.library.photo(self.photo_id)).read_bytes(),self.original)
        self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.query(search='type:live')['count'],0)
        self.assertEqual(self.path.read_bytes(),self.original)
        output = self.root/'separated'; self.library.export(self.photo_id,output); self.library.export(motion_id,output)
        peer = Library(self.root/'separated-peer'); self.addCleanup(peer.db.close); peer.import_folders([output])
        self.assertEqual(peer.query(search='type:live')['count'],0)
        self.assertEqual(peer.query()['count'],2)

    def test_still_edits_have_no_motion_and_forged_sidecars_are_not_authoritative(self):
        saved = editing.save(self.library,self.photo_id,[90,0,0,100,100,100,0])
        self.assertIsNone(motion.descriptor(self.library.photo(saved)))
        ordinary = self.source/'Ordinary.jpg'; Image.new('RGB',(96,64),'blue').save(ordinary)
        ordinary.with_name(ordinary.name+'.json').write_text(packed(dict(photosMotion=motion.descriptor(self.library.photo(self.photo_id)),photosMotionScan=1)))
        self.library.import_folders([self.source.parent])
        self.assertIsNone(motion.descriptor(self.library.photo(digest_file(ordinary))))
        self.assertEqual(self.library.query(search='type:live')['count'],1)

    def test_negative_scan_is_local_and_unchanged_rescans_do_not_queue_metadata(self):
        path = self.source/'Ordinary.jpg'; Image.new('RGB',(96,64),'blue').save(path)
        self.library.import_folders([self.source.parent]); photo_id = digest_file(path)
        self.assertNotIn('photosMotionScan',json.loads(self.library.photo(photo_id)['metadata']))
        self.library.db.execute('UPDATE photos SET dirty=0'); self.library.db.commit()
        with patch.object(motion,'read',side_effect=AssertionError('Unchanged originals should not be rescanned')):
            self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.db.execute('SELECT COUNT(*) FROM photos WHERE dirty=1').fetchone()[0],0)

    def test_short_motion_is_playable_and_malformed_descriptors_are_not_live(self):
        path = self.root/'short.mp4'
        subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-i',str(self.root/'clip.mp4'),'-t','0.5','-c','copy',str(path)],check=True,capture_output=True,timeout=8)
        jpeg(self.source/'ShortMP.jpg',path.read_bytes()); self.library.import_folders([self.source.parent])
        reply = self.request(action='playback_check',id=digest_file(self.source/'ShortMP.jpg'))
        self.assertEqual(Path(reply['playback_path']).read_bytes(),path.read_bytes())
        metadata = json.loads(self.library.photo(self.photo_id)['metadata'])
        metadata['photosMotion']['version'] = True
        self.library.db.execute('UPDATE photos SET metadata=? WHERE id=?',(packed(metadata),self.photo_id)); self.library.db.commit()
        self.assertIsNone(motion.descriptor(self.library.photo(self.photo_id)))
        self.assertEqual(self.library.query(search='type:live')['count'],1)


if __name__ == '__main__':
    unittest.main()
