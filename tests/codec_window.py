"""Render synthetic HEIC/RAW pixels and Live Photo motion on private Xvfb."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('PHOTOS_SERVICE_PACKAGE', str(ROOT)))
from photos.library import Library, digest_file
from photos.service import Server
import photos.service as service_module
from codec_fixtures import heic, dng
from test_live import movie

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_CODEC_PORTABLE') == '1'
output = ROOT / 'build/screenshots'
output.mkdir(parents=True, exist_ok=True)


def wait(check, reason, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as directory:
    folder = Path(directory)
    source = folder / 'source/Camera'
    source.mkdir(parents=True)
    heic(source/'Moment.heic', 6)
    movie(source/'Moment.mov')
    dng(source/'Camera.dng', 6)
    originals = {p: p.read_bytes() for p in source.iterdir()}
    library = Library(folder/'library')
    library.import_folders([source.parent])
    still_id, raw_id = digest_file(source/'Moment.heic'), digest_file(source/'Camera.dng')
    library.edit(still_id, 'taken', '2025-06-01T12:00:00Z')
    library.edit(raw_id, 'taken', '2024-06-01T12:00:00Z')
    calls = []
    original_process = service_module.process

    def record(request, config):
        result = original_process(request, config)
        calls.append((request, result))
        return result

    service_module.process = record
    env = {**os.environ, 'KRYON_SERVICE_ROOT':str(folder), 'KRYON_VIDEO_TEST_SILENT':'1'}
    if portable:
        flags = [] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags], cwd=ROOT, text=True).strip())
        command = [os.environ.get('PHOTOS_PLAYER', str(kryon/'build/bin/zx')),
                   os.environ.get('PHOTOS_BUNDLE', str(ROOT/'build/photos-portable.zx'))]
    else:
        command = [os.environ.get('PHOTOS_BINARY', str(ROOT/'build/photos-desktop'))]
    with Server(dict(root=str(library.root)), folder/'kryon/services/photos.sock') as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        child = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def xdo(*args):
            return subprocess.check_output(['xdotool', *map(str,args)], text=True).strip()

        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            result = subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)], capture_output=True, text=True)
            return result.stdout.splitlines()[0] if result.returncode == 0 else None

        try:
            window = wait(locate, 'Codec app did not open')
            xdo('windowsize', window, 1180, 880); xdo('windowmove', window, 0, 0); xdo('windowfocus', window)
            wait(lambda:calls, 'Gallery missing'); time.sleep(.6)

            def click(x,y):
                xdo('mousemove','--window',window,x,y); xdo('mousedown',1)
                time.sleep(.12); xdo('mouseup',1); time.sleep(.25)

            def state():
                value = subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'], text=True)
                return json.loads(ast.literal_eval(value.split(' = ',1)[1].strip())) if ' = ' in value else {}

            def shoot(name):
                path = output/('codec-'+('portable-' if portable else 'native-')+name+'.png')
                subprocess.run(['import','-window',window,str(path)], check=True, timeout=5)
                with Image.open(path) as image:
                    return image.convert('RGB').copy()

            click(260,320)
            wait(lambda:any(q.get('action')=='open' and q.get('id')==still_id for q,r in calls), 'HEIC Live Photo missing')
            time.sleep(.4)

            def check_pixels(photo_id, name):
                reply = next(r for q,r in reversed(calls) if q.get('action')=='open' and q.get('id')==photo_id)
                photo = next(p for p in reply['photos'] if p['id']==photo_id)
                with Image.open(photo['display']) as image:
                    decoded = image.copy()
                click(960,210) # 1:1
                frame = shoot(name)
                checked = 0
                for y in range(8, decoded.height-8, 9):
                    for x in range(8, decoded.width-8, 9):
                        pixel = decoded.getpixel((x,y))
                        if len(pixel)==4 and pixel[3] != 255:
                            continue
                        actual = frame.getpixel((680+x-decoded.width//2,390+y-decoded.height//2))
                        assert max(abs(a-b) for a,b in zip(pixel[:3],actual)) <= 4, (name,x,y,pixel,actual)
                        checked += 1
                assert checked > 10

            check_pixels(still_id, 'heic-pixels')
            click(725,178) # Play Live Photo.
            wait(lambda:any(r.get('playback_path') and r.get('playback_id')==still_id for q,r in calls), 'Motion not authorized')
            if not portable:
                wait(lambda:state().get('playback_state')==2, 'Motion did not start')
            time.sleep(.1)
            first = shoot('motion-first').crop((400,280,950,590))
            time.sleep(.3)
            second = shoot('motion-second').crop((400,280,950,590))
            assert ImageChops.difference(first,second).getbbox(), 'Live Photo did not render motion'
            xdo('key','Escape'); time.sleep(.3)
            xdo('key','Right')
            wait(lambda:any(q.get('action')=='open' and q.get('id')==raw_id for q,r in calls), 'RAW navigation missing')
            time.sleep(.4)
            check_pixels(raw_id, 'raw-pixels')
            xdo('key','Left')
            time.sleep(.4)
            click(875,178) # Separate files, preserving both originals.
            wait(lambda:any(q.get('action')=='live_separate' and r.get('count')==3 for q,r in calls), 'Separation did not restore both gallery items')
            assert all(p.read_bytes()==data for p,data in originals.items())
            assert all(library.original(library.photo(digest_file(p))).read_bytes()==data for p,data in originals.items())
            print(('Portable' if portable else 'Native')+' synthetic HEIC/RAW: oriented exact 1:1 pixels, Live Photo motion, navigation and retained-original separation passed')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            server.shutdown(); library.db.close()
