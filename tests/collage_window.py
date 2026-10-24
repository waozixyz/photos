"""Actual collage interaction uses synthetic photos and a private Xvfb server."""
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
portable = os.environ.get('PHOTOS_COLLAGE_PORTABLE') == '1'
suffix = 'portable' if portable else 'native'
output = ROOT / 'build/screenshots'; output.mkdir(exist_ok=True)


def wait(check, reason, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value: return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as directory:
    folder = Path(directory); source = folder / 'source/Family'; source.mkdir(parents=True)
    originals = {}; ids = []
    for i, color in enumerate(['red', 'green', 'yellow', 'purple']):
        path = source / f'Photo {i}.png'; image = Image.new('RGB', (320 + i, 240), color)
        for x in range(100):
            for y in range(240): image.putpixel((x, y), (40, 80, 180))
        image.save(path); originals[path] = path.read_bytes(); ids.append(digest_file(path))
    library = Library(folder / 'library'); library.import_folders([source.parent])
    for i, photo_id in enumerate(ids): library.edit(photo_id, 'taken', '2026-06-15T12:00:00Z')
    library.edit(ids[3], 'hidden_set', '1')
    calls = []; hold = threading.Event(); held = threading.Event(); release = threading.Event(); slow = threading.Event()
    original_process = service_module.process
    def record(request, config):
        if (request.get('action') == 'collage_check' and hold.is_set()) or (request.get('action') == 'collage_begin' and slow.is_set()):
            held.set(); release.wait(8)
        try: result = original_process(request, config)
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
    else: command = [os.environ.get('PHOTOS_BINARY', str(ROOT / 'build/photos-desktop'))]
    with Server(dict(root=str(library.root)), folder / 'kryon/services/photos.sock') as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        child = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def xdo(*args): return subprocess.check_output(['xdotool', *map(str, args)], text=True).strip()
        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--pid', str(child.pid)], capture_output=True, text=True)
            return result.stdout.splitlines()[0] if result.returncode == 0 else None
        try:
            window = wait(locate, 'Collage app did not open')
            xdo('windowsize', window, 1180, 880); xdo('windowmove', window, 0, 0); xdo('windowfocus', window)
            def state():
                raw = subprocess.check_output(['xprop', '-id', window, '_HARMONY_APP_STATE'], text=True)
                return json.loads(ast.literal_eval(raw.split(' = ', 1)[1].strip())) if ' = ' in raw else {}
            def click(x, y):
                xdo('mousemove', '--window', window, int(x), int(y)); xdo('mousedown', 1); time.sleep(.12); xdo('mouseup', 1); time.sleep(.3)
            def control(label, x, y):
                if not portable:
                    c = wait(lambda: next((c for c in state().get('controls', []) if c['label'] == label and c['enabled']), None), 'Control missing: ' + label)
                    x, y = c['x'] + c['width']/2, c['y'] + c['height']/2
                click(x, y)
            def result(action, since=0): return next((r for q, r in reversed(calls[since:]) if q.get('action') == action), None)
            def shot(name):
                path = output / f'collage-{name}-{suffix}.png'
                subprocess.run(['import', '-window', window, str(path)], check=True, timeout=5)
                with Image.open(path) as image: return image.convert('RGB').crop((192,324,1160,812)).tobytes()
            def begin():
                since = len(calls)
                control('Select',230,166); control('Select loaded',345,166); control('Create collage',1085,166)
                response = wait(lambda: result('collage_begin',since), 'Collage did not begin')
                assert len(response['collage_photos']) >= 2; time.sleep(.6)
                return response
            wait(lambda: calls, 'Gallery missing'); time.sleep(.6)
            response = begin(); assert len(response['collage_photos']) == 3
            original_order = [p['id'] for p in response['collage_photos']]
            control('Swap photos',800,208); click(518,568); click(680,568); control('Cancel swapping',800,208)
            control('Previous photo',300,252); control('Zoom +',800,252)
            xdo('mousemove', '--window', window, 518, 568); xdo('mousedown',1); time.sleep(.15)
            xdo('mousemove', '--window', window, 558, 588); time.sleep(.2); xdo('mouseup',1); time.sleep(.4)
            control('Grid layout',300,208); control('Square',550,208)
            if not portable:
                current = state(); assert current['collage_layout'] == 1 and current['collage_shape'] == 1
            shot('edited'); since = len(calls); control('Save copy',1060,208)
            saved = wait(lambda: (result('collage_save',since) or {}).get('saved_copy'), 'Collage was not saved')
            spec = json.loads(next(q['value'] for q,r in calls[since:] if q.get('action') == 'collage_save'))
            assert [p['id'] for p in spec['items']] == [original_order[1],original_order[0],original_order[2]]
            assert spec['items'][0]['zoom'] == 125 and spec['items'][0]['cx'] < 5000 and spec['items'][0]['cy'] < 5000
            assert (library.photo(saved)['width'], library.photo(saved)['height']) == (2048,1536)
            assert library.query(album='Collages')['count'] == 1
            # Start from the original gallery and revoke a selected source.
            control('Photos',90,90); time.sleep(.6); response = begin()
            private_id = response['collage_photos'][0]['id']; library.edit(private_id,'hidden_set','1'); since=len(calls)
            wait(lambda:(result('collage_check',since) or {}).get('error'),'Private source did not revoke collage')
            if not portable: wait(lambda:not state().get('collage_active') and state().get('editor')!='collage','Private collage remained displayed')
            shot('revoked'); library.edit(private_id,'hidden_set','0'); control('Photos',90,90); time.sleep(.6)
            response=begin(); before=shot('lease'); hold.set();wait(held.is_set,'Privacy check did not reach service');time.sleep(3)
            if not portable: assert not state()['collage_active'] and state()['editor']!='collage'
            assert shot('expired')!=before,'Expired collage pixels remained displayed'
            hold.clear();release.set();time.sleep(.8)
            if not portable: assert not state()['collage_active'],'Late check resurrected collage'
            # A cancelled, slow preparation must not reopen the editor.
            control('Photos',90,90);time.sleep(.6);control('Select',230,166);control('Select loaded',345,166)
            held.clear();release.clear();slow.set();control('Create collage',1085,166)
            wait(held.is_set,'Slow collage did not start');control('Cancel',230,296);slow.clear();release.set();time.sleep(.8)
            if not portable: assert not state()['collage_active'] and state()['editor']!='collage'
            for path,data in originals.items():assert path.read_bytes()==data
            assert library.query()['total']==5
            print(('Portable' if portable else 'Native')+' collage: selection, swapping, zoom/drag, layout/shape, actual copy, privacy revocation, elapsed lease, cancelled preparation and originals passed')
        except Exception:
            if child.poll() is None:shot('failure')
            print('Recent synthetic requests:',[(q.get('action'),r.get('error')) for q,r in calls[-8:]],flush=True)
            raise
        finally:
            release.set();child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            if child.returncode not in (0,-15):print('Collage process exit:',child.returncode,child.stderr.read().decode()[-3000:])
            server.shutdown();library.db.close()
