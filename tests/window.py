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
    service=Server(dict(root=str(library.root),sources=[]),folder/'kryon/services/photos.sock')
    threading.Thread(target=service.serve_forever,daemon=True).start()
    process=subprocess.Popen([str(ROOT/'build/photos-desktop')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    def xdo(*args):
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
        def state(view=None, nodes=None):
            for _ in range(100):
                raw=subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
                if ' = ' in raw:
                    value=json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip()))
                    if not value['loading'] and (nodes is None or value['nodes']==nodes) and (view is None or value['view']==view):
                        return value
                time.sleep(.1)
            raise AssertionError('Photos did not finish rendering')
        def action(label=None,control=None):
            button=next(c for c in state()['controls'] if (c['label']==label if label else c['control']==str(control)) and c['enabled'])
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
        assert state()['selection_count']==2
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
        subprocess.run(['import','-window',window,str(output/'albums.png')],check=True)
        action('Together');action('Rename album');enter('Trip');action('Save')
        assert library.query(album='Trip')['count']==4
        assert library.query(album='Together')['count']==0
        action('Select');action(control=100);action(control=101);action('Remove from album')
        state('photos',2)
        assert library.query(album='Trip')['count']==2
        assert library.query()['count']==4
        action(control=100);action('Caption');enter('A native caption');action('Save')
        assert library.query(search='A native caption')['count']==1, ([tuple(row) for row in library.db.execute('SELECT name,caption FROM photos')], state())
        action('Edit date');enter('invalid-date');action('Save')
        assert state()['editor']=='taken' and 'valid date' in state()['status']
        enter('2024-06-15 14:30:00+02:00');action('Save')
        assert library.db.execute('SELECT COUNT(*) FROM photos WHERE strftime(\'%Y-%m-%d\',taken,\'unixepoch\')=?',('2024-06-15',)).fetchone()[0]==1
        subprocess.run(['import','-window',window,str(output/'details.png')],check=True)
        action('Local sync');state('sync')
        subprocess.run(['import','-window',window,str(output/'sync.png')],check=True)
        xdo('windowsize',window,720,880);action('Photos');state('photos')
        subprocess.run(['import','-window',window,str(output/'narrow.png')],check=True)
        action('Select');action('Select loaded')
        subprocess.run(['import','-window',window,str(output/'selection-narrow.png')],check=True)
        print('Private Xvfb: gallery, batch selection and favorite, album add/rename/remove, caption editor, date validation and correction, details, local sync and narrow layout verified')
    finally:
        service.shutdown();service.server_close()
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
