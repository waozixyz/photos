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
        first = Library(folder/'desktop')
        first.import_folders([source.parent])
        config = dict(node=origin, node_id=node['node_id'])
        client = Client(first, config)
        assert client.sync() == 0
        photo_id = digest_file(source/'fixture.jpg')
        second = Library(folder/'thinkpad')
        identity = second.root/'private/identity.json'
        identity.parent.mkdir(mode=0o700)
        shutil.copyfile(client.identity_path, identity)
        os.chmod(identity,0o600)
        peer = Client(second, config)
        assert peer.sync() == 0
        assert second.query()['total'] == 2
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
        assert json.loads(first.photo(copy_id)['metadata'])['photosEdit']['parent'] == photo_id
        restored = Path(client.materialize(copy_id))
        assert digest_file(restored) == copy_id
        assert restored.read_bytes() == second.original(second.photo(copy_id)).read_bytes()
        with Image.open(restored) as copy:
            assert copy.size == (300, 300)
        assert (source/'fixture.jpg').read_bytes() == original.read_bytes()
        second.edit(photo_id, 'location', ''); peer.sync(); client.sync()
        assert first.details(photo_id)['location'] == ''
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
        edited_id=video.save(second,movie_id,[1000,3000,90,1])
        assert peer.sync()==0 and client.sync()==0
        restored_video=Path(client.materialize(edited_id))
        assert digest_file(restored_video)==edited_id
        assert restored_video.read_bytes()==second.original(second.photo(edited_id)).read_bytes()
        info=video.probe(restored_video)
        assert (info['width'],info['height'])==(80,80) and info['audio']
        assert first.details(edited_id)['location']=='1.0, 2.0'
        assert first.photo(edited_id)['albums']==second.photo(edited_id)['albums']
        assert json.loads(first.photo(edited_id)['metadata'])['photosVideoEdit']['parent']==movie_id
        print('Isolated Daochi v6: signed photo/album/Trash/editor/smart-search baseline plus edited video/audio, metadata and full original restore round trip passed')
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
