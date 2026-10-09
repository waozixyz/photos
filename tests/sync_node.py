"""Round-trip real signed requests against an isolated Daochi node."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from photos.library import Library, packed, digest_file
from photos.sync import Client
import photos.sync

# Exercise actual signed continuation requests even with this tiny fixture.
photos.sync.RECORD_PAGE = 1

repo = Path(__file__).resolve().parents[1]
node_binary = os.environ.get('PHOTOS_DAOCHI') or shutil.which('daochi')
if not node_binary or not Path(node_binary).is_file():
    raise SystemExit('Set PHOTOS_DAOCHI to the Daochi server executable for this isolated test')
with tempfile.TemporaryDirectory(dir=repo / 'build', prefix='sync-test-') as folder:
    folder = Path(folder)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('DAOCHI_') or key in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS'}:
            del env[key]
    env.update(DAOCHI_ADDR=f'127.0.0.1:{port}', DAOCHI_BASE_URL=origin, DAOCHI_DB=str(folder/'node.sqlite'),
               DAOCHI_ADMIN_TOKEN='fixture-admin', DAOCHI_KNOWN_NODES='', DAOCHI_LAN_DISCOVERY='0',
               DAOCHI_NODE_SYNC_INTERVAL_SECONDS='0', DAOCHI_TOKEN_SECRET_HEX='42'*32)
    log = (folder / 'node.log').open('w')
    process = subprocess.Popen([str(Path(node_binary).resolve())], env=env, stdout=log, stderr=log)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        node = None
        for _ in range(100):
            try:
                node = json.load(opener.open(origin+'/api/v1/node', timeout=1))
                break
            except Exception:
                if process.poll() is not None:
                    raise AssertionError((folder/'node.log').read_text())
                time.sleep(.05)
        assert node
        registration = dict(app_id='photos', display_name='Photos', status='active',
                            collections=[dict(collection_prefix='private.photos.v1.records.*', visibility='private')])
        request = urllib.request.Request(origin+'/api/v1/apps', method='POST', data=packed(registration).encode(),
                                         headers={'X-Daochi-Admin':'fixture-admin','Content-Type':'application/json'})
        opener.open(request).close()
        source = folder/'export/Fixture'
        source.mkdir(parents=True)
        Image.new('RGB',(500,300),'#4c9180').save(source/'fixture.jpg')
        Image.new('RGB',(300,500),'#ce9157').save(source/'second.jpg')
        imported_metadata = {'unknownCameraFields': {'empty': None, 'integer': 10**100,
                             'values': [True, False, '日本語\n\0']}}
        (source/'fixture.jpg.json').write_text(packed(imported_metadata))
        first = Library(folder/'desktop')
        first.import_folders([source.parent])
        assert json.loads(first.photo(digest_file(source/'fixture.jpg'))['metadata']) == imported_metadata
        assert first.db.execute('SELECT mtime FROM sources WHERE path=?',
                               (str(source/'fixture.jpg'),)).fetchone()[0] == (source/'fixture.jpg').stat().st_mtime_ns
        config = dict(node=origin, node_id=node['node_id'])
        client = Client(first, config)
        assert client.sync() == 0
        photo_id = digest_file(source/'fixture.jpg')
        from photos import semantic
        import struct
        embedding = struct.pack('<512f', 1.0, *([0.0] * 511))
        first.db.execute('INSERT INTO semantic_embeddings VALUES(?,?,?,?)', (photo_id, semantic.MODEL, embedding, ''))
        first.db.commit()
        first.edit(photo_id, 'favorite_set', '0')
        assert client.sync() == 0
        second = Library(folder/'thinkpad')
        identity = second.root/'private/identity.json'
        identity.parent.mkdir(mode=0o700)
        shutil.copyfile(client.identity_path, identity)
        os.chmod(identity,0o600)
        peer = Client(second, config)
        assert peer.sync() == 0
        assert second.query()['total'] == 2
        assert json.loads(second.photo(photo_id)['metadata']) == imported_metadata
        assert semantic.encrypted_index(second, photo_id) == semantic.encrypted_index(first, photo_id)
        assert not second.setting('semantic_enabled', False)
        original = Path(peer.materialize(photo_id))
        assert original.read_bytes() == (source/'fixture.jpg').read_bytes()
        second.edit(photo_id,'favorite')
        second.edit(photo_id,'album_add','New local album')
        assert peer.sync() == 0
        client.sync()
        assert first.photo(photo_id)['favorite'] == 1
        assert 'New local album' in json.loads(first.photo(photo_id)['albums'])
        second.edit_many([photo_id], 'taken', '2024-06-15 14:30:00+02:00')
        second.rename_album('Fixture', 'Renamed fixture')
        assert peer.sync() == 0
        client.sync()
        assert first.details(photo_id)['taken'] == '2024-06-15T12:30:00+00:00'
        assert 'Renamed fixture' in json.loads(first.photo(photo_id)['albums'])
        assert json.loads(first.photo(photo_id)['edits'])['removed_albums'] == ['Fixture']
        second.edit(photo_id, 'archived_set', '1'); peer.sync(); client.sync()
        assert first.query('archive')['count'] == 1
        assert first.query()['count'] == 1
        assert first.query(search='fixture')['count'] == 2
        assert first.query(album='Renamed fixture')['count'] == 2
        second.edit(photo_id, 'archived_set', '0'); peer.sync(); client.sync()
        second.edit_album('', 'create', 'Empty album')
        second.edit_album('Renamed fixture', 'description', 'A synchronized description')
        second.edit_album('Renamed fixture', 'pinned')
        second.edit_album('Renamed fixture', 'cover', photo_id)
        second.edit_album('Renamed fixture', 'archived')
        assert peer.sync() == 0; assert client.sync() == 0
        assert first.album_details('Empty album')['name'] == 'Empty album'
        details = first.album_details('Renamed fixture')
        assert details['description'] == 'A synchronized description' and details['pinned'] == 1 and details['cover'] == photo_id
        assert first.query()['count'] == 0 and first.query('archive')['count'] == 2
        second.edit_album('Renamed fixture', 'archived')
        second.rename_album('Empty album', 'Empty renamed')
        peer.sync(); client.sync()
        assert first.album_details('Empty renamed')['name'] == 'Empty renamed'
        second.edit_album('Empty renamed', 'delete_empty'); peer.sync(); client.sync()
        assert 'Empty renamed' not in [a['name'] for a in first.album_list('all')]
        second.edit(photo_id,'trash'); peer.sync(); client.sync()
        assert first.query('trash')['count'] == 1
        second.edit(photo_id,'restore'); peer.sync(); client.sync()
        assert first.query()['count'] == 2
        empty = source / 'empty.jpg'
        empty.write_bytes(b'')
        first.import_folders([source.parent]); client.sync(); peer.sync()
        empty_id = digest_file(empty)
        assert Path(peer.materialize(empty_id)).read_bytes() == b''
        from photos import editing
        second.edit(photo_id, 'location', '-25.3, -57.6')
        copy_id = editing.save(second, photo_id, [90, 1, 0, 120, 100, 80, 1])
        assert peer.sync() == 0; assert client.sync() == 0
        assert first.details(photo_id)['location'] == '-25.3, -57.6'
        assert first.details(copy_id)['location'] == '-25.3, -57.6'
        mapped = first.query('map', album='Renamed fixture')
        assert mapped['count'] == 2 and sum(marker['count'] for marker in mapped['markers']) == 2
        assert json.loads(first.photo(copy_id)['metadata'])['photosEdit']['parent'] == photo_id
        restored = Path(client.materialize(copy_id))
        assert digest_file(restored) == copy_id
        assert restored.read_bytes() == second.original(second.photo(copy_id)).read_bytes()
        with Image.open(restored) as copy:
            assert copy.size == (300, 300)
        assert (source/'fixture.jpg').read_bytes() == original.read_bytes()
        second.edit(photo_id, 'location', ''); peer.sync(); client.sync()
        assert first.details(photo_id)['location'] == ''
        assert first.query('map', album='Renamed fixture')['count'] == 1
        from photos import places
        places.create(second, json.dumps(['Asuncion', '-25.3, -57.6, 2']))
        assert peer.sync() == 0 and client.sync() == 0
        assert first.album_details('Asuncion')['filter'] == 'near:-25.3,-57.6,2'
        assert first.query(search='place:Asuncion')['count'] == 1
        places.update(first, 'Asuncion', '-25.3, -57.6, 3')
        assert client.sync() == 0 and peer.sync() == 0
        assert second.album_details('Asuncion')['filter'] == 'near:-25.3,-57.6,3'
        second.rename_album('Asuncion', 'Our place'); peer.sync(); client.sync()
        assert first.query(search='place:"Our place"')['count'] == 1
        first.edit_album('Our place', 'delete_empty'); client.sync(); peer.sync()
        assert not second.album_list('places')
        first.edit_album('', 'smart_create', 'Dynamic favorites', search='is:favorite year:2024')
        assert client.sync() == 0; assert peer.sync() == 0
        assert second.album_details('Dynamic favorites')['filter']=='is:favorite year:2024'
        assert second.query(album='Dynamic favorites')['count']==2
        second.edit_album('Dynamic favorites','filter','type:photo has:location');peer.sync();client.sync()
        assert first.album_details('Dynamic favorites')['filter']=='type:photo has:location'
        assert first.query(album='Dynamic favorites')['count']==1
        first.edit_album('Dynamic favorites','delete_empty');client.sync();peer.sync()
        assert 'Dynamic favorites' not in [a['name'] for a in second.album_list()]
        from photos import video
        sys.path.insert(0,str(repo/'tests'))
        from test_video import fixture
        movie = folder/'videos/Trip/Video.mp4'; fixture(movie)
        second.import_folders([movie.parents[1]])
        movie_id=digest_file(movie)
        second.edit(movie_id,'location','1, 2')
        edited_id=video.save(second,movie_id,[1000,3000,90,6,2500,2500,5000,5000])
        assert peer.sync()==0 and client.sync()==0
        restored_video=Path(client.materialize(edited_id))
        assert digest_file(restored_video)==edited_id
        assert restored_video.read_bytes()==second.original(second.photo(edited_id)).read_bytes()
        info=video.probe(restored_video)
        assert (info['width'],info['height'])==(40,60) and info['audio']
        assert first.details(edited_id)['location']=='1.0, 2.0'
        assert first.photo(edited_id)['albums']==second.photo(edited_id)['albums']
        assert json.loads(first.photo(edited_id)['metadata'])['photosVideoEdit']['parent']==movie_id
        assert json.loads(first.photo(edited_id)['metadata'])['photosVideoEdit']['adjustments']==[1000,3000,90,6,2500,2500,5000,5000]
        from codec_fixtures import heic, dng
        from test_live import movie as live_movie
        from photos import live, decoding
        camera = folder/'new-camera/Camera';camera.mkdir(parents=True)
        heic(camera/'Moment.heic',6); live_movie(camera/'Moment.mov'); dng(camera/'Camera.dng')
        second.import_folders([camera.parent])
        still_id=digest_file(camera/'Moment.heic'); motion_id=digest_file(camera/'Moment.mov');raw_id=digest_file(camera/'Camera.dng')
        assert second.query(search='type:live')['count']==1
        assert peer.sync()==0 and client.sync()==0
        assert first.query(search='type:live')['count']==1
        assert [p['id'] for p in live.components(first, first.photo(still_id))]==[still_id,motion_id]
        for media_id in [still_id,motion_id,raw_id]:
            original=Path(client.materialize(media_id))
            assert digest_file(original)==media_id and original.read_bytes()==second.original(second.photo(media_id)).read_bytes()
        with Image.open(decoding.display(first, first.photo(still_id))) as decoded:
            assert decoded.size==(64,96)
        with Image.open(decoding.display(first, first.photo(raw_id))) as decoded:
            assert decoded.size==(96,64)
        second.edit(still_id,'hidden_set','1');peer.sync();client.sync()
        assert first.photo(motion_id)['hidden']==1 and first.query(search='type:live')['count']==0
        second.edit(still_id,'hidden_set','0');live.separate(second,still_id,lambda p:True);peer.sync();client.sync()
        assert first.query(search='type:live')['count']==0 and len(live.components(first,first.photo(still_id)))==1
        # Exercise a full-sized index upload against the node's default 1-MiB
        # request bound, rather than only a one-photo embedding round trip.
        batch = folder/'semantic-batch/Batch'; batch.mkdir(parents=True)
        batch_ids = []
        import random, math
        generator = random.Random(1)
        values = [generator.uniform(-1,1) for _ in range(512)]
        norm = math.sqrt(sum(v*v for v in values))
        vector = struct.pack('<512f', *(v/norm for v in values))
        for index in range(52):
            path = batch/f'Image {index}.png'
            Image.new('RGB',(10+index,10),(index,150,100)).save(path)
            batch_ids.append(digest_file(path))
        first.import_folders([batch.parent])
        # Face-rich records must be split under the real node's byte bound,
        # with names/corrections encrypted and restored alongside each photo.
        from photos import people
        import hashlib
        face_vector = __import__('base64').b64encode(struct.pack('<128f',1.,*([0.]*127))).decode('ascii')
        for media_id in batch_ids:
            first.db.execute('INSERT INTO semantic_embeddings VALUES(?,?,?,?)', (media_id,semantic.MODEL,vector,''))
            people.accept_index(first,media_id,dict(model=people.MODEL,faces=[dict(
                id=hashlib.sha256(f'{media_id}:{i}'.encode()).hexdigest(),rect=[i*100,0,100,100],vector=face_vector,
                person=dict(id=hashlib.sha256(f'fixture-person:{i}'.encode()).hexdigest(),name=f'Fixture person {i}',ignored=0,updated=1),
                excluded=[]) for i in range(32)]))
        first.db.commit()
        assert client.sync() == 2 and client.sync() == 0 and peer.sync() == 0
        assert second.query(album='Batch')['count'] == 52
        for media_id in batch_ids:
            assert people.export_index(first,media_id)==people.export_index(second,media_id)
            assert second.db.execute('SELECT embedding FROM semantic_embeddings WHERE photo_id=?',(media_id,)).fetchone()[0] == vector
        assert Path(peer.materialize(batch_ids[0])).read_bytes() == (batch/'Image 0.png').read_bytes()
        # Birth dates, opt-in ordinary album rules and membership provenance
        # must survive real signed, encrypted paged requests in both directions.
        from photos import auto_albums
        person = hashlib.sha256(b'fixture-person:0').hexdigest()
        first.set_setting('people_enabled',True)
        people.edit(first,'person_birth',person,'1990-06-15')
        first.edit_album('','create','Automatic fixture')
        auto_albums.configure(first,'Automatic fixture',[person])
        while client.sync(): pass
        assert peer.sync()==0
        assert people.group(second,person)['birth']=='1990-06-15'
        assert json.loads(second.album_details('Automatic fixture')['auto_people'])==[person]
        assert second.query(album='Automatic fixture')['count']==52
        second.edit(batch_ids[0],'album_add','Automatic fixture')
        auto_albums.configure(second,'Automatic fixture',[],remove=True)
        while peer.sync(): pass
        assert client.sync()==0
        assert first.query(album='Automatic fixture')['count']==1
        assert first.query(album='Automatic fixture')['photos'][0]['id']==batch_ids[0]
        # Manual assignments are encrypted separately from detected faces.
        # A tag-only peer must not mark originals scanned, nor erase local faces.
        second.set_setting('people_enabled',True)
        tagged=people.tag(first,[photo_id],'Manually tagged person')
        while client.sync(): pass
        assert peer.sync()==0
        assert second.query(search='person:"Manually tagged person"')['count']==1
        assert not second.db.execute('SELECT 1 FROM face_scans WHERE photo_id=?',(photo_id,)).fetchone()
        assert people.export_index(first,photo_id)==people.export_index(second,photo_id)
        people.tag(second,[photo_id],'Manually tagged person',remove=True)
        while peer.sync(): pass
        assert client.sync()==0
        assert first.query(search='person:"Manually tagged person"')['count']==0
        assert people.export_index(first,photo_id)['tags']==[]
        assert not first.db.execute('SELECT 1 FROM face_scans WHERE photo_id=?',(photo_id,)).fetchone()
        from photos import collage
        collage_spec = dict(layout=3,shape=1,items=[dict(id=i,cx=5000,cy=5000,zoom=125) for i in batch_ids[:3]])
        collage_id = collage.save(first,collage_spec,dict(root=str(first.root)))
        while client.sync(): pass
        assert peer.sync()==0
        assert json.loads(second.photo(collage_id)['metadata'])['photosCollage']==collage_spec
        assert Path(peer.materialize(collage_id)).read_bytes()==first.original(first.photo(collage_id)).read_bytes()
        assert second.query(album='Collages')['photos'][0]['id']==collage_id
        from motion_fixtures import jpeg as motion_jpeg, heif as motion_heif, samsung as motion_samsung, extended_jpeg, samsung_heif
        from photos import motion, live
        motion_folder=folder/'embedded/Camera';motion_folder.mkdir(parents=True)
        clip=movie.read_bytes()
        motion_jpeg(motion_folder/'EmbeddedMP.jpg',clip)
        motion_heif(motion_folder/'EmbeddedMP.heic',clip)
        motion_heif(motion_folder/'EmbeddedMP.avif',clip,4)
        motion_samsung(motion_folder/'Samsung.jpg',clip)
        extended_jpeg(motion_folder/'Extended.jpg',clip)
        samsung_heif(motion_folder/'Samsung-heic.heic',clip)
        samsung_heif(motion_folder/'Samsung-no-xmp.heic',clip,xmp=False)
        first.import_folders([motion_folder.parent])
        while client.sync(): pass
        assert peer.sync()==0
        for path in motion_folder.iterdir():
            embedded_id=digest_file(path)
            assert motion.descriptor(first.photo(embedded_id))==motion.descriptor(second.photo(embedded_id))
            assert Path(peer.materialize(embedded_id)).read_bytes()==path.read_bytes()
            extracted,_=motion.extract(second,embedded_id,lambda p:not p['hidden'])
            assert extracted.read_bytes()==clip
        embedded_id=digest_file(motion_folder/'EmbeddedMP.jpg')
        live.separate(second,embedded_id,lambda p:not p['hidden'])
        while peer.sync(): pass
        assert client.sync()==0
        assert motion.descriptor(first.photo(embedded_id)) is None
        assert json.loads(first.photo(embedded_id)['edits'])['live_separated']
        assert first.original(first.photo(embedded_id)).read_bytes()==(motion_folder/'EmbeddedMP.jpg').read_bytes()
        first.import_folders([motion_folder.parent])
        assert motion.descriptor(first.photo(embedded_id)) is None
        print('Isolated Daochi v6: signed sync, face/semantic indexes, unchanged originals, editing, places, Live Photo privacy, embedded JPEG/HEIC/AVIF motion and separation round trip passed')
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
