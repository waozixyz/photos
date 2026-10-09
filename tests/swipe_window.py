"""Reversible gallery selection on synthetic photos and a private Xvfb."""
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
from photos.library import Library
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_SWIPE_PORTABLE') == '1'
hosted = os.environ.get('PHOTOS_SWIPE_HOSTED') == '1'
assert not hosted or portable
output = ROOT / 'build/screenshots'; output.mkdir(exist_ok=True)


def wait(check, reason, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as temporary:
    folder = Path(temporary); source = folder / 'source/Synthetic'; source.mkdir(parents=True)
    originals = {}
    for i in range(72):
        path = source / f'Photo {i:03}.png'
        Image.new('RGB', (300, 200), (70+i, 150, 100)).save(path)
        originals[path] = path.read_bytes()
    library = Library(folder / 'library'); library.import_folders([source.parent])
    library.db.execute('UPDATE photos SET taken=1700000000'); library.db.commit()
    initial = [p['id'] for p in library.query()['photos']]
    all_ids = initial + [p['id'] for p in library.query(offset=60)['photos']]
    with Image.open(library.original(library.photo(all_ids[-4]))) as frame:
        last_row_color = frame.convert('RGB').getpixel((150,100))
    calls = []; results = []
    original_process = service_module.process

    def record(request, config):
        result = original_process(request, config)
        calls.append(dict(request)); results.append(result)
        return result

    service_module.process = record
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder)}
    if portable:
        flags = [] if (ROOT / 'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *flags], cwd=ROOT, text=True).strip())
        command = [os.environ.get('PHOTOS_PLAYER', str(kryon / 'build/bin/zib')),
                   os.environ.get('PHOTOS_BUNDLE', str(ROOT / 'build/photos-portable.zib'))]
    else:
        command = [os.environ.get('PHOTOS_BINARY', str(ROOT / 'build/photos-desktop'))]
    # These are this test's own windows on the private display, for focus
    # cancellation and the real Harmony embedding path.
    x = c.CDLL('libX11.so.6'); x.XOpenDisplay.restype = c.c_void_p
    x.XDefaultRootWindow.argtypes = [c.c_void_p]; x.XDefaultRootWindow.restype = c.c_ulong
    x.XCreateSimpleWindow.argtypes = [c.c_void_p,c.c_ulong,c.c_int,c.c_int,c.c_uint,c.c_uint,c.c_uint,c.c_ulong,c.c_ulong]
    x.XCreateSimpleWindow.restype = c.c_ulong
    x.XInternAtom.argtypes = [c.c_void_p,c.c_char_p,c.c_int]; x.XInternAtom.restype = c.c_ulong
    x.XChangeProperty.argtypes = [c.c_void_p,c.c_ulong,c.c_ulong,c.c_ulong,c.c_int,c.c_int,c.c_void_p,c.c_int]
    x.XMapWindow.argtypes = [c.c_void_p,c.c_ulong]; x.XDestroyWindow.argtypes = [c.c_void_p,c.c_ulong]
    x.XSetInputFocus.argtypes = [c.c_void_p,c.c_ulong,c.c_int,c.c_ulong]
    x.XFlush.argtypes = [c.c_void_p]; x.XCloseDisplay.argtypes = [c.c_void_p]
    connection = x.XOpenDisplay(None); assert connection
    root_window = x.XDefaultRootWindow(connection)
    secondary = x.XCreateSimpleWindow(connection,root_window,1200,900,100,80,0,0,0)
    x.XMapWindow(connection,secondary)
    parent = None
    if hosted:
        parent = x.XCreateSimpleWindow(connection,root_window,0,0,1180,880,0,0,0)
        container = x.XCreateSimpleWindow(connection,parent,0,0,1180,880,0,0,0)
        pid = (c.c_ulong * 1)(os.getpid())
        x.XChangeProperty(connection,parent,x.XInternAtom(connection,b'_NET_WM_PID',0),6,32,0,pid,1)
        x.XMapWindow(connection,parent); x.XMapWindow(connection,container)
        loader = Path(os.environ.get('PHOTOS_APP_START',Path.home()/'.local/lib/harmony/libapp-start.so'))
        assert loader.is_file()
        env.update(HARMONY_APP_HOST_PID=str(os.getpid()),HARMONY_APP_CONTAINER=str(container),LD_PRELOAD=str(loader),SDL_VIDEODRIVER='x11')
    x.XFlush(connection)
    child = None; window = None
    with Server(dict(root=str(library.root)), folder / 'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever,daemon=True).start()
        child = subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)

        def xdo(*args):
            return subprocess.check_output(['xdotool', *map(str,args)],text=True).strip()

        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            r = subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return r.stdout.splitlines()[0] if r.returncode == 0 else None

        def state():
            raw = subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
            return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}

        def move(px,py):
            xdo('mousemove','--window',window,px,py); time.sleep(.18)

        def click(px,py):
            move(px,py); xdo('mousedown',1); time.sleep(.12); xdo('mouseup',1); time.sleep(.4)

        def count(expected):
            if not portable:
                wait(lambda: state().get('selection_count') == expected, f'Selection should contain {expected} items')

        def favorite(expected):
            before = len(calls); click(266,210)
            request = wait(lambda: next((q for q in calls[before:] if q.get('action') == 'favorite_set'),None), 'Selected favorites did not reach the service')
            assert set(request['ids']) == set(expected), ('Wrong reversible selection',len(request['ids']),len(expected))
            wait(lambda: set(r[0] for r in library.db.execute('SELECT id FROM photos WHERE favorite=1')) == set(expected), 'Batch favorites did not persist')
            time.sleep(.5)

        try:
            window = wait(locate,'Swipe test app did not open')
            xdo('windowsize',window,1180,880); xdo('windowmove',window,0,0); xdo('windowfocus',window)
            wait(lambda: calls,'Initial gallery missing'); time.sleep(.8)
            # A click still selects one item; horizontal activation adds a
            # range, and reversing restores the range's original selection.
            click(230,166); click(260,410); count(1)
            move(490,410); xdo('mousedown',1); time.sleep(.15)
            move(740,410); count(3)
            suffix = 'hosted' if hosted else ('portable' if portable else 'native')
            subprocess.run(['import','-window',window,str(output/('swipe-'+suffix+'.png'))],check=True,timeout=5)
            move(490,410); count(2); xdo('mouseup',1); time.sleep(.25)
            favorite(initial[:2])
            # Removing a range is reversible too, retaining older picks.
            library.db.execute('UPDATE photos SET favorite=0'); library.db.commit()
            click(230,166); click(343,166); count(60)
            move(260,410); xdo('mousedown',1); time.sleep(.15)
            move(740,410); count(57); move(490,410); count(58)
            xdo('mouseup',1); time.sleep(.25); favorite(initial[2:])
            # Losing focus while pressed cancels the gesture. Returning and
            # moving the still-held pointer cannot extend the last range.
            library.db.execute('UPDATE photos SET favorite=0'); library.db.commit()
            click(230,166); click(260,410); count(1)
            move(490,410); xdo('mousedown',1); time.sleep(.15); move(740,410); count(3)
            x.XSetInputFocus(connection,secondary,1,0); x.XFlush(connection); time.sleep(.3)
            move(990,650); xdo('windowfocus',window); time.sleep(.3)
            xdo('mouseup',1); time.sleep(.25); count(3); favorite(initial[:3])
            # Edge scrolling loads the next page and keeps the original anchor
            # across append responses, without lifting the held pointer.
            library.db.execute('UPDATE photos SET favorite=0'); library.db.commit()
            click(230,166); click(260,410); count(1)
            move(490,410); xdo('mousedown',1); time.sleep(.15); move(740,410)
            move(740,831)
            wait(lambda: any(q.get('offset') == 60 for q in calls), 'Swipe edge scroll never loaded the next gallery page')
            def at_last_row():
                # Observe the rendered end of the gallery instead of assuming
                # the portable interpreter runs as fast as the native host.
                path = output/('swipe-end-'+suffix+'.png')
                subprocess.run(['import','-window',window,str(path)],check=True,timeout=5)
                with Image.open(path) as frame:
                    pixel = frame.convert('RGB').getpixel((260,760))
                    return max(abs(a-b) for a,b in zip(pixel,last_row_color)) <= 3
            wait(at_last_row,'Swipe did not scroll into the final appended row',timeout=35)
            time.sleep(.3); xdo('mouseup',1); time.sleep(.3)
            before = len(calls); click(266,210)
            request = wait(lambda: next((q for q in calls[before:] if q.get('action') == 'favorite_set'),None), 'Paged swipe selection did not persist')
            assert len(request['ids']) > 60, ('Swipe did not continue into appended page',len(request['ids']))
            assert set(initial).issubset(request['ids'])
            assert not any(q.get('action') == 'open' for q in calls), 'A claimed swipe opened a photo'
            # The switch uses the ordinary protected library route, persists
            # locally and leaves a disabled drag to normal gallery handling.
            click(65,378); click(300,592)
            wait(lambda: library.setting('swipe_selection_enabled',True) is False,'Swipe preference was not saved')
            reopened = Library(library.root)
            assert reopened.setting('swipe_selection_enabled',True) is False
            reopened.db.close()
            click(65,90); click(230,166); click(260,410); count(1)
            move(490,410); xdo('mousedown',1); time.sleep(.15); move(740,410)
            xdo('mouseup',1); time.sleep(.3); count(1)
            click(65,378); click(300,592)
            wait(lambda: library.setting('swipe_selection_enabled',False) is True,'Swipe preference did not re-enable')
            assert all(path.read_bytes() == value for path,value in originals.items())
            print(suffix.capitalize()+' swipe: click selection, reversible add/remove, focus cancellation, edge scrolling across pages, batch identities and original preservation passed')
        except BaseException:
            if window:
                subprocess.run(['import','-window',window,str(output/('swipe-failed-'+('portable' if portable else 'native')+'.png'))],timeout=5)
                if not portable:
                    snapshot = state()
                    print('Swipe state:',{k:snapshot.get(k) for k in ('view','loading','scroll','nodes','selection_count','status')})
            print('Recent actions:',[(q.get('action'),q.get('offset'),len(q.get('ids',[]))) for q in calls[-8:]])
            raise
        finally:
            if child:
                child.terminate()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            service.shutdown(); library.db.close()
            if parent: x.XDestroyWindow(connection,parent)
            x.XDestroyWindow(connection,secondary); x.XCloseDisplay(connection)
