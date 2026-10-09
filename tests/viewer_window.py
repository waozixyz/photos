"""Verify photo viewing against rendered synthetic private-Xvfb pixels."""
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
sys.path.insert(0, str(ROOT))
from photos.library import Library, digest_file
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_VIEWER_PORTABLE') == '1'
output = ROOT / 'build/screenshots'
output.mkdir(parents=True, exist_ok=True)


def wait(check, reason, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as directory:
    folder = Path(directory)
    source = folder / 'source/Synthetic/Gradient.png'
    source.parent.mkdir(parents=True)
    image = Image.new('RGB', (1600, 1000))
    image.putdata([(x * 255 // 1599, y * 255 // 999, 70 if (x // 80 + y // 80) % 2 else 160)
                   for y in range(1000) for x in range(1600)])
    # Wider than the decoder's old 4096-pixel ceiling. Pixel-size viewing
    # must retain all 5000 source pixels in both native and portable hosts.
    image = image.resize((5000, 1000))
    image.save(source)
    camera = source.with_name('Camera.jpg')
    exif = Image.Exif(); exif[274] = 6
    Image.new('RGB', (800, 400), (210, 130, 65)).save(camera, exif=exif)
    originals = {source: source.read_bytes(), camera: camera.read_bytes()}
    library = Library(folder / 'library')
    library.import_folders([source.parents[1]])
    parent_id = digest_file(source)
    library.edit(parent_id, 'taken', '2025-06-01T12:00:00Z')
    library.edit(digest_file(camera), 'taken', '2024-06-01T12:00:00Z')
    calls = []
    original_process = service_module.process

    def record(request, config):
        result = original_process(request, config)
        calls.append((request, result))
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
    with Server(dict(root=str(library.root)), folder / 'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever, daemon=True).start()
        child = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def xdo(*args):
            return subprocess.check_output(['xdotool', *map(str, args)], text=True).strip()

        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--pid', str(child.pid)], capture_output=True, text=True)
            return result.stdout.splitlines()[0] if result.returncode == 0 else None

        try:
            window = wait(locate, 'Photo viewer did not open')
            xdo('windowsize', window, 1180, 880); xdo('windowmove', window, 0, 0); xdo('windowfocus', window)
            wait(lambda: calls, 'Initial gallery missing'); time.sleep(.6)

            def click(x, y):
                xdo('mousemove', '--window', window, x, y); xdo('mousedown', 1)
                time.sleep(.12); xdo('mouseup', 1); time.sleep(.35)

            def state():
                raw = subprocess.check_output(['xprop', '-id', window, '_HARMONY_APP_STATE'], text=True)
                return json.loads(ast.literal_eval(raw.split(' = ', 1)[1].strip())) if ' = ' in raw else {}

            def shoot(name):
                path = output / name
                subprocess.run(['import', '-window', window, str(path)], check=True, timeout=5)
                with Image.open(path) as frame:
                    return frame.convert('RGB').copy()

            click(260, 320)
            wait(lambda:any(q.get('action')=='open' and q.get('id')==parent_id for q,r in calls), 'Photo did not open')
            time.sleep(.4)
            original_view = shoot('viewer-fit.png').crop((580,300,780,480))
            click(545, 210); click(545, 210)
            if not portable:
                wait(lambda: state().get('photo_zoom') == 2.25, 'Zoom controls did not magnify')
            magnified = shoot('viewer-zoom.png').crop((580,300,780,480))
            assert ImageChops.difference(original_view, magnified).getbbox(), 'Magnification did not change pixels'
            xdo('mousemove', '--window', window, 700, 410); xdo('mousedown', 1); time.sleep(.15)
            xdo('mousemove', '--window', window, 570, 330); time.sleep(.2); xdo('mouseup', 1); time.sleep(.3)
            panned = shoot('viewer-pan.png').crop((580,300,780,480))
            assert ImageChops.difference(magnified, panned).getbbox(), 'Dragging did not pan the image'
            if not portable:
                center = state()['photo_center']
                assert .5 < center[0] <= 1 and .5 < center[1] <= 1, center
            xdo('mousemove', '--window', window, 680, 390)
            anchor = shoot('viewer-anchor.png').getpixel((680,390))
            xdo('click', 4); time.sleep(.35)
            wheeled = shoot('viewer-wheel.png')
            assert max(abs(a-b) for a,b in zip(anchor,wheeled.getpixel((680,390)))) <= 3, 'Wheel zoom moved its anchor'
            if not portable:
                wait(lambda: state().get('photo_zoom',0)>2.25, 'Mouse wheel did not zoom')
            click(824,210)
            if not portable:
                wait(lambda: state().get('photo_zoom')==1 and state().get('photo_center')==[.5,.5], 'Fit did not reset view')
            fitted = shoot('viewer-refit.png').crop((580,300,780,480))
            assert not ImageChops.difference(original_view,fitted).getbbox(), 'Fit did not restore original pixels'
            click(960,210)
            if not portable:
                wait(lambda: abs(state().get('photo_zoom',0) - 5000/976)<.01, 'Large original was reduced or pixel size used the toolbar dimensions')
            pixel_view = shoot('viewer-pixel-portable.png' if portable else 'viewer-pixel-native.png')
            for x, y in [(400,330), (500,390), (600,430), (700,340), (800,420), (900,350), (1000,410)]:
                expected = image.getpixel((2500 + x - 680, 500 + y - 390))
                actual = pixel_view.getpixel((x,y))
                assert max(abs(a-b) for a,b in zip(expected,actual)) <= 4, 'Pixel-size view used a reduced image'
            click(1100,210)
            if not portable:
                wait(lambda: state().get('photo_expanded'), 'Expanded viewing did not open')
            expanded = shoot('viewer-expanded-portable.png' if portable else 'viewer-expanded-native.png')
            assert expanded.size == (1180,880)
            # Previous/Next keeps expanded viewing but resets zoom for the new photo.
            before = len(calls); xdo('key','Right')
            wait(lambda:any(q.get('action')=='open' and q.get('id')==digest_file(camera) for q,r in calls[before:]), 'Expanded navigation did not open next photo')
            time.sleep(.4)
            if not portable:
                wait(lambda: state().get('photo_zoom')==1 and state().get('photo_expanded'), 'New photo did not reset expanded zoom')
            click(800,845) # 1:1 uses the decoder's rotated 400 x 800 dimensions.
            if not portable:
                wait(lambda: abs(state().get('photo_zoom',0)-800/754)<.01, 'EXIF-oriented image dimensions were ignored')
            xdo('key','Escape'); time.sleep(.3)
            if not portable:
                wait(lambda: not state().get('photo_expanded') and state().get('selected',-1)>=0, 'Escape did not return to normal viewer')
            xdo('key','Escape'); time.sleep(.3)
            if not portable:
                wait(lambda: state().get('selected')==-1, 'Second Escape did not return to gallery')
            assert all(path.read_bytes()==data for path,data in originals.items())
            assert library.original(library.photo(parent_id)).read_bytes()==originals[source]
            print(('Portable' if portable else 'Native')+' photo viewer: rendered zoom/pan, pointer-anchored wheel, exact fit restoration, pixel size, expanded navigation, EXIF orientation and original preservation passed')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            service.shutdown(); library.db.close()
