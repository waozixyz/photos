import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from photos import editing, video, vault
from photos.library import Library, LibraryError, digest_file
from photos.worker import process


def fixture(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-f','lavfi','-i',
                    'testsrc2=size=120x80:rate=10:duration=4','-f','lavfi','-i',
                    'sine=frequency=440:duration=4','-c:v','libx264','-threads','1',
                    '-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)],
                   env=video.environment(), check=True, capture_output=True)


class VideoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'source/Trip/Video with spaces.mp4'
        fixture(self.source)
        self.original = self.source.read_bytes()
        self.library = Library(self.root/'library')
        self.library.import_folders([self.source.parents[1]])
        self.photo_id = digest_file(self.source)
        self.config = dict(root=str(self.library.root))

    def tearDown(self):
        self.library.db.close()
        self.temp.cleanup()

    def test_playback_authorization_is_small_read_only_and_refuses_trash_and_missing_media(self):
        before = self.library.photo(self.photo_id)
        result = process(dict(action='playback_check',id=self.photo_id),self.config)
        self.assertEqual(result['playback_id'],self.photo_id)
        self.assertEqual(result['playback_path'],str(self.library.original(before)))
        self.assertNotIn('photos',result)
        self.assertEqual(self.library.photo(self.photo_id),before)
        self.library.edit(self.photo_id,'trash')
        with self.assertRaisesRegex(LibraryError,'outside Trash'):
            process(dict(action='playback_check',id=self.photo_id),self.config)
        self.library.edit(self.photo_id,'restore')
        self.library.original(before).unlink()
        with self.assertRaisesRegex(LibraryError,'Download'):
            process(dict(action='playback_check',id=self.photo_id),self.config)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_playback_hidden_lock_expiry_and_expiry_at_publication_never_expose_a_path(self):
        self.library.edit(self.photo_id,'hidden')
        token = vault.authenticate(self.library,'Synthetic video password',setup=True)
        request = dict(action='playback_check',id=self.photo_id,view='hidden',vault_token=token)
        self.assertIn('playback_path',process(request,self.config))
        from photos import access_sessions
        shortened = access_sessions.Registry(session_ms=30); self.addCleanup(shortened.close)
        with patch('photos.access_sessions.registry',shortened):
            token=vault.authenticate(self.library,'Synthetic video password')
            request['vault_token']=token; time.sleep(.05)
            result = process(request,self.config)
        self.assertIn('error',result)
        self.assertNotIn('playback_path',result)
        token = vault.authenticate(self.library,'Synthetic video password')
        with patch('photos.vault.state',return_value=dict(vault_configured=1,vault_unlocked=0)):
            with self.assertRaisesRegex(LibraryError,'Hidden is locked'):
                process({**request,'vault_token':token},self.config)
        vault.lock(self.library,token)
        self.assertNotIn('playback_path',process({**request,'vault_token':token},self.config))

    def test_playback_rechecks_access_after_downloading_the_original(self):
        stored = self.library.original(self.library.photo(self.photo_id))
        stored.unlink()
        def download(client,photo_id):
            stored.write_bytes(self.original)
            self.library.edit(photo_id,'hidden')
        with patch('photos.sync.Client.materialize',download):
            with self.assertRaisesRegex(LibraryError,'Hidden is locked'):
                process(dict(action='playback_check',id=self.photo_id),{**self.config,'node':'http://127.0.0.1:18080','node_id':'ab'*32})
        self.assertEqual(stored.read_bytes(),self.original)

    def test_trim_rotate_crop_copy_keeps_audio_metadata_source_and_export(self):
        self.library.edit(self.photo_id, 'caption', 'A video caption')
        self.library.edit(self.photo_id, 'tags', 'family')
        self.library.edit(self.photo_id, 'location', '-25.3, -57.6')
        self.library.edit(self.photo_id, 'favorite')
        self.library.edit(self.photo_id, 'archived')
        result = process(dict(action='video_save',id=self.photo_id,value='[1000,3000,90,1]'),self.config)
        token = result['video_job']
        for _ in range(200):
            result = process(dict(action='video_status',video_job=token),self.config)
            if not result['video_working']:
                break
            time.sleep(.05)
        self.assertFalse(result['video_working'])
        self.assertFalse(result['error'])
        copy = self.library.photo(result['saved_copy'])
        info = video.probe(self.library.original(copy))
        self.assertEqual((info['width'],info['height']), (80,80))
        self.assertAlmostEqual(info['duration_ms'],2000,delta=150)
        self.assertTrue(info['audio'])
        self.assertEqual(copy['caption'],'A video caption')
        self.assertEqual(copy['tags'],'family')
        self.assertEqual(copy['taken'],self.library.photo(self.photo_id)['taken'])
        self.assertEqual(copy['albums'],self.library.photo(self.photo_id)['albums'])
        self.assertEqual((copy['favorite'],copy['archived']),(1,1))
        self.assertEqual(self.library.details(copy['id'])['location'],'-25.3, -57.6')
        self.assertEqual(json.loads(copy['metadata'])['photosVideoEdit']['parent'],self.photo_id)
        self.library.edit(copy['id'],'caption','A later copy caption')
        self.library.edit(copy['id'],'trash')
        repeated=video.save(self.library,self.photo_id,[1000,3000,90,1])
        self.assertEqual(repeated,copy['id'])
        self.assertEqual(self.library.photo(repeated)['caption'],'A later copy caption')
        self.assertTrue(self.library.photo(repeated)['trashed'])
        self.library.edit(repeated,'restore')
        copy=self.library.photo(repeated)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual(self.library.original(self.library.photo(self.photo_id)).read_bytes(),self.original)
        self.library.export(copy['id'],self.root/'export')
        restored = Library(self.root/'restored')
        try:
            restored.import_folders([self.root/'export'])
            self.assertEqual(restored.photo(copy['id'])['caption'],copy['caption'])
            self.assertEqual(restored.details(copy['id'])['location'],'-25.3, -57.6')
            self.assertEqual(digest_file(restored.original(restored.photo(copy['id']))),copy['id'])
        finally:
            restored.db.close()

    def test_custom_video_crop_after_rotation_keeps_audio_metadata_and_original(self):
        self.library.edit(self.photo_id, 'caption', 'Cropped video')
        self.library.edit(self.photo_id, 'location', '-25.3, -57.6')
        values = [1000, 3000, 90, 6, 2500, 2500, 5000, 5000]
        preview = video.preview(self.library, self.photo_id, values)
        self.assertGreater(preview['edit_width'], 0)
        self.assertGreater(preview['edit_height'], 0)
        copy_id = video.save(self.library, self.photo_id, values)
        copy = self.library.photo(copy_id)
        info = video.probe(self.library.original(copy))
        self.assertEqual((info['width'], info['height']), (40, 60))
        self.assertTrue(info['audio'])
        self.assertLess(abs(info['duration_ms'] - 2000), 150)
        self.assertEqual(copy['caption'], 'Cropped video')
        self.assertEqual(self.library.details(copy_id)['location'], '-25.3, -57.6')
        self.assertEqual(json.loads(copy['metadata'])['photosVideoEdit']['adjustments'], values)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.library.original(self.library.photo(self.photo_id)).read_bytes(), self.original)
        for rectangle in ([0, 0, 0, 10000], [-1, 0, 5000, 5000], [5000, 0, 5001, 10000],
                          [0, 0, 10000, False]):
            with self.assertRaises(LibraryError):
                video.save(self.library, self.photo_id, [0, 4000, 0, 6, *rectangle])
        self.assertEqual(self.library.query()['total'], 2)

    def test_bounded_frame_previews_and_cancel_retain_original(self):
        begin = process(dict(action='video_begin',id=self.photo_id),self.config)
        self.assertEqual(begin['video_duration'],4000)
        session=begin['edit_session']
        for crop in range(1,6):
            result=process(dict(action='video_preview',id=self.photo_id,value=[1000,3000,90,crop],edit_session=session),self.config)
            with Image.open(result['edit_preview']) as frame:
                self.assertLessEqual(max(frame.size),1280)
                self.assertGreater(min(frame.size),0)
        self.assertLessEqual(len(list(Path(result['edit_preview']).parent.glob('*.png'))),2)
        process(dict(action='image_close',edit_session=session),self.config)
        self.assertFalse(Path(result['edit_preview']).parent.exists())
        self.assertEqual(self.library.query()['total'],1)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_invalid_ranges_decoder_failure_and_trash_never_add_copy(self):
        for value in ([0,0,0,0],[0,999,0,0],[-1,3000,0,0],[0,5000,0,0],
                      [0,3000,45,0],[0,3000,0,6],[True,3000,0,0],'bad','[0,4000,0,0]'):
            with self.assertRaises(LibraryError):
                video.save(self.library,self.photo_id,value)
        with patch('photos.video.subprocess.run',side_effect=subprocess.TimeoutExpired('fixture',1)):
            with self.assertRaises(LibraryError):
                video.save(self.library,self.photo_id,[0,3000,90,0])
        self.library.edit(self.photo_id,'trash')
        with self.assertRaises(LibraryError):
            process(dict(action='video_begin',id=self.photo_id),self.config)
        self.assertEqual(self.library.query()['total'],1)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_hidden_expiry_and_concurrent_trash_during_processing_prevent_copy(self):
        self.library.edit(self.photo_id,'hidden')
        denied=process(dict(action='video_save',id=self.photo_id,value=[0,3000,90,0]),self.config)
        self.assertIn('locked',denied['error'])
        with self.assertRaises(LibraryError):
            video.save(self.library,self.photo_id,[0,3000,90,0],authorize=lambda photo:False)
        original_render=video.render
        def trash_during_render(*args,**kwargs):
            result=original_render(*args,**kwargs)
            self.library.edit(self.photo_id,'trash')
            return result
        with patch('photos.video.render',side_effect=trash_during_render):
            with self.assertRaises(LibraryError):
                video.save(self.library,self.photo_id,[0,3000,90,0],authorize=lambda photo:True)
        self.assertEqual(self.library.query()['total'],1)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertFalse(list((self.library.root/'private/edit-previews').glob('.video-*')))

    def test_background_save_cancel_is_bounded_and_does_not_create_a_copy(self):
        import threading
        begun=threading.Event();release=threading.Event()
        original_render=video.render
        def wait_render(*args,**kwargs):
            begun.set();release.wait(10)
            return original_render(*args,**kwargs)
        with patch('photos.video.render',side_effect=wait_render):
            start=time.monotonic()
            result=process(dict(action='video_save',id=self.photo_id,value=[0,3000,90,0]),self.config)
            self.assertLess(time.monotonic()-start,2)
            token=result['video_job'];self.assertTrue(begun.wait(2))
            with self.assertRaises(LibraryError):
                video.start(self.library,self.photo_id,[0,3000,90,0],'')
            response=process(dict(action='video_cancel',video_job=token),self.config)
            self.assertTrue(response['video_working'])
            release.set()
            for _ in range(200):
                response=video.state(self.library,token)
                if not response['video_working']:break
                time.sleep(.05)
            self.assertFalse(response['video_working'])
            self.assertFalse(response['saved_copy'])
            self.assertIn('cancelled',response['status'])
        self.assertEqual(self.library.query()['total'],1)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_service_restart_clears_the_ui_processing_session(self):
        # The worker's in-memory registry disappears when the service restarts.
        # A stale UI token must stop polling and allow another editing session.
        response=process(dict(action='video_status',video_job='a'*32),self.config)
        self.assertEqual(response['video_job'],'')
        self.assertEqual(response['video_working'],0)
        self.assertIn('session ended',response['error'])
        self.assertEqual(self.library.query()['total'],1)
