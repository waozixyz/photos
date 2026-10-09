import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from photos import guest, live, vault, query
from photos.library import Library, LibraryError, digest_file, packed
from photos.worker import process


class GuestTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root/'source/Family'; self.source.mkdir(parents=True)
        self.ids = []; self.originals = {}
        for i in range(72):
            path = self.source/f'Photo {i}.png'; Image.new('RGB', (30+i, 40), (i*3, 80, 120)).save(path)
            self.ids.append(digest_file(path)); self.originals[path] = path.read_bytes()
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source.parent]); self.config = dict(root=str(self.library.root))
        self.password = 'guest-test-password'
        self.owner_token = vault.authenticate(self.library, self.password, setup=True)
        self.addCleanup(vault.failures.pop, str(self.library.root), None)
        self.addCleanup(vault.lock, self.library, self.owner_token)

    def request(self, **values):
        return process(dict(gallery_format=2, **values), self.config)

    def start(self, ids=None, **values):
        return self.request(action='guest_start', ids=self.ids[:2] if ids is None else ids, **values)

    def originals_safe(self):
        for path, data in self.originals.items(): self.assertEqual(path.read_bytes(), data)

    def test_selection_is_read_only_paged_and_persistent_across_clients(self):
        before = [tuple(row) for row in self.library.db.execute('SELECT * FROM photos ORDER BY id')]
        result = self.start(self.ids[:65]); self.assertEqual(result['guest_active'], 1)
        self.assertEqual(result['count'], 65); self.assertEqual(result['total'], 65)
        self.assertEqual([p['id'] for p in result['photos']], self.ids[:60])
        self.assertFalse(result['albums']); self.assertEqual(result['pending'], 0)
        self.assertFalse(vault.unlocked(self.library, self.owner_token))
        self.assertEqual(guest.path(self.library).stat().st_mode & 0o777, 0o600)
        page = self.request(action='query', offset=60, view='hidden', vault_token=self.owner_token)
        self.assertEqual([p['id'] for p in page['photos']], self.ids[60:65])
        self.assertFalse(page['vault_unlocked']); self.assertNotIn('album_detail', page)
        opened = self.request(action='open', id=self.ids[64], offset=60)
        self.assertEqual(opened['detail'], dict(id=self.ids[64]))
        self.assertEqual([tuple(row) for row in self.library.db.execute('SELECT * FROM photos ORDER BY id')], before)
        self.originals_safe()

    def test_all_owner_actions_and_arbitrary_ids_are_denied_before_dispatch(self):
        self.start()
        for action in ['favorite', 'hidden', 'trash', 'restore', 'caption', 'export', 'export_start', 'import',
                       'sync', 'image_begin', 'video_save', 'collage_save', 'detect_text', 'detect_qr',
                       'person_tag', 'people_enable', 'album_create', 'vault_unlock', 'vault_change', 'guest_start']:
            result = self.request(action=action, id=self.ids[0], value=self.password,
                                  vault_token=self.owner_token, guest_bypass=True)
            self.assertEqual(result['guest_active'], 1); self.assertIn('error', result)
            self.assertFalse(result['photos']); self.assertFalse(result['albums'])
        for identity in [self.ids[3], '../outside', 'a'*64+'\0', None]:
            for action in ['open', 'playback_check']:
                result = self.request(action=action, id=identity)
                self.assertIn('error', result); self.assertNotIn('playback_path', result)
                self.assertFalse(result['photos'])
        self.assertEqual(self.library.query()['total'], 72); self.originals_safe()

    def test_password_exit_is_required_and_retains_hidden_lock_and_keys(self):
        self.start()
        failed = self.request(action='guest_exit', value='wrong-password', vault_token=self.owner_token)
        self.assertEqual(failed['guest_active'], 1); self.assertIn('Incorrect password', failed['error'])
        self.assertTrue(guest.path(self.library).is_file())
        result = self.request(action='guest_exit', value=self.password)
        self.assertEqual(result['guest_active'], 0); self.assertEqual(result['guest_exit_authorized'], 1)
        self.assertFalse(result['vault_unlocked']); self.assertNotIn('vault_token', result)
        self.assertEqual(result['total'], 72); self.assertFalse(guest.path(self.library).exists())
        self.assertEqual(self.request(action='query')['guest_active'], 0); self.originals_safe()

    def test_hidden_trash_and_private_live_components_are_never_guest_inputs(self):
        self.library.edit(self.ids[0], 'hidden_set', '1'); self.library.edit(self.ids[1], 'trash')
        for ids in [[self.ids[0]], [self.ids[2], self.ids[1]], [], [True], ['a'*64+'\0'], self.ids[:1]*1001]:
            with self.assertRaises(LibraryError): self.start(ids)
            self.assertIsNone(guest.scope(self.library))
        movie = self.source/'Motion.mp4'; movie.write_bytes(b'Synthetic motion')
        self.library.import_folders([self.source.parent]); movie_id = digest_file(movie)
        live.pair(self.library, self.ids[2], movie_id)
        with self.assertRaises(LibraryError): self.start([movie_id])
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?', (movie_id,)); self.library.db.commit()
        with self.assertRaises(LibraryError): self.start([self.ids[2]])
        self.originals_safe()

    def test_later_privacy_changes_revoke_guest_paths_and_counts(self):
        self.start()
        self.library.edit(self.ids[0], 'hidden_set', '1')
        result = self.request(action='query'); self.assertEqual(result['count'], 1)
        self.assertEqual([p['id'] for p in result['photos']], [self.ids[1]])
        self.assertIn('error', self.request(action='open', id=self.ids[0]))
        self.library.edit(self.ids[1], 'trash')
        result = self.request(action='query'); self.assertFalse(result['photos']); self.assertEqual(result['count'], 0)
        self.assertEqual(result['guest_active'], 1); self.originals_safe()

    def test_album_identity_and_frozen_smart_rule_keep_scope_after_changes(self):
        self.library.edit_album('', 'smart_create', 'Favorites only', search='is:favorite')
        self.library.edit(self.ids[1], 'favorite_set', '1'); self.library.edit(self.ids[2], 'favorite_set', '1')
        result = self.request(action='guest_start', value='album', album='Favorites only')
        self.assertEqual({p['id'] for p in result['photos']}, set(self.ids[1:3]))
        self.assertEqual(self.request(action='open', id=self.ids[2])['detail'], dict(id=self.ids[2]))
        self.assertIn('error', self.request(action='open', id=self.ids[3]))
        self.library.rename_album('Favorites only', 'Renamed')
        self.library.edit_album('Renamed', 'filter', 'type:photo')
        self.assertEqual(self.request(action='query')['count'], 2)
        self.assertIn('error', self.request(action='open', id=self.ids[3]))
        self.library.edit(self.ids[1], 'favorite_set', '0')
        self.assertIn('error', self.request(action='open', id=self.ids[1]))
        self.assertEqual(self.request(action='query')['count'], 1)
        self.originals_safe()

    def test_regular_album_supports_video_and_membership_revocation(self):
        movie = self.source/'Video.mp4'; movie.write_bytes(b'Synthetic video')
        self.library.import_folders([self.source.parent]); movie_id = digest_file(movie)
        self.request(action='guest_start', value='album', album='Family')
        self.assertEqual(self.request(action='open', id=movie_id)['detail'], dict(id=movie_id))
        result = self.request(action='playback_check', id=movie_id)
        self.assertEqual(result['guest_active'], 1); self.assertEqual(result['playback_id'], movie_id)
        self.library.edit(movie_id, 'album_remove', 'Family')
        result = self.request(action='playback_check', id=movie_id)
        self.assertIn('error', result); self.assertNotIn('playback_path', result)

    def test_malformed_or_symlink_scope_fails_closed_and_authenticated_exit_recovers(self):
        for raw in ['{', packed(dict(version=1, kind='selection', ids=[])),
                    packed(dict(version=1, kind='album', id='a'*64, filter=1))]:
            guest.path(self.library).write_text(raw)
            result = self.request(action='query'); self.assertEqual(result['guest_active'], 1)
            self.assertFalse(result['photos']); self.assertEqual(result['total'], 0)
            self.assertIn('error', self.request(action='caption', id=self.ids[0], value='bypass'))
            self.assertEqual(self.request(action='guest_exit', value=self.password)['guest_active'], 0)
        outside = self.root/'outside.json'; outside.write_text('Private unrelated content')
        guest.path(self.library).symlink_to(outside)
        self.assertFalse(self.request(action='query')['photos'])
        self.request(action='guest_exit', value=self.password)
        self.assertEqual(outside.read_text(), 'Private unrelated content'); self.originals_safe()

    def test_native_exact_photo_predicate_rejects_invalid_ids_and_private_matches(self):
        base = dict(view='photos', sort='newest', hidden_access=0, album='Family')
        self.assertEqual(query.page(self.library, **base, photo_id=self.ids[4])['ids'], [self.ids[4]])
        for identity in ['', '../outside', 'a'*64+'\0', False]:
            with self.assertRaises(LibraryError): query.page(self.library, **base, photo_id=identity)
        self.library.edit(self.ids[4], 'hidden_set', '1')
        self.assertFalse(query.page(self.library, **base, photo_id=self.ids[4])['ids'])

    def test_slow_decode_rechecks_privacy_before_returning_any_guest_path(self):
        self.start()
        def hide_after_decode(library, photo):
            self.library.edit(photo['id'], 'hidden_set', '1')
            return self.library.original(photo)
        with patch.object(guest.decoding, 'supported', return_value=True), \
             patch.object(guest.decoding, 'display', hide_after_decode):
            result = self.request(action='open', id=self.ids[0])
        self.assertIn('error', result); self.assertFalse(result['photos'])
        self.assertNotIn(str(self.library.original(self.library.photo(self.ids[0]))), packed(result))
        self.assertEqual(result['guest_active'], 1); self.originals_safe()

    def test_start_without_password_and_failed_publication_do_not_change_originals(self):
        verifier = self.library.root/'private/hidden-password.json'
        saved = verifier.read_bytes(); verifier.unlink()
        with self.assertRaisesRegex(LibraryError, 'Set up App lock or a Hidden password'): self.start()
        self.assertIsNone(guest.scope(self.library)); verifier.write_bytes(saved)
        with patch.object(guest, 'atomic', side_effect=OSError('fixture')):
            with self.assertRaises(OSError): self.start()
        self.assertIsNone(guest.scope(self.library))
        self.assertFalse(self.library.db.in_transaction); self.originals_safe()

    def test_native_scope_validates_every_identity_and_rejects_malformed_rules(self):
        good = dict(version=1, kind='selection', ids=self.ids[:2])
        malformed = [None, False, {}, {**good, 'version': '1'}, {**good, 'version': True},
                     {**good, 'kind': 'selection\0ignored'}, {**good, 'ids': []},
                     {**good, 'ids': self.ids[:1]*2}, {**good, 'ids': self.ids[:1]*1001},
                     {**good, 'ids': [True]}, {**good, 'ids': ['a'*63]},
                     {**good, 'ids': ['a'*64+'\0']}, {**good, 'ids': ['../outside']},
                     dict(version=1, kind='album', id='a'*64, filter=None),
                     dict(version=1, kind='album', id='a'*64, filter='a'*513),
                     dict(version=1, kind='album', id='a'*64, filter='type:photo\0type:video')]
        before = [tuple(row) for row in self.library.db.execute('SELECT * FROM photos ORDER BY id')]
        for value in malformed:
            with self.subTest(value=str(value)[:100]), self.assertRaises(LibraryError):
                query.page(self.library, guest_scope=value)
        self.assertEqual([tuple(row) for row in self.library.db.execute('SELECT * FROM photos ORDER BY id')], before)
        self.originals_safe()

    def test_native_thousand_identity_scope_keeps_order_paging_and_no_library_table(self):
        import hashlib
        ids = list(reversed(self.ids)) + [hashlib.sha256(f'absent {i}'.encode()).hexdigest() for i in range(928)]
        value = dict(version=1, kind='selection', ids=ids)
        self.assertGreater(len(packed(dict(guest_scope=value)).encode()), 65536)
        first = query.page(self.library, guest_scope=value, limit=60)
        second = query.page(self.library, guest_scope=value, offset=60, limit=60)
        self.assertEqual(first['count'], 72); self.assertEqual(first['ids'], ids[:60])
        self.assertEqual(second['count'], 72); self.assertEqual(second['ids'], ids[60:72])
        self.assertIsNone(self.library.db.execute("SELECT name FROM sqlite_master WHERE name='guest_selection'").fetchone())
        self.assertFalse(query.page(self.library, guest_scope=value, photo_id=ids[-1])['ids'])
        self.originals_safe()

    def test_native_guest_scope_overrides_owner_parameters_and_rechecks_live_privacy(self):
        value = dict(version=1, kind='selection', ids=self.ids[:2])
        result = query.page(self.library, guest_scope=value, view='hidden', hidden_access=1,
                            album='Other', search='is:favorite', filter='type:video', sort='oldest',
                            slideshow=1, slide_albums=[dict(name='Other', filter='')],
                            jump_day='2020-01-01')
        self.assertEqual(result['ids'], self.ids[:2]); self.assertEqual(result['count'], 2)
        self.assertNotIn('jump_offset', result); self.assertNotIn('slide_offset', result)
        self.assertFalse(query.page(self.library, guest_scope=value, photo_id=self.ids[3])['ids'])
        movie = self.source/'Privacy.mp4'; movie.write_bytes(b'Synthetic paired video')
        self.library.import_folders([self.source.parent]); movie_id = digest_file(movie)
        live.pair(self.library, self.ids[0], movie_id)
        self.assertEqual(query.page(self.library, guest_scope=value)['count'], 2)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?', (movie_id,)); self.library.db.commit()
        self.assertEqual(query.page(self.library, guest_scope=value)['ids'], self.ids[1:2])
        self.assertFalse(query.page(self.library, guest_scope=value, photo_id=self.ids[0])['ids'])
        self.originals_safe()
