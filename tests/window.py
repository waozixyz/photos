"""Only synthetic photos and this test's own private Xvfb windows."""
import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from photos.library import Library
from photos.service import Server
import photos.service as service_module

ROOT=Path(__file__).resolve().parents[1]
assert os.environ.get('PHOTOS_PRIVATE_XVFB')=='1'
output=ROOT/'build/screenshots'
output.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as folder:
    folder=Path(folder)
    album=folder/'export/Weekend'
    album.mkdir(parents=True)
    for i,color in enumerate(['#438e7c','#ce9157','#708bb5','#bd788b']):
        Image.new('RGB',(700,500),color).save(album/f'Photo {i}.jpg')
    library=Library(folder/'library');library.import_folders([album.parent])
    config=folder/'config.json';config.write_text(json.dumps(dict(root=str(library.root),sources=[])))
    env=os.environ.copy();env['PHOTOS_CONFIG']=str(config);env['KRYON_SERVICE_ROOT']=str(folder)
    calls=[]
    original_process=service_module.process
    def record_request(request, config):
        summary={key:request.get(key) for key in ('action','view','search','album','offset')}
        try:
            result=original_process(request,config)
            summary.update(count=result.get('count'),rows=len(result.get('photos',[])),unlocked=result.get('vault_unlocked'),error=result.get('error'))
            return result
        finally:
            calls.append(summary)
    service_module.process=record_request
    service=Server(dict(root=str(library.root),sources=[]),folder/'kryon/services/photos.sock')
    threading.Thread(target=service.serve_forever,daemon=True).start()
    process=subprocess.Popen([str(ROOT/'build/photos-desktop')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    def xdo(*args):
        if args == ('click','1'):
            subprocess.check_call(['xdotool','mousedown','1'])
            time.sleep(.12)
            return subprocess.check_output(['xdotool','mouseup','1'],text=True).strip()
        return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()
    try:
        window=''
        for _ in range(100):
            assert process.poll() is None,process.stderr.read().decode()
            r=subprocess.run(['xdotool','search','--onlyvisible','--pid',str(process.pid)],capture_output=True,text=True)
            if r.returncode==0:
                window=r.stdout.splitlines()[0];break
            time.sleep(.1)
        assert window
        xdo('windowsize',window,1180,880)
        xdo('windowmove',window,0,0)
        xdo('windowfocus',window)
        def state(view=None, nodes=None, selection=None):
            geometry=xdo('getwindowgeometry','--shell',window)
            expected_right=int(next(line.split('=',1)[1] for line in geometry.splitlines() if line.startswith('WIDTH=')))-12
            for _ in range(100):
                raw=subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
                if ' = ' in raw:
                    value=json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip()))
                    find=next((c for c in value['controls'] if c['control']=='42'),None)
                    resized=find and abs(find['x']+find['width']-expected_right)<.1
                    if resized and not value['loading'] and (nodes is None or value['nodes']==nodes) and (view is None or value['view']==view) and (selection is None or value['selection_count']==selection):
                        return value
                time.sleep(.1)
            subprocess.run(['import','-window',window,str(output/'failed.png')],check=True)
            raise AssertionError(('Photos did not finish rendering', value, 'recent requests', calls[-12:], 'hidden rows', library.db.execute('SELECT COUNT(*) FROM photos WHERE hidden=1 AND trashed=0').fetchone()[0]))
        def action(label=None,control=None):
            button=None
            for _ in range(100):
                snapshot=state()
                button=next((c for c in snapshot['controls'] if (c['label']==label if label else c['control']==str(control)) and c['enabled']),None)
                if button:break
                assert process.poll() is None
                time.sleep(.1)
            assert button, (label,control,snapshot)
            xdo('mousemove','--window',window,int(button['x']+button['width']/2),int(button['y']+button['height']/2));xdo('click','1');time.sleep(.3)
        state('photos', 4)
        subprocess.run(['import','-window',window,str(output/'library.png')],check=True)
        with Image.open(output/'library.png') as captured:
            assert captured.size == (1180,880), captured.size
            # A solid synthetic thumbnail must actually appear in the gallery.
            pixel=captured.convert('RGB').getpixel((260,300))
            assert any(max(abs(a-b) for a,b in zip(pixel,color)) <= 4 for color in [(67,142,124),(206,145,87),(112,139,181),(189,120,139)])
        action(control=100);action('Favorite');time.sleep(.5)
        assert library.db.execute('SELECT SUM(favorite) FROM photos').fetchone()[0]==1
        action('Back');action('Select');action(control=100);action(control=101)
        assert state(selection=2)['selection_count']==2
        action('Favorite')
        assert library.db.execute('SELECT SUM(favorite) FROM photos').fetchone()[0]==2
        action('Select');action('Select loaded');action('Add to album')
        def enter(value):
            action(control=41)
            xdo('keydown','ctrl');xdo('key','a');time.sleep(.1);xdo('keyup','ctrl');time.sleep(.1)
            xdo('type','--delay','45','--clearmodifiers','--',value);time.sleep(.2)
        enter('Together');action('Save')
        assert library.query(album='Together')['count']==4, ([tuple(row) for row in library.db.execute('SELECT name,albums FROM photos')], state())
        action('Albums');state('albums')
        action('New album');enter('Empty album');action('Save');state('photos',0)
        action('Description');enter('An empty album description');action('Save')
        assert library.album_details('Empty album')['description']=='An empty album description'
        action('Rename album');enter('Empty renamed');action('Save')
        action('Pin album');assert library.album_details('Empty renamed')['pinned']==1
        action('Archive album');assert library.album_details('Empty renamed')['archived']==1
        action('Albums');state('albums');action('Active albums');state('albums')
        action('Empty renamed');action('Unarchive album');action('Delete empty album');state('albums')
        action('Archived albums');action('Empty albums');state('albums')
        subprocess.run(['import','-window',window,str(output/'albums.png')],check=True)
        action('Together');action('Rename album');enter('Trip');action('Save')
        assert library.query(album='Trip')['count']==4
        assert library.query(album='Together')['count']==0
        action('Select');action(control=100);action(control=101);action('Remove from album')
        state('photos',2)
        assert library.query(album='Trip')['count']==2
        assert library.query()['count']==4
        action(control=100);action('Use as album cover')
        assert library.album_details('Trip')['cover']
        action('Caption');enter('A native caption');action('Save')
        assert library.query(search='A native caption')['count']==1, ([tuple(row) for row in library.db.execute('SELECT name,caption FROM photos')], state())
        action('Edit date');enter('invalid-date');action('Save')
        assert state()['editor']=='taken' and 'valid date' in state()['status']
        enter('2024-06-15 14:30:00+02:00');action('Save')
        assert library.db.execute('SELECT COUNT(*) FROM photos WHERE strftime(\'%Y-%m-%d\',taken,\'unixepoch\')=?',('2024-06-15',)).fetchone()[0]==1
        subprocess.run(['import','-window',window,str(output/'details.png')],check=True)
        action('Photos'); state('photos',4)
        action('Newest first'); assert state()['sort']=='oldest'
        action('Oldest first'); assert state()['sort']=='newest'
        action('Select'); action('Select loaded'); action(control=69)
        state('photos',0)
        action(control=17); state('archive',4)
        subprocess.run(['import','-window',window,str(output/'archive.png')],check=True)
        action(control=100); action('Unarchive'); state('archive',3)
        action('Albums'); action('Weekend'); state('photos',4)
        action('Select'); action('Select loaded'); action('Remove from album')
        state('photos',0)
        action('Uncategorized'); state('uncategorized',2)
        assert library.query('trash')['count']==0
        subprocess.run(['import','-window',window,str(output/'uncategorized.png')],check=True)
        action('Local sync');state('sync')
        action('Continuous export');enter(str(folder/'folder-backup'));action('Save')
        from photos.worker import daemon
        daemon(dict(root=str(library.root),sources=[]),once=True)
        action('Sync');state('sync')
        assert library.setting('export')['continuous'] and not library.setting('export')['requested']
        assert len(list((folder/'folder-backup').rglob('*.jpg')))==4
        action('Stop export');assert not library.setting('export')['continuous']
        subprocess.run(['import','-window',window,str(output/'sync.png')],check=True)
        xdo('windowsize',window,720,880);action('Photos');state('photos')
        subprocess.run(['import','-window',window,str(output/'narrow.png')],check=True)
        action('Select');action('Select loaded')
        subprocess.run(['import','-window',window,str(output/'selection-narrow.png')],check=True)
        action('Done');xdo('windowsize',window,1180,880)
        ids = [row[0] for row in library.db.execute('SELECT id FROM photos')]
        library.edit_many(ids, 'archived_set', '0')
        action('Photos');state('photos',4);action(control=100)
        parent = library.query()['photos'][0]
        original_bytes = library.original(parent).read_bytes()
        action('Edit image');assert state()['editor']=='image'
        action('Rotate right');action('Flip horizontal');action('Flip vertical');action('Crop: original')
        action(control=271);action(control=273);action(control=275)
        subprocess.run(['import','-window',window,str(output/'editing.png')],check=True)
        action('Reset');action('Rotate right');action('Save copy')
        assert state()['editor']==''
        assert library.query()['total']==5
        assert library.original(library.photo(parent['id'])).read_bytes()==original_bytes
        copy = library.db.execute('SELECT * FROM photos WHERE id NOT IN (?,?,?,?)', ids).fetchone()
        assert (copy['width'],copy['height'])==(500,700)
        assert json.loads(copy['metadata'])['photosEdit']['parent']==parent['id']
        action('Edit image');action('Rotate right');action('Cancel')
        assert library.query()['total']==5 and state()['editor']==''
        action('Edit location');enter('-25.3, -57.6');action('Save')
        assert library.details(parent['id'])['location']=='-25.3, -57.6'
        action('Photos');action('Select');action('Select loaded');action('Edit location');enter('1, 2');action('Save')
        assert all(library.details(row[0])['location']=='1.0, 2.0' for row in library.db.execute('SELECT id FROM photos'))
        library.edit_many([parent['id']], 'taken', '2024-10-08T12:00:00Z')
        action('Memories');action('Choose day');enter('2026-10-08');action('Save')
        assert state('memories')['nodes']==1
        subprocess.run(['import','-window',window,str(output/'memories.png')],check=True)
        xdo('windowsize',window,720,880);action(control=100);action('Edit image');state()
        subprocess.run(['import','-window',window,str(output/'editing-narrow.png')],check=True)
        for control in state()['controls']:
            assert 0 <= control['x'] and control['x']+control['width']<=720.1, control
        action('Cancel')
        xdo('windowsize',window,1180,880)
        action('Hide');action('Hidden');state('hidden',0)
        assert not state()['vault_unlocked']
        subprocess.run(['import','-window',window,str(output/'hidden-locked.png')],check=True)
        action('Create password');enter('Synthetic window password')
        subprocess.run(['import','-window',window,str(output/'hidden-password.png')],check=True)
        action('Save')
        assert state()['editor']=='vault_confirm'
        enter('A different confirmation');action('Save')
        assert 'do not match' in state()['status']
        enter('Synthetic window password');action('Save');state('hidden',1)
        assert state()['vault_unlocked']
        assert 'Synthetic window password' not in json.dumps(state())
        action(control=100);action('Edit image');action('Lock Hidden');state('hidden',0)
        assert state()['editor']=='' and not state()['vault_unlocked']
        action('Unlock Hidden');enter('An incorrect password');action('Save')
        assert 'Incorrect password' in state()['status']
        enter('Synthetic window password');action('Save');state('hidden',1)
        action('Change password');enter('Synthetic replacement password');action('Save')
        assert state()['editor']=='vault_change_confirm'
        enter('A different replacement');action('Save')
        assert 'do not match' in state()['status']
        enter('Synthetic replacement password')
        subprocess.run(['import','-window',window,str(output/'hidden-password-change.png')],check=True)
        action('Save');state('hidden',0)
        assert not state()['vault_unlocked']
        assert 'Synthetic replacement password' not in json.dumps(state())
        action('Unlock Hidden');enter('Synthetic replacement password');action('Save');state('hidden',1)
        action('Photos');state('photos',4)
        action('Hidden');state('hidden',0)
        assert not state()['vault_unlocked']
        action('Photos');state('photos',4)
        action('Search tips')
        assert state()['editor']=='search_help'
        action('Back')
        action(control=40)
        xdo('type','--delay',45,'--clearmodifiers','--','is:favorite type:photo');action('Find')
        state('photos',2)
        action('Save search');enter('Native smart album');action('Save')
        state('photos',2)
        assert library.album_details('Native smart album')['filter']=='is:favorite type:photo'
        action('Edit search');enter('year:2024');action('Save');state('photos',1)
        action('Edit search');enter('has:location');action('Save');state('photos',4)
        subprocess.run(['import','-window',window,str(output/'smart-album.png')],check=True)
        action('Delete smart album');state('albums')
        assert library.query()['total']==5
        assert library.original(library.photo(parent['id'])).read_bytes()==original_bytes
        print('Private Xvfb: baseline organization/export/editor/memories plus confirmed Hidden password changes, search tips, structured filters, dynamic smart album creation/edit/deletion and original preservation verified')
    finally:
        service.shutdown();service.server_close()
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
