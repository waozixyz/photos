"""Similar-photo review with synthetic originals on a private display."""
import ast
import ctypes as c
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('PHOTOS_SERVICE_PACKAGE', str(ROOT)))
from photos.library import Library, digest_file, packed
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_SIMILAR_PORTABLE') == '1'
hosted = os.environ.get('PHOTOS_SIMILAR_HOSTED') == '1'
assert not hosted or portable
output = ROOT / 'build/screenshots'
output.mkdir(exist_ok=True)


def wait(check, reason, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as temporary:
    folder = Path(temporary)
    source = folder / 'source/Storage'
    source.mkdir(parents=True)
    originals = {}
    names = ['Reference.png','Match A.png','Match B.png','Other.png','Private.png','Trash.png']
    for i, name in enumerate(names):
        path = source/name
        Image.new('RGB',(300,200),(70+i*25,150,100)).save(path)
        originals[path] = path.read_bytes()
    library = Library(folder/'library')
    library.import_folders([source.parent])
    ids = {p.name:digest_file(p) for p in originals}
    import math,struct
    from photos import semantic
    for name, score in zip(names,[1.,.99,.96,.75,.999,.999]):
        blob = struct.pack('<512f',score,math.sqrt(1-score*score),*([0.0]*510))
        library.db.execute('INSERT INTO semantic_embeddings VALUES(?,?,?,?)',(ids[name],semantic.MODEL,blob,''))
        library.db.execute('UPDATE photos SET taken=?,hidden=?,trashed=? WHERE id=?',
                           (1700000001 if name=='Reference.png' else 1700000000,int(name=='Private.png'),1 if name=='Trash.png' else 0,ids[name]))
    library.db.commit()
    library.set_setting('semantic_enabled',False)
    library.set_setting('semantic_processing',False)
    library.edit(ids['Match A.png'],'album_add','Scene only')
    library.edit_album('Scene only','cover',ids['Match A.png'])
    calls = []
    original_process = service_module.process

    def record(request, config):
        result = original_process(request, config)
        calls.append((dict(request), result))
        return result

    service_module.process = record
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder)}
    if portable:
        flags = [] if (ROOT / 'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *flags], cwd=ROOT, text=True).strip())
        command = [os.environ.get('PHOTOS_PLAYER', str(kryon/'build/bin/zib')), os.environ.get('PHOTOS_BUNDLE', str(ROOT/'build/photos-portable.zib'))]
    else:
        command = [os.environ.get('PHOTOS_BINARY', str(ROOT/'build/photos-desktop'))]
    x = None
    connection = None
    parent = None
    if hosted:
        x = c.CDLL('libX11.so.6')
        x.XOpenDisplay.restype = c.c_void_p
        x.XDefaultRootWindow.argtypes = [c.c_void_p]; x.XDefaultRootWindow.restype = c.c_ulong
        x.XCreateSimpleWindow.argtypes = [c.c_void_p,c.c_ulong,c.c_int,c.c_int,c.c_uint,c.c_uint,c.c_uint,c.c_ulong,c.c_ulong]
        x.XCreateSimpleWindow.restype = c.c_ulong
        x.XMapWindow.argtypes = [c.c_void_p,c.c_ulong]
        x.XInternAtom.argtypes = [c.c_void_p,c.c_char_p,c.c_int]; x.XInternAtom.restype = c.c_ulong
        x.XChangeProperty.argtypes = [c.c_void_p,c.c_ulong,c.c_ulong,c.c_ulong,c.c_int,c.c_int,c.c_void_p,c.c_int]
        x.XFlush.argtypes = [c.c_void_p]
        x.XDestroyWindow.argtypes = [c.c_void_p,c.c_ulong]
        x.XCloseDisplay.argtypes = [c.c_void_p]
        connection = x.XOpenDisplay(None)
        assert connection
        parent = x.XCreateSimpleWindow(connection,x.XDefaultRootWindow(connection),0,0,1180,880,0,0,0)
        container = x.XCreateSimpleWindow(connection,parent,0,0,1180,880,0,0,0)
        pid = (c.c_ulong * 1)(os.getpid())
        x.XChangeProperty(connection,parent,x.XInternAtom(connection,b'_NET_WM_PID',0),6,32,0,pid,1)
        x.XMapWindow(connection,parent); x.XMapWindow(connection,container); x.XFlush(connection)
        loader = Path(os.environ.get('PHOTOS_APP_START',Path.home()/'.local/lib/harmony/libapp-start.so'))
        assert loader.is_file()
        env.update(HARMONY_APP_HOST_PID=str(os.getpid()),HARMONY_APP_CONTAINER=str(container),LD_PRELOAD=str(loader),SDL_VIDEODRIVER='x11')
    with Server(dict(root=str(library.root)), folder/'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever,daemon=True).start()
        child = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        window = None

        def xdo(*args):
            return subprocess.check_output(['xdotool', *map(str,args)],text=True).strip()

        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            result = subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode == 0 else None

        def state():
            raw = subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
            return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}

        def control(label, px, py):
            if not portable:
                def ready():
                    current = state()
                    return next((item for item in current.get('controls',[]) if item['label']==label and item['enabled'] and (label!='Trash' or item['control']=='64')), None) if not current.get('loading') else None
                item = wait(ready, 'Missing similarity control: '+label)
                px = item['x'] + item['width']/2; py = item['y'] + item['height']/2
            xdo('mousemove','--window',window,int(px),int(py)); xdo('mousedown',1)
            time.sleep(.15); xdo('mouseup',1); time.sleep(.5)

        def result(since):
            return next((r for q,r in reversed(calls[since:]) if q.get('view')=='similar'),None)

        try:
            window = wait(locate,'Similarity app did not open')
            xdo('windowsize',window,1180,880); xdo('windowmove',window,0,0); xdo('windowfocus',window)
            wait(lambda: calls,'Initial similarity gallery missing'); time.sleep(.5)
            control('Reference.png',300,350)
            wait(lambda: any(q.get('action')=='open' for q,r in calls),'Reference did not open')
            since = len(calls); control('Enable ML search',1090,166)
            wait(lambda: any(q.get('view')=='semantic' for q,r in calls[since:]),'Similarity did not open opt-in search settings')
            assert library.setting('semantic_enabled',False) is False
            library.set_setting('semantic_enabled',True)
            control('Photos',65,90); control('Reference.png',300,350)
            since = len(calls); control('Find similar',1090,166)
            found = wait(lambda: result(since),'Similarity matches did not load')
            assert [p['id'] for p in found['photos']] == [ids['Match A.png'],ids['Match B.png']]
            assert all(q.get('similar_id')==ids['Reference.png'] for q,r in calls[since:] if q.get('view')=='similar')
            time.sleep(.8)
            suffix = 'hosted' if hosted else ('portable' if portable else 'native')
            subprocess.run(['import','-window',window,str(output/('similar-'+suffix+'.png'))],check=True,timeout=5)
            control('Select',230,236); control('Select loaded',343,236)
            since = len(calls); control('Trash',830,324)
            wait(lambda: any(q.get('action')=='trash' and set(q.get('ids',[]))=={ids['Match A.png'],ids['Match B.png']} for q,r in calls[since:]),'Similarity selection did not reach Trash')
            wait(lambda: library.photo(ids['Match A.png'])['trashed'] and library.photo(ids['Match B.png'])['trashed'],'Similar matches were not recoverably trashed')
            assert not library.photo(ids['Reference.png'])['trashed']
            library.edit_many([ids['Match A.png'],ids['Match B.png']],'restore')
            control('Photos',65,90); control('Reference.png',300,350)
            since = len(calls); control('Find similar',1090,166)
            wait(lambda: (r:=result(since)) and r['count']==2,'Restored matches did not reappear')
            control('Select',230,236); control('Select loaded',343,236)
            control('Keep reference & albums',340,236)
            subprocess.run(['import','-window',window,str(output/('similar-cleanup-'+suffix+'.png'))],check=True,timeout=5)
            since=len(calls);control('Cancel',454,368)
            assert not any(q.get('action')=='similar_cleanup' for q,r in calls[since:])
            assert not library.photo(ids['Match A.png'])['trashed']
            control('Keep reference & albums',340,236)
            since=len(calls);control('Keep reference',304,368)
            wait(lambda: any(q.get('action')=='similar_cleanup' and q.get('value')==ids['Reference.png'] for q,r in calls[since:]),'Album-preserving cleanup request missing')
            wait(lambda: library.photo(ids['Match A.png'])['trashed'] and library.photo(ids['Match B.png'])['trashed'],'Cleanup did not trash selected matches')
            assert 'Scene only' in json.loads(library.photo(ids['Reference.png'])['albums'])
            assert library.album_details('Scene only')['cover']==ids['Reference.png']
            assert not library.photo(ids['Reference.png'])['trashed']
            library.edit_many([ids['Match A.png'],ids['Match B.png']],'restore')
            control('Photos',65,90); control('Reference.png',300,350)
            since=len(calls);control('Find similar',1090,166)
            wait(lambda: (r:=result(since)) and r['count']==2,'Cleaned-up matches did not restore')
            since=len(calls);control('All similar groups',1068,166)
            grouped=wait(lambda: next((r for q,r in reversed(calls[since:]) if q.get('view')=='similar_groups'),None),'Library-wide groups missing')
            assert grouped['count']==1 and grouped['photos'][0]['id']==ids['Reference.png']
            assert grouped['photos'][0]['group_size']==3
            time.sleep(.5)
            subprocess.run(['import','-window',window,str(output/('similar-groups-'+suffix+'.png'))],check=True,timeout=5)
            since=len(calls);control('Reference.png',300,350)
            wait(lambda: (r:=result(since)) and r['count']==2,'Group did not open its reference matches')
            since = len(calls); library.edit(ids['Reference.png'],'hidden_set','1')
            wait(lambda: (r:=result(since)) is not None and r['count']==0,'Private reference still returned derived matches')
            if not portable:
                wait(lambda: state().get('nodes')==0,'Private reference left old matches displayed')
            since = len(calls); control('Photos',65,90)
            wait(lambda: any(q.get('view')=='photos' for q,r in calls[since:]),'Similarity review did not return to timeline')
            for path, data in originals.items():
                assert path.read_bytes()==data
                assert library.original(library.photo(digest_file(path))).read_bytes()==data
            print('Private Xvfb '+suffix+': opt-in settings, ranked matches, selection, confirmed/cancelled album-preserving cleanup, cover retention, restore, privacy revocation and originals passed')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            service.shutdown(); library.db.close()
            if parent: x.XDestroyWindow(connection,parent); x.XCloseDisplay(connection)
