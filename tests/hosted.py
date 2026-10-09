"""Date feed, live host colors and paging on synthetic, private Xvfb windows."""
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
sys.path.insert(0, str(ROOT))
from photos.library import Library
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
x = c.CDLL('libX11.so.6')
x.XOpenDisplay.restype = c.c_void_p
x.XDefaultRootWindow.argtypes = [c.c_void_p]; x.XDefaultRootWindow.restype = c.c_ulong
x.XCreateSimpleWindow.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_int, c.c_uint, c.c_uint, c.c_uint, c.c_ulong, c.c_ulong]
x.XCreateSimpleWindow.restype = c.c_ulong
x.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]; x.XInternAtom.restype = c.c_ulong
x.XChangeProperty.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_ulong, c.c_int, c.c_int, c.c_void_p, c.c_int]
x.XFlush.argtypes = [c.c_void_p]
x.XDestroyWindow.argtypes = [c.c_void_p, c.c_ulong]
x.XCloseDisplay.argtypes = [c.c_void_p]
display = x.XOpenDisplay(None)
assert display
parent = x.XCreateSimpleWindow(display, x.XDefaultRootWindow(display), 0, 0, 1, 1, 0, 0, 0)
container = x.XCreateSimpleWindow(display, parent, 0, 0, 1, 1, 0, 0, 0)
def property_(window, name, values):
    data = (c.c_ulong * len(values))(*values)
    x.XChangeProperty(display, window, x.XInternAtom(display, name.encode(), 0), 6, 32, 0, data, len(values))
    x.XFlush(display)
property_(parent, '_NET_WM_PID', [os.getpid()])

def palette(background, text):
    property_(container, '_HARMONY_APP_THEME', [1, background, background, text, text, 0x428a76ff, 0x303a44ff, 0x39454fff])

def wait(check, reason, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(reason)

with tempfile.TemporaryDirectory() as directory:
    folder = Path(directory); source = folder / 'source/Fixture'; source.mkdir(parents=True)
    for i in range(66):
        Image.new('RGB', (700, 500), (67 + i, 142, 124)).save(source / f'Photo {i}.png')
    library = Library(folder / 'library'); library.import_folders([source.parent])
    ids = [row[0] for row in library.db.execute('SELECT id FROM photos ORDER BY id')]
    for i, photo_id in enumerate(ids):
        library.db.execute('UPDATE photos SET taken=? WHERE id=?', (1700000000 if i < 30 else 1600000000, photo_id))
    library.db.commit()
    calls = []
    paging = threading.Event(); release_page = threading.Event()
    original = service_module.process
    def process(request, config):
        calls.append(dict(request))
        if request.get("offset") == 60 and request.get("view") == "photos":
            paging.set(); release_page.wait(5)
        return original(request, config)
    service_module.process = process
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder), 'HARMONY_APP_HOST_PID': str(os.getpid()), 'HARMONY_APP_CONTAINER': str(container)}
    with Server(dict(root=str(library.root)), folder / 'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever, daemon=True).start()
        locked = [] if (ROOT / 'ziran.local.toml').exists() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *locked], cwd=ROOT, text=True).strip())
        player = Path(os.environ.get('PHOTOS_PLAYER', kryon / 'build/bin/zib'))
        palette(0x141e28ff, 0xe8eef4ff)
        log_path = ROOT / 'build/screenshots/hosted.log'; log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('w') as log:
            child = subprocess.Popen([str(player), str(ROOT / 'build/photos-portable.zib')], env=env, stdout=log, stderr=log)
            def xdo(*args):
                return subprocess.check_output(['xdotool', *map(str, args)], text=True).strip()
            try:
                def find():
                    assert child.poll() is None, log_path.read_text()
                    result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--pid', str(child.pid)], capture_output=True, text=True)
                    return result.stdout.splitlines()[0] if result.returncode == 0 else None
                window = wait(find, 'Photos did not open')
                xdo('windowsize', window, 1180, 880)
                shot = ROOT / 'build/screenshots/hosted.png'
                def color(expected):
                    assert child.poll() is None, log_path.read_text()
                    subprocess.run(['import', '-window', window, str(shot)], check=True, stdout=subprocess.DEVNULL)
                    with Image.open(shot) as image:
                        return image.width > 20 and image.height > 600 and image.convert('RGB').getpixel((20, 600)) == expected
                wait(lambda: color((20, 30, 40)), 'Portable Photos did not inherit the dark host theme')
                wait(lambda: len(calls) >= 1, 'Date feed did not load')
                assert calls[0]['view'] == 'photos' and calls[0]['offset'] == 0
                palette(0xf4f2edff, 0x242830ff)
                wait(lambda: color((244, 242, 237)), 'Photos did not follow the host theme change')
                xdo('mousemove', '--window', window, 600, 700)
                xdo('click', '--repeat', 100, '--delay', 10, 5)
                wait(lambda: any(request['offset'] == 60 for request in calls), 'Scrolling did not append older photos')
                wait(paging.is_set, 'Paging did not start')
                # A background read must preserve a click instead of disabling
                # the sidebar until the service replies.
                xdo('mousemove', '--window', window, 65, 137); xdo('click', 1)
                time.sleep(.2); release_page.set()
                wait(lambda: any(request['view'] == 'albums' for request in calls), 'Navigation click was lost during a background update')
                time.sleep(.5)
                assert child.poll() is None, log_path.read_text()
                print('Portable date feed, appended older photos, inherited dark colors, live light theme change and navigation during loading verified')
            finally:
                release_page.set()
                child.terminate()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
                service.shutdown()
    library.db.close()
x.XDestroyWindow(display, parent); x.XCloseDisplay(display)
