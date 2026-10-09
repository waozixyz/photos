"""Exercise actual People UI with synthetic photos on private Xvfb."""
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
from photos import people
from test_people import vector
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_PEOPLE_PORTABLE') == '1'
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
    for index in range(5):
        path = source / f'Photo {index}.png'
        Image.new('RGB', (300 + index, 200), (80 + index * 30, 160, 100)).save(path)
        ids.append(digest_file(path)); originals[path] = path.read_bytes()
    library = Library(folder / 'library')
    library.import_folders([source.parent])
    for i, location in enumerate(['0, 0', '0, .01', '0, .1', '0, 0']):
        library.edit(ids[i], 'location', location)
        library.edit(ids[i], 'taken', '2025-06-15T12:00:00Z')
    library.edit(ids[3], 'hidden_set', '1')
    library.edit(ids[4], 'taken', '2026-06-15T12:00:00Z')
    library.set_setting('people_enabled',True)
    library.set_setting('people_processing',True)
    for i in range(3): people.store(library,ids[i],[dict(rect=[1000,1000,5000,6000],embedding=vector(.95-i*.05))],folder)
    person=library.db.execute('SELECT person_id FROM faces LIMIT 1').fetchone()[0]
    library.set_setting('people_processing',False)
    library.set_setting('people_enabled',False)
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
            window = wait(locate, 'Semantic search app did not open')
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
            control('People',90,698)
            wait(lambda:any(q.get('view')=='people' for q,r in calls),'People view missing')
            time.sleep(.5); control('Enable People',290,234)
            enabled=wait(lambda:result('people_enable'),'People did not enable'); assert enabled['people_enabled']
            assert enabled['albums'][0]['count']==3
            time.sleep(.6); control('Pause face indexing',290,234)
            wait(lambda:result('people_pause'),'Face pause missing');time.sleep(.5)
            control('Unnamed person',450,328)
            wait(lambda:any(q.get('person')==person and r.get('photos') for q,r in calls),'Person gallery missing')
            time.sleep(.5); control('Name or merge',670,208)
            text('Ada'); control('Save',250,350)
            named=wait(lambda:result('person_name'),'Person naming missing'); assert named['person_detail']['name']=='Ada'
            time.sleep(.6);shoot('people-named-portable.png' if portable else 'people-named-native.png')
            control('Select',245,332);time.sleep(.5)
            control('Select loaded',355,332);time.sleep(.5)
            assert len(named['photos'])==3
            control('Not this person',290,252)
            corrected=wait(lambda:result('person_remove'),'Face correction missing'); assert not corrected['photos']
            time.sleep(.6);control('All people',345,208)
            grouped=wait(lambda:next((r for q,r in reversed(calls) if q.get('view')=='people' and not q.get('person') and len(r.get('albums',[]))==3),None),'Corrected groups missing')
            time.sleep(.6);control('Unnamed person',450,328)
            time.sleep(.5);control('Name or merge',670,208);text('Grace');control('Save',250,350)
            wait(lambda:any(r.get('person_detail',{}).get('name')=='Grace' for q,r in calls),'Second name missing')
            time.sleep(.6);control('Ignore person',990,208)
            ignored=wait(lambda:result('person_ignore'),'Ignore missing');assert ignored['person_detail']['ignored']
            time.sleep(.5);control('All people',345,208);time.sleep(.5)
            control('Show ignored',275,278)
            ignored_groups=wait(lambda:next((r for q,r in reversed(calls) if q.get('people_mode')=='ignored' and r.get('albums')),None),'Ignored group missing')
            assert ignored_groups['albums'][0]['name']=='Grace'
            time.sleep(.5);control('Grace',450,328);time.sleep(.5);control('Unignore person',990,208)
            wait(lambda:any(q.get('action')=='person_ignore' and not r.get('person_detail',{}).get('ignored',1) for q,r in calls),'Unignore missing')
            time.sleep(.5);control('All people',345,208);time.sleep(.5);control('Show active',275,278);time.sleep(.5)
            control('Disable People',630,234)
            disabled=wait(lambda:result('people_disable'),'Disable missing');assert not disabled['people_enabled'] and not disabled['albums']
            # Tag a photo with no recognized faces, then tag a mixed selection.
            # Recognition stays paused while exercising the real UI/service.
            library.set_setting('people_enabled',True)
            control('Photos',90,86);time.sleep(.6)
            control('Photo 4.png',310,385)
            wait(lambda:result('open'),'Manual photo did not open');time.sleep(.5)
            control('Tag person',1097,617);text('Ada');control('Save',250,350)
            tagged=wait(lambda:result('person_tag'),'Single person tag missing')
            assert tagged['detail']['id']==ids[4] and tagged['detail']['people']=='People: Ada'
            assert not library.db.execute('SELECT 1 FROM faces WHERE photo_id=?',(ids[4],)).fetchone()
            time.sleep(.6);shoot('people-manual-portable.png' if portable else 'people-manual-native.png')
            control('Remove person tag',1097,647);text('Ada');control('Save',250,350)
            removed=wait(lambda:result('person_untag'),'Single untag missing');assert removed['detail']['people']==''
            time.sleep(.5);control('Back',235,166);time.sleep(.5)
            control('Select',245,166);time.sleep(.4);control('Select loaded',355,166);time.sleep(.4)
            start=len(calls);control('Tag person',926,254);text('Ada');control('Save',250,350)
            tagged=wait(lambda:result('person_tag',start),'Selection tag missing')
            tag_request=next(q for q,r in calls[start:] if q.get('action')=='person_tag')
            assert set(tag_request['ids'])=={ids[0],ids[1],ids[2],ids[4]}
            assert library.query(search='person:Ada')['count']==4
            time.sleep(.5);control('People',90,698)
            wait(lambda:next((r for q,r in reversed(calls) if q.get('view')=='people' and any(a['name']=='Ada' and a['count']==4 for a in r.get('albums',[]))),None),'Manually tagged person catalog missing')
            time.sleep(.5);control('Ada',450,328);time.sleep(.5)
            control('Select',245,332);time.sleep(.4);control('Select loaded',355,332);time.sleep(.4)
            start=len(calls);control('Not this person',290,252)
            corrected=wait(lambda:result('person_remove',start),'Manual people correction missing');assert not corrected['photos']
            assert library.query(search='person:Ada')['count']==0
            assert library.db.execute('SELECT count(*) FROM faces').fetchone()[0]==3
            for path,data in originals.items(): assert path.read_bytes()==data
            assert library.query()['total']==5
            print(('Portable' if portable else 'Native')+' People: recognition controls, single and selection manual tagging, untagging, catalog and correction, private exclusion and unchanged originals passed')

        finally:
            if sys.exc_info()[0] is not None:
                if 'shoot' in locals() and child.poll() is None:
                    try: shoot('people-failed-portable.png' if portable else 'people-failed-native.png')
                    except (OSError, subprocess.SubprocessError): pass
                print('Last synthetic requests:', [(q.get('action'), q.get('view'), q.get('search'), r.get('error')) for q,r in calls[-5:]])
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            if child.returncode not in (0, -15): print('People process exit:', child.returncode, child.stderr.read().decode()[-4000:])
            service.shutdown(); library.db.close()
