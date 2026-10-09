import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from photos import people
from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos.sync import Client
from test_people import vector


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); source=self.root/'source/Family'; source.mkdir(parents=True)
        self.library=Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.ids=[]
        for i in range(18):
            path=source/f'photo{i}.png'; Image.new('RGB',(30+i,20),(i*10,90,60)).save(path); self.ids.append(digest_file(path))
        self.library.import_folders([source.parent]); self.library.set_setting('people_enabled',True); self.library.set_setting('people_processing',True)
        for i,photo_id in enumerate(self.ids):
            self.library.edit(photo_id,'taken',f'{2020+i//6}-07-{1+i%6:02d}T12:00:00Z')
            people.store(self.library,photo_id,[dict(rect=[0,0,3000,3000],embedding=vector())],self.root)
        self.person=self.library.db.execute('SELECT person_id FROM faces LIMIT 1').fetchone()[0]
        self.library.set_setting('people_processing',False);people.edit(self.library,'person_name',self.person,'Ada')
        self.config=dict(root=str(self.library.root))

    def test_memory_lane_year_eligibility_paging_and_current_year(self):
        result=self.library.query('lane',person=self.person,sort='oldest',limit=2)
        self.assertEqual(result['lane_years'],[2020,2021,2022]); self.assertEqual(result['lane_year'],2020)
        self.assertEqual(result['count'],6);self.assertEqual([p['id'] for p in result['photos']],self.ids[:2])
        self.assertGreaterEqual(result['current_year'],2022)
        later=self.library.query('lane',person=self.person,lane_year=2022,sort='oldest',offset=2,limit=2)
        self.assertEqual([p['id'] for p in later['photos']],self.ids[14:16]);self.assertEqual(later['lane_year'],2022)
        for i in self.ids[:3]:self.library.edit(i,'hidden_set','1')
        self.assertEqual(self.library.query('lane',person=self.person,hidden_access=True)['lane_years'],[2021,2022])
        for i in self.ids[6:9]:self.library.edit(i,'trash')
        empty=self.library.query('lane',person=self.person,hidden_access=True)
        self.assertFalse(empty['photos']);self.assertFalse(empty['lane_years']);self.assertEqual(empty['person_detail']['name'],'')

    def test_birthday_cutoff_ignored_disabled_and_archived_lane_are_private(self):
        people.edit(self.library,'person_birth',self.person,'2018-06-01')
        self.assertEqual(self.library.query('lane',person=self.person)['lane_years'],[2021,2022])
        self.library.edit_album('Family','archived')
        self.assertFalse(self.library.query('lane',person=self.person)['photos'])
        self.library.edit_album('Family','archived')
        people.edit(self.library,'person_ignore',self.person)
        self.assertFalse(self.library.query('lane',person=self.person)['photos'])
        people.edit(self.library,'person_ignore',self.person);self.library.set_setting('people_enabled',False)
        self.assertFalse(self.library.query('lane',person=self.person)['photos'])
        for value in ['2023-02-29','not a date','9999-01-01',[],True]:
            with self.assertRaises(LibraryError):people.edit(self.library,'person_birth',self.person,value)
        self.assertEqual(people.group(self.library,self.person)['birth'],'2018-06-01')

    def test_birth_date_encrypted_round_trip_and_old_indexes_are_accepted(self):
        people.edit(self.library,'person_birth',self.person,'2018-06-01')
        sender=Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64))
        peer=Library(self.root/'peer');self.addCleanup(peer.db.close)
        receiver=Client(peer,dict(node='http://127.0.0.1:18080',node_id='a'*64),sender.identity_path)
        record=sender.record(self.library.photo(self.ids[0]),manifest={});self.assertNotIn('2018-06-01',json.dumps(record))
        receiver.accept(record);peer.db.commit();self.assertEqual(people.group(peer,self.person)['birth'],'2018-06-01')
        old=people.export_index(self.library,self.ids[1]);old['faces'][0]['person'].pop('birth')
        people.accept_index(peer,self.ids[1],old);peer.db.commit();self.assertEqual(people.group(peer,self.person)['birth'],'2018-06-01')
        old['faces'][0]['person']['updated'] += 1
        old['faces'][0]['person']['name'] = 'Ada Lovelace'
        people.accept_index(peer,self.ids[1],old);peer.db.commit()
        self.assertEqual(people.group(peer,self.person)['birth'],'2018-06-01')
        self.assertEqual(people.group(peer,self.person)['name'],'Ada Lovelace')
        old['faces'][0]['person'].update(birth='',updated=old['faces'][0]['person']['updated']+1)
        people.accept_index(peer,self.ids[1],old);peer.db.commit()
        self.assertEqual(people.group(peer,self.person)['birth'],'')

    def test_birthdays_before_epoch_and_leap_day_cutoff(self):
        for i,photo_id in enumerate(self.ids):
            timestamp = datetime(1960+i//6,7,1+i%6,12,tzinfo=timezone.utc).timestamp()
            self.library.db.execute('UPDATE photos SET taken=? WHERE id=?',(timestamp,photo_id))
        self.library.db.commit()
        people.edit(self.library,'person_birth',self.person,'1958-06-01')
        self.assertEqual(self.library.query('lane',person=self.person)['lane_years'],[1961,1962])
        people.edit(self.library,'person_birth',self.person,'1960-02-29')
        self.assertFalse(self.library.query('lane',person=self.person)['photos'])
        for i,photo_id in enumerate(self.ids):
            timestamp = datetime(1963+i//6,2,28,12,tzinfo=timezone.utc).timestamp()
            self.library.db.execute('UPDATE photos SET taken=? WHERE id=?',(timestamp,photo_id))
        self.library.db.commit()
        self.assertEqual(self.library.query('lane',person=self.person)['lane_years'],[1963,1964,1965])

    def test_jump_to_date_keeps_filters_sort_and_paging_without_changing_files(self):
        originals={i:self.library.original(self.library.photo(i)).read_bytes() for i in self.ids}
        newest=self.library.query(jump_day='2021-07-03',limit=2)
        self.assertEqual(newest['jump_offset'],9);self.assertEqual([p['id'] for p in newest['photos']],[self.ids[8],self.ids[7]])
        self.assertEqual(self.library.query(offset=newest['jump_offset']+2,limit=2)['photos'][0]['id'],self.ids[6])
        oldest=self.library.query(jump_day='2021-07-03',sort='oldest',limit=2,album='Family')
        self.assertEqual(oldest['jump_offset'],8);self.assertEqual([p['id'] for p in oldest['photos']],[self.ids[8],self.ids[9]])
        self.library.edit(self.ids[8],'hidden_set','1')
        result=process(dict(action='jump_date',value='2021-07-03',search='person:Ada'),self.config)
        self.assertTrue(result['jumped']);self.assertNotIn(self.ids[8],[p['id'] for p in result['photos']])
        self.assertEqual(self.library.query(jump_day='1990-01-01')['photos'][0]['id'],self.ids[0])
        self.assertEqual(self.library.query(jump_day='2090-01-01')['jump_offset'],0)
        self.assertFalse(self.library.query(search='does not match',jump_day='2021-01-01')['photos'])
        for value in ['',True,'2021-02-29','2021-01-01;DROP TABLE photos']:
            with self.assertRaises(LibraryError):process(dict(action='jump_date',value=value),self.config)
        for i,blob in originals.items():self.assertEqual(self.library.original(self.library.photo(i)).read_bytes(),blob)
