import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos import auto_albums, people
from photos.library import Library, LibraryError, digest_file
from photos.sync import Client
from owner_fixture import ACCOUNT
from photos.worker import process
from photos.export import Export
from test_people import vector


class AutoAlbumsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name); self.source=self.root/'source/Family'; self.source.mkdir(parents=True)
        self.ids=[]
        for i in range(5):
            path=self.source/f'photo{i}.png'; Image.new('RGB',(30+i,20),(40*i,90,60)).save(path); self.ids.append(digest_file(path))
        self.library=Library(self.root/'library');self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source.parent]); self.library.set_setting('people_enabled',True);self.library.set_setting('people_processing',True)
        for photo_id in self.ids[:2]:
            people.store(self.library,photo_id,[dict(rect=[0,0,3000,3000],embedding=vector())],self.root)
        self.person=self.library.db.execute('SELECT person_id FROM faces LIMIT 1').fetchone()[0]
        self.library.set_setting('people_processing',False);people.edit(self.library,'person_name',self.person,'Ada')
        self.album=self.library.edit_album('','create','Ada album')

    def members(self):
        return {p['id'] for p in self.library.query(album=self.album)['photos']}

    def test_existing_new_matches_pause_and_manual_membership_are_preserved(self):
        originals={i:self.library.original(self.library.photo(i)).read_bytes() for i in self.ids}
        self.library.edit(self.ids[0],'album_add',self.album)
        auto_albums.configure(self.library,self.album,[self.person])
        self.assertEqual(self.members(),set(self.ids[:2]))
        self.library.set_setting('people_processing',True)
        people.store(self.library,self.ids[2],[dict(rect=[0,0,3000,3000],embedding=vector())],self.root)
        self.assertEqual(self.members(),set(self.ids[:3]))
        self.library.set_setting('people_enabled',False)
        # Stored indexes are retained while disabled, and apply pauses.
        self.library.db.execute('INSERT INTO faces SELECT ?,?,person_id,rect,embedding,excluded FROM faces LIMIT 1',('c'*64,self.ids[3]));self.library.db.commit()
        auto_albums.apply(self.library);self.assertEqual(self.members(),set(self.ids[:3]))
        self.library.set_setting('people_enabled',True);auto_albums.apply(self.library)
        self.assertEqual(self.members(),set(self.ids[:4]))
        auto_albums.configure(self.library,self.album,[],remove=True)
        self.assertEqual(self.members(),{self.ids[0]})
        for i,data in originals.items():self.assertEqual(self.library.original(self.library.photo(i)).read_bytes(),data)

    def test_manual_removal_and_readdition_rename_and_stop(self):
        auto_albums.configure(self.library,self.album,[self.person])
        self.library.edit(self.ids[0],'album_remove',self.album)
        self.library.import_folders([self.source.parent]);auto_albums.apply(self.library)
        self.assertEqual(self.members(),{self.ids[1]})
        self.library.rename_album(self.album,'Renamed');self.album='Renamed'
        auto_albums.configure(self.library,self.album,[],remove=False)
        self.assertEqual(self.members(),{self.ids[1]})
        auto_albums.configure(self.library,self.album,[self.person])
        self.library.edit(self.ids[1],'album_add',self.album)
        auto_albums.configure(self.library,self.album,[],remove=True)
        self.assertEqual(self.members(),{self.ids[1]})

    def test_hidden_trash_ignored_and_invalid_configuration_are_atomic(self):
        self.library.edit(self.ids[0],'hidden_set','1');self.library.edit(self.ids[1],'trash')
        with self.assertRaises(LibraryError):auto_albums.configure(self.library,self.album,[self.person])
        self.assertFalse(self.members());self.assertEqual(json.loads(self.library.album_details(self.album)['auto_people']),[])
        self.library.edit(self.ids[1],'restore');auto_albums.configure(self.library,self.album,[self.person]);self.assertEqual(self.members(),{self.ids[1]})
        people.edit(self.library,'person_ignore',self.person)
        self.library.edit(self.ids[0],'hidden_set','0');auto_albums.apply(self.library)
        self.assertEqual(self.members(),{self.ids[1]})
        for value in [None,['bad'],[True],['a'*64]*65]:
            with self.assertRaises(LibraryError):auto_albums.configure(self.library,self.album,value,remove=True)
            self.assertEqual(self.members(),{self.ids[1]})
        smart=self.library.edit_album('','smart_create','Smart',search='is:favorite')
        with self.assertRaises(LibraryError):auto_albums.configure(self.library,smart,[])

    def test_stop_cannot_mutate_a_hidden_original_or_private_live_component(self):
        auto_albums.configure(self.library,self.album,[self.person])
        self.library.edit(self.ids[0],'hidden_set','1')
        before=self.library.photo(self.ids[0])['edits']
        auto_albums.configure(self.library,self.album,[],remove=True)
        self.assertEqual(self.library.photo(self.ids[0])['edits'],before)
        self.assertIn(self.album,json.loads(self.library.photo(self.ids[0])['albums']))
        self.assertNotIn(self.album,json.loads(self.library.photo(self.ids[1])['albums']))

    def test_rule_and_provenance_encrypted_restore_and_folder_export(self):
        auto_albums.configure(self.library,self.album,[self.person])
        sender=Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64),account=ACCOUNT)
        peer=Library(self.root/'peer');self.addCleanup(peer.db.close)
        receiver=Client(peer,dict(node='http://127.0.0.1:18080',node_id='a'*64),sender.identity_path,account=ACCOUNT)
        record=sender.album_record(self.library.album_details(self.album));self.assertNotIn(self.person,json.dumps(record))
        receiver.accept_album(record)
        for i in self.ids[:2]:receiver.accept(sender.record(self.library.photo(i), dict(blobs=[])))
        peer.db.commit()
        self.assertEqual(json.loads(peer.album_details(self.album)['auto_people']),[self.person])
        auto_albums.configure(peer,self.album,[],remove=True)
        self.assertTrue(all(self.album not in json.loads(peer.photo(i)['albums']) for i in self.ids[:2]))
        destination=self.root/'export'
        with Export(self.library,str(destination),[self.source.parent]) as export:
            export.run()
        imported=Library(self.root/'imported');self.addCleanup(imported.db.close)
        imported.import_folders([destination])
        self.assertEqual(json.loads(imported.album_details(self.album)['auto_people']),[self.person])
        auto_albums.configure(imported,self.album,[],remove=True)
        self.assertTrue(all(self.album not in json.loads(imported.photo(i)['albums']) for i in self.ids[:2]))

    def test_replacing_a_rule_keeps_photos_of_a_newly_selected_person(self):
        auto_albums.configure(self.library,self.album,[self.person])
        other='d'*64
        self.library.db.execute('INSERT INTO people(id,name,ignored,updated) VALUES(?,?,?,?)',(other,'Grace',0,1))
        self.library.db.execute('INSERT INTO faces SELECT ?,photo_id,?,rect,embedding,excluded FROM faces WHERE photo_id=? LIMIT 1',
                                ('e'*64,other,self.ids[0]))
        self.library.db.commit()
        auto_albums.configure(self.library,self.album,[other],remove=True)
        self.assertEqual(self.members(),{self.ids[0]})
        album_id=self.library.album_details(self.album)['id']
        self.assertEqual(json.loads(self.library.photo(self.ids[0])['edits'])['auto_albums'][album_id],[other])

    def test_picker_preserves_saved_rules_and_pause_has_no_private_candidates(self):
        auto_albums.configure(self.library,self.album,[self.person])
        config=dict(root=str(self.library.root))
        result=process(dict(action='auto_people_begin',album=self.album),config)
        self.assertEqual(result['view'],'auto_people');self.assertEqual(result['auto_selection'],[self.person])
        self.assertEqual(result['albums'][0]['name'],'Ada');self.assertFalse(result['photos'])
        self.library.set_setting('people_enabled',False)
        self.assertFalse(process(dict(action='query',view='auto_people',album=self.album),config)['albums'])
        result=process(dict(action='album_auto_people',album=self.album,ids=[]),config)
        self.assertEqual(result['view'],'photos');self.assertEqual(self.members(),set(self.ids[:2]))

    def test_merged_people_keep_album_rules_birth_dates_and_cleanup_provenance(self):
        auto_albums.configure(self.library,self.album,[self.person])
        people.edit(self.library,'person_birth',self.person,'1990-06-15')
        other='f'*64
        self.library.db.execute('INSERT INTO people(id,name,ignored,updated) VALUES(?,?,?,?)',(other,'Grace',0,1))
        self.library.db.execute('INSERT INTO faces SELECT ?,?,?,rect,embedding,excluded FROM faces LIMIT 1',
                                ('b'*64,self.ids[2],other))
        self.library.db.commit()
        self.assertEqual(people.edit(self.library,'person_name',self.person,'Grace'),other)
        self.assertEqual(json.loads(self.library.album_details(self.album)['auto_people']),[other])
        self.assertEqual(people.group(self.library,other)['birth'],'1990-06-15')
        self.assertEqual(self.members(),set(self.ids[:3]))
        self.library.rename_album(self.album,'Together');self.album='Together'
        auto_albums.configure(self.library,self.album,[],remove=True)
        self.assertFalse(self.members())
