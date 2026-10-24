"""Memory Lane and date jumps through actual native/portable controls on private Xvfb."""
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
sys.path.insert(0, str(ROOT / 'tests'))
from photos.library import Library, LibraryError, digest_file
from photos import people
from photos.service import Server
import photos.service as service_module
from test_people import vector

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_DISCOVERY_PORTABLE') == '1'
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
    source = folder / 'source/Family'
    source.mkdir(parents=True)
    ids, originals = [], {}
    for index in range(90):
        path = source / f'Photo {index}.png'
        Image.new('RGB', (300 + index, 200), (60 + index, 160, 100)).save(path)
        ids.append(digest_file(path)); originals[path] = path.read_bytes()
    library = Library(folder / 'library')
    library.import_folders([source.parent])
    library.set_setting('people_enabled', True)
    library.set_setting('people_processing', True)
    for i, photo_id in enumerate(ids):
        library.edit(photo_id, 'taken', f'{2020+i//6}-07-{1+i%6:02d}T12:00:00Z' if i < 18 else '2010-01-01T12:00:00Z')
        if i < 18:
            people.store(library, photo_id, [dict(rect=[1000,1000,5000,6000],embedding=vector())], folder)
    person = library.db.execute('SELECT person_id FROM faces LIMIT 1').fetchone()[0]
    library.set_setting('people_processing', False)
    people.edit(library, 'person_name', person, 'Ada')
    library.edit_album('', 'create', 'Ada album')
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
            window = wait(locate, 'Discovery app did not open')
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
                xdo('keydown', 'ctrl'); time.sleep(.1); xdo('keydown', 'a'); time.sleep(.1)
                xdo('keyup', 'a'); xdo('keyup', 'ctrl'); time.sleep(.1)
                xdo('type', '--clearmodifiers', '--delay', '18', value)
                if not value:
                    xdo('keydown', 'BackSpace'); time.sleep(.1); xdo('keyup', 'BackSpace')
                time.sleep(.25)

            def result(action, since=0):
                return next((r for q, r in reversed(calls[since:]) if q.get('action') == action), None)

            def shoot(name):
                subprocess.run(['import', '-window', window, str(output / name)], check=True, timeout=5)

            control('Jump to date', 600, 166)
            text('2021-07-03'); since = len(calls); control('Save', 250, 339)
            jumped = wait(lambda: result('jump_date', since), 'Date jump missing')
            assert jumped['jump_offset'] == 9 and jumped['photos'][0]['id'] == ids[8]
            time.sleep(.7); shoot('discovery-jump-portable.png' if portable else 'discovery-jump-native.png')
            since = len(calls); control('Photo 8.png', 300, 310)
            opened = wait(lambda: result('open', since), 'Jumped photo did not open')
            assert opened['detail']['id'] == ids[8] and opened['photos'][0]['id'] == ids[8]
            assert next(q for q,r in calls[since:] if q.get('action') == 'open')['offset'] == 9
            time.sleep(.5); control('Back', 240, 166)
            since = len(calls)
            wait(lambda: any(q.get('action') == 'query' and q.get('offset') == 9 for q,r in calls[since:]), 'Refresh lost the jumped date')
            control('People', 90, 698)
            wait(lambda: any(q.get('view') == 'people' and not q.get('person') for q,r in calls), 'People view missing')
            time.sleep(.6); control('Ada', 450, 328)
            wait(lambda: any(q.get('person') == person and r.get('photos') for q,r in calls), 'Person gallery missing')
            time.sleep(.6); control('Birth date', 455, 288)
            text('2017-06-01'); control('Save', 250, 339)
            born = wait(lambda: result('person_birth'), 'Birthday save missing')
            assert born['person_detail']['birth'] == '2017-06-01'
            time.sleep(.6); control('Memory Lane', 285, 288)
            lane = wait(lambda: next((r for q,r in reversed(calls) if q.get('view') == 'lane'), None), 'Memory Lane missing')
            assert lane['lane_years'] == [2020,2021,2022] and lane['lane_year'] == 2020 and len(lane['photos']) == 6
            time.sleep(.6); shoot('discovery-lane-portable.png' if portable else 'discovery-lane-native.png')
            since = len(calls); control('Next year', 990, 230)
            wait(lambda: any(r.get('lane_year') == 2021 for q,r in calls[since:]), 'Next Memory Lane year missing')
            time.sleep(.6); since = len(calls); control('Previous year', 670, 230)
            wait(lambda: any(r.get('lane_year') == 2020 for q,r in calls[since:]), 'Previous Memory Lane year missing')
            time.sleep(.5)
            # Ignore the group concurrently; refresh must revoke its identity
            # and timeline even with an already open page.
            people.edit(library, 'person_ignore', person)
            since = len(calls)
            revoked = wait(lambda: next((r for q,r in calls[since:] if q.get('view') == 'lane' and not r.get('photos') and not r.get('lane_years')), None), 'Ignored lane was not revoked')
            assert not revoked['person_detail']['name'] and not revoked['person_detail'].get('birth')
            people.edit(library, 'person_ignore', person)
            control('Albums',90,137)
            time.sleep(.6); control('Ada album',450,220)
            wait(lambda: any(q.get('album') == 'Ada album' for q,r in calls), 'Regular album missing')
            time.sleep(.6); control('Auto-add people',420,254)
            picker = wait(lambda: result('auto_people_begin'), 'People picker missing')
            assert picker['auto_selection'] == [] and picker['albums'][0]['name'] == 'Ada'
            time.sleep(.6); control('Ada',450,372)
            shoot('discovery-auto-portable.png' if portable else 'discovery-auto-native.png')
            since=len(calls);control('Save people',300,230)
            filled=wait(lambda:result('album_auto_people',since),'Automatic album did not save')
            assert filled['count'] == 18
            assert json.loads(library.album_details('Ada album')['auto_people']) == [person]
            library.set_setting('people_processing',True)
            people.store(library,ids[18],[dict(rect=[1000,1000,5000,6000],embedding=vector())],folder)
            library.set_setting('people_processing',False)
            since=len(calls)
            wait(lambda:any(q.get('album') == 'Ada album' and r.get('count') == 19 for q,r in calls[since:]),'New indexed match did not join album')
            time.sleep(.8);control('Jump to date',819,166)
            text('2021-07-03');since=len(calls);control('Save',250,339)
            album_jump=wait(lambda:result('jump_date',since),'Album date jump missing')
            assert album_jump['jump_offset']==9 and album_jump['photos'][0]['id']==ids[8]
            time.sleep(.8)
            # Deliberate memberships remain when automatic contributions are
            # removed; original files and the Family album remain intact.
            library.edit(ids[0],'album_add','Ada album')
            since=len(calls);control('Auto-add people',420,254)
            wait(lambda:result('auto_people_begin',since),'Reopening the people picker lost its navigation click')
            time.sleep(.8);control('Deselect all',990,230)
            control('Keep previously auto-added photos',600,274)
            time.sleep(.5);shoot('discovery-stop-portable.png' if portable else 'discovery-stop-native.png')
            since=len(calls);control('Save people',300,230)
            stopped=wait(lambda:result('album_auto_people_remove',since),'Automatic album did not stop')
            assert stopped['count']==1 and stopped['photos'][0]['id']==ids[0]
            assert json.loads(library.album_details('Ada album')['auto_people'])==[]
            for path, data in originals.items():
                assert path.read_bytes() == data
            assert library.query()['total'] == 90
            print(('Portable' if portable else 'Native') + ' discovery: date/open/refresh, birthday, eligible years, privacy revocation, automatic people albums/new indexing/selective removal and unchanged originals passed')
        except Exception:
            shoot('discovery-failure-portable.png' if portable else 'discovery-failure-native.png')
            print('Recent synthetic requests:',[(q.get('action'),q.get('view'),q.get('ids'),r.get('error'),r.get('view')) for q,r in calls[-10:]],flush=True)
            raise
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
            service.shutdown()
    library.db.close()
