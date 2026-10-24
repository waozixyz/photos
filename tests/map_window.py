"""Actual map pixels and navigation on synthetic private-Xvfb windows only."""
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
portable = os.environ.get('PHOTOS_MAP_PORTABLE') == '1'
output = ROOT / 'build/screenshots'
output.mkdir(exist_ok=True)

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
    source = folder / 'source/Trip'
    source.mkdir(parents=True)
    ids, originals = [], {}
    for index in range(6):
        path = source / f'Photo {index}.png'
        Image.new('RGB', (300 + index, 200), (100 + index * 10, 150, 80)).save(path)
        ids.append(digest_file(path)); originals[path] = path.read_bytes()
    library = Library(folder / 'library')
    library.import_folders([source.parent])
    for index, coordinates in enumerate(['-25.3, -57.6', '-25.31, -57.61', '0, 0', '90, 180', '1, 2', '1, 2']):
        library.edit(ids[index], 'location', coordinates)
        library.edit(ids[index], 'taken', f'2025-06-{10+index:02d}T12:00:00Z')
    library.edit(ids[4], 'hidden_set', '1'); library.edit(ids[5], 'trash')
    calls = []
    original_process = service_module.process
    def record(request, config):
        result = original_process(request, config); calls.append((request, result)); return result
    service_module.process = record
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder)}
    if portable:
        flags = [] if (ROOT / 'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *flags], cwd=ROOT, text=True).strip())
        command = [os.environ.get('PHOTOS_PLAYER', str(kryon / 'build/bin/zx')),
                   os.environ.get('PHOTOS_BUNDLE', str(ROOT / 'build/photos-portable.zx'))]
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
            window = wait(locate, 'Map app did not open')
            xdo('windowsize', window, 1180, 880); xdo('windowmove', window, 0, 0); xdo('windowfocus', window)
            wait(lambda: calls, 'Initial gallery missing'); time.sleep(.5)
            def click(x, y):
                xdo('mousemove', '--window', window, x, y); xdo('mousedown', 1); time.sleep(.12); xdo('mouseup', 1); time.sleep(.35)
            def state():
                raw = subprocess.check_output(['xprop', '-id', window, '_HARMONY_APP_STATE'], text=True)
                return json.loads(ast.literal_eval(raw.split(' = ', 1)[1].strip())) if ' = ' in raw else {}
            def shoot(name):
                path = output / name
                subprocess.run(['import', '-window', window, str(path)], check=True, timeout=5)
                with Image.open(path) as frame: return frame.convert('RGB').copy()
            def latest():
                return next((q for q, r in reversed(calls) if q.get('view') == 'map'), None)
            click(90, 560)
            wait(lambda: latest(), 'Map query missing')
            wait(lambda: any(q.get('view') == 'map' and r.get('count') == 4 and len(r.get('markers', [])) == 3 for q, r in calls), 'Map did not include all public coordinates')
            time.sleep(.4)
            if not portable: wait(lambda: state().get('map_markers') == 3, 'Native map markers missing')
            world = shoot('map-world-portable.png' if portable else 'map-world-native.png')
            colors = world.crop((200, 275, 1160, 720)).getcolors(1000000)
            self_colors = [color for count, color in colors if count > 1000]
            assert len(self_colors) >= 2, 'Offline land image did not render'
            click(230, 165)
            wait(lambda: latest().get('map_bounds') == [-90000000, -45000000, 90000000, 45000000], 'Map zoom did not change geographic bounds')
            zoomed = shoot('map-zoom.png')
            assert ImageChops.difference(world.crop((200,275,1160,720)), zoomed.crop((200,275,1160,720))).getbbox(), 'Map zoom did not change pixels'
            before = latest()['map_bounds']
            xdo('mousemove', '--window', window, 700, 400); xdo('mousedown', 1); time.sleep(.15)
            xdo('mousemove', '--window', window, 900, 450); time.sleep(.2); xdo('mouseup', 1)
            wait(lambda: latest()['map_bounds'] != before, 'Map pan did not refresh coordinates')
            before = latest()['map_bounds']
            xdo('mousemove', '--window', window, 700, 400); xdo('click', 4)
            wait(lambda: latest()['map_bounds'] != before, 'Map wheel zoom did not refresh coordinates')
            after = latest()['map_bounds']
            px, py = (700-192)/976, (400-260)/488
            old_anchor = (before[0]+px*(before[2]-before[0]), before[3]-py*(before[3]-before[1]))
            new_anchor = (after[0]+px*(after[2]-after[0]), after[3]-py*(after[3]-after[1]))
            assert max(abs(a-b) for a,b in zip(old_anchor,new_anchor)) < 4, 'Map zoom moved its geographic anchor'
            click(450, 165)
            wait(lambda: latest().get('map_bounds') == [-180000000,-90000000,180000000,90000000], 'World did not reset map')
            time.sleep(.4); click(523, 573)
            wait(lambda: latest().get('map_gallery') == 1, 'Map marker did not open its photos')
            result = next(r for q, r in reversed(calls) if q.get('view') == 'map' and q.get('map_gallery') == 1)
            assert {p['id'] for p in result['photos']} == set(ids[:2]), 'Marker gallery included another location or private photos'
            time.sleep(.5); click(260, 340)
            wait(lambda: any(q.get('action') == 'open' and q.get('id') in ids[:2] for q,r in calls), 'Mapped photo did not open')
            time.sleep(.4); click(220, 165); time.sleep(.4); click(250, 165)
            wait(lambda: latest().get('map_gallery') == 0, 'Gallery did not return to map')
            dense = folder / 'dense'
            dense.mkdir()
            for row in range(8):
                for column in range(12):
                    path = dense / f'Location {row}-{column}.png'
                    Image.new('RGB', (20 + column, 20 + row), (30 + row, 40 + column, 50)).save(path)
                    path.with_name(path.name + '.json').write_text(json.dumps({'geoData': {
                        'latitude': -78.75 + row * 22.5, 'longitude': -165 + column * 30}}))
            library.import_folders([dense])
            # Explicit refresh avoids assuming portable frames run at 60 Hz.
            refresh_at = [0.0]
            def dense_ready():
                assert child.poll() is None, child.stderr.read().decode()
                if any(q.get('view') == 'map' and not q.get('map_gallery') and r.get('count') == 100 for q, r in calls):
                    return True
                if time.monotonic() >= refresh_at[0]:
                    refresh_at[0] = time.monotonic() + 2
                    click(450, 165)
                return False
            wait(dense_ready, 'Dense map did not refresh all coordinates')
            if not portable: wait(lambda: state().get('map_markers') == 96, 'Dense map did not render every cluster')
            dense_result = next(r for q, r in reversed(calls) if q.get('view') == 'map' and r.get('count') == 100)
            def dense_pixels():
                assert child.poll() is None, child.stderr.read().decode()
                frame = shoot('map-dense-portable.png' if portable else 'map-dense-native.png')
                fill = frame.getpixel((40, 90))
                for marker in dense_result['markers']:
                    px = 192 + (marker['longitude'] + 180000000) / 360000000 * 976
                    py = 260 + (90000000 - marker['latitude']) / 180000000 * 488
                    bx = max(192, min(1124, px - 22)); by = max(260, min(716, py - 16))
                    if frame.getpixel((int(bx + 8), int(by + 8))) != fill:
                        return False
                return True
            wait(dense_pixels, 'Dense marker pixels did not finish rendering')
            for path, data in originals.items(): assert path.read_bytes() == data
            assert library.query()['total'] == 102
            print(('Portable' if portable else 'Native') + ' offline map: land pixels, sparse/dense clusters, anchored zoom, drag pan, privacy filtering, marker gallery and original preservation passed')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            if child.returncode not in (0, -15):
                print('Map process exit:', child.returncode, child.stderr.read().decode()[-4000:])
            service.shutdown(); library.db.close()
