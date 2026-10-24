"""Large-file review with synthetic originals on a private display."""
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
portable = os.environ.get('PHOTOS_STORAGE_PORTABLE') == '1'
hosted = os.environ.get('PHOTOS_STORAGE_HOSTED') == '1'
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
    for i, name in enumerate(['Small.png', 'Big.png', 'Hidden.png', 'Pending.png', 'Video.mp4']):
        path = source / name
        if name.endswith('.mp4'):
            path.write_bytes(b'Synthetic video original')
        else:
            Image.new('RGB', (300, 200), (70+i*30, 150, 100)).save(path)
        originals[path] = path.read_bytes()
    library = Library(folder / 'library')
    library.import_folders([source.parent])
    ids = {p.name: digest_file(p) for p in originals}
    for name, size in [('Small.png', 20*2**20), ('Big.png', 5*2**30+19), ('Hidden.png', 6*2**30), ('Pending.png', 7*2**30), ('Video.mp4', 30*2**20)]:
        library.db.execute('UPDATE photos SET size=?,manifest=?,hidden=? WHERE id=?',
                           (size, '{}' if name == 'Pending.png' else packed(dict(chunks=['synthetic-encrypted-chunk'])), int(name == 'Hidden.png'), ids[name]))
    library.db.commit()
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
        command = [os.environ.get('PHOTOS_PLAYER', str(kryon/'build/bin/zx')), os.environ.get('PHOTOS_BUNDLE', str(ROOT/'build/photos-portable.zx'))]
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
                item = wait(ready, 'Missing storage control: '+label)
                px = item['x'] + item['width']/2; py = item['y'] + item['height']/2
            xdo('mousemove','--window',window,int(px),int(py)); xdo('mousedown',1)
            time.sleep(.15); xdo('mouseup',1); time.sleep(.5)

        def result(view, kind, since):
            return next((r for q,r in reversed(calls[since:]) if q.get('view')==view and q.get('large_type','all')==kind), None)

        try:
            window = wait(locate,'Storage app did not open')
            xdo('windowsize',window,1180,880); xdo('windowmove',window,0,0); xdo('windowfocus',window)
            wait(lambda: calls,'Initial storage gallery missing'); time.sleep(.5)
            control('Local sync',65,378)
            since = len(calls); control('Large files',530,687)
            initial = wait(lambda: result('large','all',since),'Large files did not load')
            assert [p['id'] for p in initial['photos']] == [ids['Big.png'],ids['Video.mp4'],ids['Small.png']]
            assert initial['photos'][0]['storage_size'] == 5*2**30+19
            time.sleep(.8)
            suffix = 'hosted' if hosted else ('portable' if portable else 'native')
            subprocess.run(['import','-window',window,str(output/('storage-'+suffix+'.png'))],check=True,timeout=5)
            since = len(calls); control('Photos only',422,204)
            photos = wait(lambda: result('large','photos',since),'Photo filter did not load')
            assert [p['id'] for p in photos['photos']] == [ids['Big.png'],ids['Small.png']]
            since = len(calls); control('Videos only',570,204)
            videos = wait(lambda: result('large','videos',since),'Video filter did not load')
            assert [p['id'] for p in videos['photos']] == [ids['Video.mp4']]
            control('Select',230,250); control('Select loaded',343,250)
            since = len(calls); control('Trash',830,294)
            wait(lambda: any(q.get('action')=='trash' and q.get('ids')==[ids['Video.mp4']] for q,r in calls[since:]),'Large-file selection did not reach Trash')
            wait(lambda: library.photo(ids['Video.mp4'])['trashed'],'Selected original was not recoverably trashed')
            assert not library.photo(ids['Big.png'])['trashed']
            library.edit(ids['Video.mp4'],'restore')
            since = len(calls); control('All files',275,204)
            wait(lambda: (r:=result('large','all',since)) and r['count']==3,'Restored video missing from storage review')
            since = len(calls); control('Photos',65,90)
            wait(lambda: any(q.get('view')=='photos' for q,r in calls[since:]),'Storage review did not return to timeline')
            if not portable:
                wait(lambda: state().get('view')=='photos','Storage review did not return to timeline')
            for path, data in originals.items():
                assert path.read_bytes()==data
                assert library.original(library.photo(digest_file(path))).read_bytes()==data
            print('Private Xvfb '+suffix+': storage entry, exact byte badges, filters, size order, selection, recoverable Trash and originals passed')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            service.shutdown(); library.db.close()
            if parent: x.XDestroyWindow(connection,parent); x.XCloseDisplay(connection)
