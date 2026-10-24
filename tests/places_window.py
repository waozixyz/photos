"""Exercise actual named-place UI with synthetic photos on private Xvfb."""
import ast
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
from photos.library import Library, LibraryError, digest_file
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_PLACES_PORTABLE') == '1'
output = ROOT / 'build/screenshots'
output.mkdir(exist_ok=True)


def wait(check, reason, timeout=25):
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
    for index in range(4):
        path = source / f'Photo {index}.png'
        Image.new('RGB', (300 + index, 200), (80 + index * 30, 160, 100)).save(path)
        ids.append(digest_file(path)); originals[path] = path.read_bytes()
    library = Library(folder / 'library')
    library.import_folders([source.parent])
    for i, location in enumerate(['0, 0', '0, .01', '0, .1', '0, 0']):
        library.edit(ids[i], 'location', location)
        library.edit(ids[i], 'taken', '2025-06-15T12:00:00Z')
    library.edit(ids[3], 'hidden_set', '1')
    calls = []
    original_process = service_module.process
    def record(request, config):
        try:
            result = original_process(request, config)
        except LibraryError as error:
            calls.append((request, dict(error=str(error)))); raise
        calls.append((request, result)); return result
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
            window = wait(locate, 'Places app did not open')
            xdo('windowsize', window, 1180, 880); xdo('windowmove', window, 0, 0); xdo('windowfocus', window)
            wait(lambda: calls, 'Initial gallery missing'); time.sleep(.5)
            def state():
                raw = subprocess.check_output(['xprop', '-id', window, '_HARMONY_APP_STATE'], text=True)
                return json.loads(ast.literal_eval(raw.split(' = ', 1)[1].strip())) if ' = ' in raw else {}
            def click(x, y):
                xdo('mousemove', '--window', window, x, y); xdo('mousedown', 1); time.sleep(.12); xdo('mouseup', 1); time.sleep(.3)
            def control(label, x, y):
                if not portable:
                    def ready():
                        current = state()
                        return next((c for c in current.get('controls', []) if c['label'] == label and c['enabled']), None) if not current.get('loading') else None
                    c = wait(ready, 'Control missing: ' + label)
                    x, y = c['x'] + c['width']/2, c['y'] + c['height']/2
                click(int(x), int(y))
            def text(value, search=False):
                click(400, 30 if search else 244)
                xdo('keydown', 'ctrl'); time.sleep(.12); xdo('keydown', 'a'); time.sleep(.12)
                xdo('keyup', 'a'); xdo('keyup', 'ctrl'); time.sleep(.12)
                xdo('type', '--clearmodifiers', '--delay', '18', value)
                if not value:
                    xdo('keydown', 'BackSpace'); time.sleep(.1); xdo('keyup', 'BackSpace')
                time.sleep(.25)
            def result(action, since=0):
                return next((r for q, r in reversed(calls[since:]) if q.get('action') == action), None)
            def shoot(name):
                subprocess.run(['import', '-window', window, str(output / name)], check=True, timeout=5)
            text('name:"Photo 0"', True); control('Find', 1130, 30)
            wait(lambda: any(q.get('search') == 'name:"Photo 0"' and r.get('count') == 1 for q, r in calls), 'Photo filter failed')
            time.sleep(.4); control('Photo 0.png', 260, 300)
            wait(lambda: result('open'), 'Photo did not open'); time.sleep(.4)
            control('Name place', 1100, 702)
            text('Home'); control('Save', 245, 352); time.sleep(.3)
            text('0, 0, 1.2'); at = len(calls); control('Save', 245, 352)
            created = wait(lambda: result('place_create', at), 'Named place was not created')
            assert created.get('album') == 'Home' and created['count'] == 2 and not created.get('error'), created
            assert {p['id'] for p in created['photos']} == set(ids[:2])
            time.sleep(.5); shoot('places-gallery-portable.png' if portable else 'places-gallery-native.png')
            control('Edit area', 800, 221); text('0, 0, 0'); at = len(calls); control('Save', 245, 352)
            rejected = wait(lambda: result('place_update', at), 'Invalid radius response missing')
            assert 'error' in rejected and library.album_details('Home')['filter'] == 'near:0,0,1.2'
            text('0, 0, 0.001'); at = len(calls); control('Save', 245, 352)
            changed = wait(lambda: result('place_update', at), 'Area did not change')
            assert changed['count'] == 1
            time.sleep(.4); control('Rename place', 980, 177); text('Our home'); at = len(calls); control('Save', 245, 352)
            wait(lambda: result('album_rename', at), 'Place was not renamed')
            text('place:"Our home"', True); control('Find', 1130, 30)
            wait(lambda: any(q.get('search') == 'place:"Our home"' and r.get('count') == 1 for q, r in calls), 'Named search failed')
            text('', True); control('Find', 1130, 30); time.sleep(.4)
            control('Places', 90, 605)
            wait(lambda: any(q.get('view') == 'places' and any(a['name'] == 'Our home' for a in r.get('albums', [])) for q, r in calls), 'Places catalog missing')
            time.sleep(.4); shoot('places-catalog-portable.png' if portable else 'places-catalog-native.png')
            control('Our home', 500, 232); time.sleep(.4)
            control('Delete place', 1040, 221)
            wait(lambda: library.db.execute('SELECT deleted FROM albums WHERE name=?', ('Our home',)).fetchone()[0] == 1, 'Place deletion failed')
            # A separate manual area does not need a photo or remote geocoder.
            control('Places', 90, 605); time.sleep(.4); control('New place', 248, 177)
            text('Date line'); control('Save', 245, 352); text('0, 180, 10'); at = len(calls); control('Save', 245, 352)
            wait(lambda: result('place_create', at), 'Manual place missing')
            assert library.album_details('Date line')['filter'] == 'near:0,180,10'
            for path, data in originals.items(): assert path.read_bytes() == data
            assert library.query()['total'] == 4
            print(('Portable' if portable else 'Native') + ' Places: photo/manual creation, radius validation and updates, private matches, rename/search/catalog/delete and original preservation passed')
        finally:
            if sys.exc_info()[0] is not None:
                if 'shoot' in locals() and child.poll() is None:
                    try: shoot('places-failed-portable.png' if portable else 'places-failed-native.png')
                    except (OSError, subprocess.SubprocessError): pass
                print('Last synthetic requests:', [(q.get('action'), q.get('view'), q.get('search'), r.get('error')) for q,r in calls[-5:]])
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            if child.returncode not in (0, -15): print('Places process exit:', child.returncode, child.stderr.read().decode()[-4000:])
            service.shutdown(); library.db.close()
