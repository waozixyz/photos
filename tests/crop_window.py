"""Drag real crop rectangles on synthetic native/portable private windows."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from photos import video
from photos.library import Library, digest_file
from photos.service import Server
import photos.service as service_module
from test_video import fixture

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_CROP_PORTABLE') == '1'
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


original_process = service_module.process
for kind in ('image', 'video'):
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        source = folder / ('source/Synthetic/Photo.png' if kind == 'image' else 'source/Synthetic/Video.mp4')
        if kind == 'image':
            source.parent.mkdir(parents=True)
            image = Image.new('RGBA', (320, 200))
            for y in range(200):
                for x in range(320):
                    image.putpixel((x, y), (x * 255 // 319, y * 255 // 199, 100, 150))
            image.save(source)
        else:
            fixture(source)
        original = source.read_bytes()
        library = Library(folder / 'library')
        library.import_folders([source.parents[1]])
        parent_id = digest_file(source)
        calls = []

        def record(request, config):
            response = original_process(request, config)
            calls.append((request, response))
            return response

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

            def request(action, before=0):
                return next(((q, r) for q, r in reversed(calls[before:]) if q.get('action') == action), None)

            try:
                window = wait(locate, 'Crop test did not open')
                xdo('windowsize', window, 1180, 880)
                xdo('windowmove', window, 0, 0)
                xdo('windowfocus', window)
                wait(lambda: calls, 'Gallery request missing')
                time.sleep(.6)

                def click(x, y):
                    xdo('mousemove', '--window', window, x, y)
                    xdo('mousedown', 1); time.sleep(.12); xdo('mouseup', 1); time.sleep(.3)

                click(260, 320)
                wait(lambda: request('open'), 'Original did not open')
                click(250, 210)
                action = 'image_preview' if kind == 'image' else 'video_preview'
                wait(lambda: request('image_preview' if kind == 'image' else 'video_begin'), 'Editor did not open')
                def preview_visible():
                    target = output / 'crop-ready.png'
                    subprocess.run(['import', '-window', window, str(target)], check=True)
                    with Image.open(target) as frame:
                        colors = frame.convert('RGB').crop((400,300,900,600)).get_flattened_data()
                        return sum(max(pixel)-min(pixel)>60 for pixel in colors)>3000
                wait(preview_visible, 'Editor did not render the initial preview')
                time.sleep(.2)
                # Rotation and a horizontal flip must happen before crop selection.
                before = len(calls)
                click(300 if kind == 'image' else 680, 208)
                wait(lambda: request(action, before), 'Rotation preview missing')
                if kind == 'image':
                    before = len(calls); click(540, 208)
                    wait(lambda: request(action, before), 'Flip preview missing')
                before = len(calls)
                click(1090, 166)
                q, response = wait(lambda: request(action, before), 'Uncropped preview missing')
                assert 'error' not in response, response
                time.sleep(.4)
                scale = min(976 / response['edit_width'], 500 / response['edit_height'])
                w, h = response['edit_width'] * scale, response['edit_height'] * scale
                left, top = 192 + (976 - w) / 2, 238 + (500 - h) / 2
                xdo('mousemove', '--window', window, round(left + w * .2), round(top + h / 4))
                xdo('mousedown', 1); time.sleep(.15)
                for fraction in (.4, .5, .6, .75):
                    xdo('mousemove', '--window', window, round(left + w * (fraction - .05)), round(top + h * fraction))
                    time.sleep(.12)
                xdo('mouseup', 1); time.sleep(.3)
                subprocess.run(['import', '-window', window, str(output / f'crop-{kind}-{"portable" if portable else "native"}.png')], check=True)
                before = len(calls); click(330, 764)
                q, response = wait(lambda: request(action, before), 'Chosen crop was not previewed')
                assert 'error' not in response, response
                import json
                values = json.loads(q['value'])
                rectangle = values[7:] if kind == 'image' else values[4:]
                assert len(rectangle) == 4 and all(abs(a-b) < 100 for a,b in zip(rectangle, [2000,2500,5000,5000])), values
                # Cancelling a reopened crop restores the chosen crop, including coordinates.
                before = len(calls); click(1090, 166)
                wait(lambda: request(action, before), 'Crop tool did not reopen')
                before = len(calls); click(1000, 764)
                q, response = wait(lambda: request(action, before), 'Crop cancellation did not restore preview')
                assert json.loads(q['value']) == values
                before = len(calls); click(300 if kind == 'image' else 680, 208)
                q, response = wait(lambda: request(action, before), 'Crop did not rotate with the image')
                values = json.loads(q['value'])
                rectangle = values[7:] if kind == 'image' else values[4:]
                assert all(abs(a-b) < 100 for a,b in zip(rectangle, [2500,2000,5000,5000])), values
                before = len(calls); click(680, 764 if kind == 'image' else 772)
                wait(lambda: request('image_save' if kind == 'image' else 'video_save', before), 'Copy did not start')
                wait(lambda: library.query()['total'] == 2, 'Crop copy was not saved')
                if kind == 'video':
                    wait(lambda: request('video_status', before) and request('video_status', before)[1].get('saved_copy'), 'Video completion missing')
                copy_id = next(row[0] for row in library.db.execute('SELECT id FROM photos') if row[0] != parent_id)
                copy = library.photo(copy_id)
                if kind == 'image':
                    assert abs(copy['width'] - 160) <= 2 and abs(copy['height'] - 100) <= 2, copy
                    with Image.open(library.original(copy)) as result:
                        assert result.mode == 'RGBA' and result.getpixel((0,0))[3] == 150
                else:
                    info = video.probe(library.original(copy))
                    assert abs(info['width'] - 60) <= 2 and abs(info['height'] - 40) <= 2 and info['audio'], info
                assert source.read_bytes() == original
                assert library.original(library.photo(parent_id)).read_bytes() == original
                print(('Portable' if portable else 'Native') + ' ' + kind + ' custom crop: drag after rotation/flips, preview, cancel restoration, save copy and original preservation passed')
            finally:
                child.terminate()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
                service.shutdown(); library.db.close()
