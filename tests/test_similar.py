import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from photos import guest, live, query, semantic, vault, native_edit
from photos.library import Library, LibraryError, digest_file
from photos.worker import process


def vector(score):
    return struct.pack('<512f', score, math.sqrt(1-score*score), *([0.0]*510))


class SimilarTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source/Similar'; self.source.mkdir(parents=True)
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.originals = {}; self.ids = []
        for i in range(8):
            path = self.source/f'Photo {i}.png'
            Image.new('RGB',(30+i,40),(200-i*10,100,80)).save(path)
            self.originals[path] = path.read_bytes(); self.ids.append(digest_file(path))
        self.library.import_folders([self.source.parent])
        for identity, score in zip(self.ids,[1.,.99,.96,.92,.899,1.,1.,.98]):
            self.library.db.execute('INSERT INTO semantic_embeddings VALUES(?,?,?,?)',(identity,semantic.MODEL,vector(score),''))
        self.library.db.commit()
        self.library.edit(self.ids[1],'archived_set','1')
        self.library.edit(self.ids[5],'hidden_set','1')
        self.library.edit(self.ids[6],'trash')
        self.library.set_setting('semantic_enabled',True)
        self.library.set_setting('semantic_processing',False)
        self.config = dict(root=str(self.library.root))

    def page(self, seed=None, **values):
        return self.library.query('similar',similar_id=self.ids[0] if seed is None else seed,**values)

    def safe(self):
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(),data)
            self.assertEqual(self.library.original(self.library.photo(digest_file(path))).read_bytes(),data)

    def test_native_image_vector_ranking_threshold_paging_and_filters(self):
        with patch.object(semantic,'infer',side_effect=AssertionError('Image similarity must use existing native indexes')):
            result = self.page(limit=2,sort='oldest')
            self.assertEqual(result['count'],4)
            self.assertEqual([p['id'] for p in result['photos']],[self.ids[1],self.ids[7]])
            self.assertEqual([p['id'] for p in self.page(offset=2)['photos']],self.ids[2:4])
            self.assertEqual(self.page(offset=4)['photos'],[])
            self.assertEqual(self.page(search='Photo 2')['count'],1)
            self.assertEqual(self.page(search='is:archived')['count'],1)
            self.assertEqual(self.page(search='type:video')['count'],0)
        self.safe()

    def test_private_missing_corrupt_or_unindexed_seed_returns_no_derivations(self):
        for identity in [self.ids[5],self.ids[6],'a'*64]:
            self.assertEqual(self.page(identity,hidden_access=True)['count'],0)
        for bad in [b'short',struct.pack('<512f',float('nan'),*([0.0]*511)),struct.pack('<512f',*([1.0]*512))]:
            self.library.db.execute('UPDATE semantic_embeddings SET embedding=? WHERE photo_id=?',(bad,self.ids[0])); self.library.db.commit()
            self.assertEqual(self.page()['photos'],[])
        self.library.db.execute('DELETE FROM semantic_embeddings WHERE photo_id=?',(self.ids[0],)); self.library.db.commit()
        self.assertEqual(self.page()['count'],0)
        self.safe()

    def test_candidates_with_wrong_models_errors_or_invalid_vectors_are_excluded(self):
        self.library.db.execute('UPDATE semantic_embeddings SET model=? WHERE photo_id=?',('obsolete',self.ids[1]))
        self.library.db.execute('UPDATE semantic_embeddings SET embedding=? WHERE photo_id=?',(b'invalid',self.ids[2]))
        self.library.db.execute('UPDATE semantic_embeddings SET embedding=? WHERE photo_id=?',(vector(.5),self.ids[3]))
        self.library.db.execute('UPDATE semantic_embeddings SET error=? WHERE photo_id=?',('unavailable',self.ids[7]))
        self.library.db.commit()
        self.assertEqual(self.page()['photos'],[])
        self.safe()

    def test_pair_privacy_applies_to_seed_and_matches(self):
        movie = self.source/'Motion.mp4'; movie.write_bytes(b'Synthetic motion')
        self.originals[movie] = movie.read_bytes()
        self.library.import_folders([self.source.parent])
        movie_id = digest_file(movie)
        live.pair(self.library,self.ids[1],movie_id)
        self.assertEqual(self.page()['count'],4)
        self.assertEqual(self.page(self.ids[1])['count'],5)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(movie_id,)); self.library.db.commit()
        self.assertEqual(self.page()['count'],3)
        self.assertEqual(self.page(self.ids[1],hidden_access=True)['photos'],[])
        self.assertEqual(self.page(movie_id)['photos'],[])
        self.safe()

    def test_disabled_machine_learning_guest_scope_and_app_lock(self):
        self.library.set_setting('semantic_enabled',False)
        self.assertEqual(self.page()['photos'],[])
        self.library.set_setting('semantic_enabled',True)
        password = 'synthetic-similarity-password'
        token = vault.authenticate(self.library,password,setup=True)
        self.addCleanup(vault.lock,self.library,token)
        guest.start(self.library,dict(ids=[self.ids[2]]))
        response = process(dict(view='similar',similar_id=self.ids[5],gallery_format=2),self.config)
        self.assertEqual([p['id'] for p in response['photos']],[self.ids[2]])
        process(dict(action='guest_exit',value=password),self.config)
        process(dict(action='app_lock_enable',value=password),self.config)
        response = process(dict(view='similar',similar_id=self.ids[0]),self.config)
        self.assertEqual(response['photos'],[])
        self.assertEqual(response['app_locked'],1)
        self.safe()

    def test_invalid_seed_and_date_jump_are_rejected_without_modification(self):
        for identity in ['', 'x'*64, self.ids[0]+'\0', "' OR 1=1--", 1]:
            with self.assertRaises(LibraryError): self.page(identity)
        with self.assertRaises(LibraryError): self.page(jump_day='2024-01-01')
        self.safe()

    def test_snapshot_prevents_seed_privacy_changes_during_ranking_and_hydration(self):
        original = query.page

        def locked(library, **request):
            writer = Library(library.root)
            try:
                writer.db.execute('PRAGMA busy_timeout=0')
                with self.assertRaisesRegex(Exception,'locked'):
                    writer.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(self.ids[0],))
            finally: writer.db.close()
            return original(library,**request)

        with patch('photos.query.page',side_effect=locked):
            self.assertEqual(self.page()['count'],4)
        self.library.edit(self.ids[0],'hidden_set','1')
        self.assertEqual(self.page()['photos'],[])
        self.assertFalse(self.library.db.in_transaction)
        self.safe()

    def test_cleanup_preserves_regular_albums_covers_and_manual_provenance(self):
        import json
        seed, removed, other = self.ids[:3]
        self.library.edit(removed,'album_add','Only this scene 日本語')
        self.library.edit_album('Only this scene 日本語','cover',removed)
        self.library.edit_album('Only this scene 日本語','archived')
        self.library.edit(seed,'album_remove','Only this scene 日本語')
        caption = self.library.photo(seed)['caption']
        result = process(dict(action='similar_cleanup',value=seed,ids=[removed],
                              view='similar',similar_id=seed),self.config)
        self.assertNotIn('error',result)
        self.assertIn('recoverable Trash',result['status'])
        self.assertTrue(self.library.photo(removed)['trashed'])
        self.assertFalse(self.library.photo(seed)['trashed'])
        self.assertFalse(self.library.photo(other)['trashed'])
        self.assertIn('Only this scene 日本語',json.loads(self.library.photo(seed)['albums']))
        self.assertNotIn('Only this scene 日本語',json.loads(self.library.photo(seed)['edits']).get('removed_albums',[]))
        self.assertEqual(self.library.album_details('Only this scene 日本語')['cover'],seed)
        self.assertTrue(self.library.album_details('Only this scene 日本語')['archived'])
        self.assertEqual(self.library.photo(seed)['caption'],caption)
        self.library.import_folders([self.source.parent])
        self.assertTrue(self.library.photo(removed)['trashed'])
        self.assertIn('Only this scene 日本語',json.loads(self.library.photo(seed)['albums']))
        self.library.edit(removed,'restore')
        self.assertIn('Only this scene 日本語',json.loads(self.library.photo(removed)['albums']))
        self.safe()

    def test_cleanup_keeps_both_live_components_and_trashes_both_discarded_components(self):
        import json
        movies = []
        for i, still in enumerate(self.ids[:2]):
            path = self.source/f'Motion {i}.mp4'; path.write_bytes(b'Synthetic motion '+bytes([i]))
            self.originals[path] = path.read_bytes()
            self.library.import_folders([self.source.parent])
            movie = digest_file(path); movies.append(movie); live.pair(self.library,still,movie)
        self.library.edit(self.ids[1],'album_add','Retain Live scene')
        self.library.edit_many([self.ids[1]],'similar_cleanup',self.ids[0])
        for identity in [self.ids[0],movies[0]]:
            self.assertFalse(self.library.photo(identity)['trashed'])
            self.assertIn('Retain Live scene',json.loads(self.library.photo(identity)['albums']))
        for identity in [self.ids[1],movies[1]]: self.assertTrue(self.library.photo(identity)['trashed'])
        self.safe()

    def test_cleanup_rejects_private_stale_unindexed_and_unrelated_selection_together(self):
        from photos.library import packed
        for ids, seed in [([self.ids[1],self.ids[4]],self.ids[0]),
                          ([self.ids[1],self.ids[5]],self.ids[0]),
                          ([self.ids[1],self.ids[6]],self.ids[0]),
                          ([self.ids[1]],self.ids[5]),
                          ([self.ids[1],self.ids[1]],self.ids[0]),
                          ([self.ids[0]],self.ids[0]),([self.ids[1]],'invalid')]:
            before = [packed(dict(p)) for p in self.library.db.execute('SELECT * FROM photos ORDER BY id')]
            with self.assertRaises(LibraryError): self.library.edit_many(ids,'similar_cleanup',seed)
            self.assertEqual(before,[packed(dict(p)) for p in self.library.db.execute('SELECT * FROM photos ORDER BY id')])
        self.library.set_setting('semantic_enabled',False)
        with self.assertRaises(LibraryError): self.library.edit_many([self.ids[1]],'similar_cleanup',self.ids[0])
        self.assertFalse(self.library.photo(self.ids[1])['trashed'])
        self.safe()

    def test_cleanup_partial_cover_or_trash_failure_rolls_back_every_change(self):
        import json
        self.library.edit(self.ids[1],'album_add','Only selected')
        self.library.edit_album('Only selected','cover',self.ids[1])
        for table, field in [('albums','cover'),('photos','trashed')]:
            self.library.db.execute(f"CREATE TRIGGER stop_cleanup BEFORE UPDATE OF {field} ON {table} BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
            self.library.db.commit()
            with self.assertRaises(LibraryError): self.library.edit_many([self.ids[1]],'similar_cleanup',self.ids[0])
            self.assertNotIn('Only selected',json.loads(self.library.photo(self.ids[0])['albums']))
            self.assertFalse(self.library.photo(self.ids[1])['trashed'])
            self.assertEqual(self.library.album_details('Only selected')['cover'],self.ids[1])
            self.library.db.execute('DROP TRIGGER stop_cleanup'); self.library.db.commit()
        self.safe()

    def test_cleanup_writer_snapshot_authorization_interruption_and_private_pair(self):
        import json, os, subprocess
        from photos.library import packed
        child = subprocess.Popen([str(native_edit.program()),str(self.library.root/'library.sqlite')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={k:v for k,v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS'}})
        try:
            child.stdin.write(packed(dict(action='similar_cleanup',value=self.ids[0],ids=[self.ids[1]])).encode()+b'\n');child.stdin.flush()
            self.assertEqual(set(json.loads(child.stdout.readline())['authorize']),set(self.ids[:2]))
            writer = Library(self.library.root)
            try:
                writer.db.execute('PRAGMA busy_timeout=0')
                with self.assertRaisesRegex(Exception,'locked'):writer.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(self.ids[0],))
            finally: writer.db.close()
        finally:
            child.kill();child.wait(timeout=5);child.stdin.close();child.stdout.close();child.stderr.close()
        self.assertFalse(self.library.photo(self.ids[1])['trashed'])
        path=self.source/'Private motion.mp4';path.write_bytes(b'Private synthetic motion')
        self.originals[path]=path.read_bytes();self.library.import_folders([self.source.parent])
        movie=digest_file(path);live.pair(self.library,self.ids[1],movie)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(movie,));self.library.db.commit()
        with self.assertRaises(LibraryError): self.library.edit_many([self.ids[1]],'similar_cleanup',self.ids[0])
        self.assertFalse(self.library.photo(self.ids[1])['trashed'])
        self.safe()

    def test_cleanup_is_unavailable_in_guest_view_or_while_app_locked(self):
        password='synthetic-cleanup-password'
        token=vault.authenticate(self.library,password,setup=True);self.addCleanup(vault.lock,self.library,token)
        guest.start(self.library,dict(ids=[self.ids[1]]))
        request=dict(action='similar_cleanup',value=self.ids[0],ids=[self.ids[1]],view='similar',similar_id=self.ids[0])
        self.assertIn('error',process(request,self.config))
        self.assertFalse(self.library.photo(self.ids[1])['trashed'])
        process(dict(action='guest_exit',value=password),self.config)
        process(dict(action='app_lock_enable',value=password),self.config)
        self.assertTrue(process(request,self.config)['app_locked'])
        self.assertFalse(self.library.photo(self.ids[1])['trashed'])
        self.safe()

    def test_library_groups_use_visible_valid_indexes_and_page_reference_photos(self):
        self.library.db.execute('UPDATE photos SET taken=1000')
        self.library.db.execute('UPDATE photos SET taken=2000 WHERE id=?',(self.ids[0],));self.library.db.commit()
        result=self.library.query('similar_groups',limit=1)
        self.assertEqual(result['count'],1)
        self.assertEqual([(p['id'],p['group_size']) for p in result['photos']],[(self.ids[0],5)])
        self.assertEqual(self.library.query('similar_groups',offset=1)['photos'],[])
        self.assertEqual(self.library.query('similar_groups',search='Photo 0')['count'],1)
        self.assertEqual(self.library.query('similar_groups',search='type:video')['count'],0)
        with self.assertRaises(LibraryError):self.library.query('similar_groups',jump_day='2024-01-01')
        self.library.set_setting('semantic_enabled',False)
        self.assertEqual(self.library.query('similar_groups')['photos'],[])
        self.assertEqual(query.page(self.library,view='similar_groups',sort='newest')['ids'],[])
        self.safe()

    def test_library_groups_separate_dissimilar_sets_and_skip_invalid_indexes(self):
        from photos.library import packed
        self.library.db.execute('UPDATE photos SET taken=1000')
        for i, identity in enumerate(self.ids):
            values=[0.0]*512;values[0 if i<3 else 1]=1.0
            self.library.db.execute('UPDATE semantic_embeddings SET embedding=? WHERE photo_id=?',(struct.pack('<512f',*values),identity))
        self.library.db.execute('UPDATE photos SET taken=2000 WHERE id=?',(self.ids[0],))
        self.library.db.execute('UPDATE photos SET taken=1900 WHERE id=?',(self.ids[3],))
        self.library.db.execute('UPDATE semantic_embeddings SET embedding=? WHERE photo_id=?',(b'broken',self.ids[7]));self.library.db.commit()
        result=self.library.query('similar_groups',limit=1)
        self.assertEqual(result['count'],2)
        self.assertEqual([(p['id'],p['group_size']) for p in result['photos']],[(self.ids[0],3)])
        self.assertEqual([(p['id'],p['group_size']) for p in self.library.query('similar_groups',offset=1)['photos']],[(self.ids[3],2)])
        self.library.edit(self.ids[3],'hidden_set','1')
        self.assertEqual(self.library.query('similar_groups')['count'],1)
        self.safe()
