"""Guest handover and authenticated exit on this test's private display."""
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
sys.path.insert(0, os.environ.get('PHOTOS_SERVICE_PACKAGE', str(ROOT)))
from photos.library import Library, digest_file
from photos import guest, vault
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_GUEST_PORTABLE') == '1'
output = ROOT/'build/screenshots'; output.mkdir(exist_ok=True)


def wait(check, reason, timeout=20):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = check()
        if value: return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as temporary:
    folder = Path(temporary); source = folder/'source/Family'; source.mkdir(parents=True)
    ids = []; originals = {}
    for i in range(8):
        path = source/f'Photo {i}.png'; Image.new('RGB', (300+i, 200), (45+i*20, 150, 100)).save(path)
        ids.append(digest_file(path)); originals[path] = path.read_bytes()
    library = Library(folder/'library'); library.import_folders([source.parent])
    for identity in ids: library.edit(identity, 'taken', '2025-06-15T12:00:00Z')
    library.edit(ids[6], 'hidden_set', '1'); library.edit(ids[7], 'trash')
    password = 'private-guest-test-password'; vault.authenticate(library, password, setup=True)
    calls = []; original_process = service_module.process
    def record(request, config):
        result = original_process(request, config)
        calls.append((dict(action=request.get('action'), id=request.get('id'), ids=request.get('ids')), result))
        return result
    service_module.process = record
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder)}
    if portable:
        flags = [] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *flags], cwd=ROOT, text=True).strip())
        command = [os.environ.get('PHOTOS_PLAYER', str(kryon/'build/bin/zib')),
                   os.environ.get('PHOTOS_BUNDLE', str(ROOT/'build/photos-portable.zib'))]
    else:
        command = [os.environ.get('PHOTOS_BINARY', str(ROOT/'build/photos-desktop'))]
    child = None; window = None
    with Server(dict(root=str(library.root)), folder/'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever, daemon=True).start()
        def xdo(*arguments):
            return subprocess.check_output(['xdotool', *map(str, arguments)], text=True).strip()
        def state():
            raw = subprocess.check_output(['xprop', '-id', window, '_HARMONY_APP_STATE'], text=True)
            return json.loads(ast.literal_eval(raw.split(' = ', 1)[1].strip())) if ' = ' in raw else {}
        def launch():
            global child, window
            child = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            def locate():
                assert child.poll() is None, child.stderr.read().decode()
                result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--pid', str(child.pid)], capture_output=True, text=True)
                return result.stdout.splitlines()[0] if result.returncode == 0 else None
            window = wait(locate, 'Guest test app did not open')
            xdo('windowsize', window, 1180, 880); xdo('windowmove', window, 0, 0); xdo('windowfocus', window)
            time.sleep(.5)
        def stop():
            if child is not None:
                if child.poll() is None: child.terminate()
                child.wait(timeout=5); child.stderr.close()
        def click(x, y):
            xdo('mousemove', '--window', window, int(x), int(y)); xdo('mousedown', 1); time.sleep(.12)
            xdo('mouseup', 1); time.sleep(.35)
        def control(label, x, y, number=None):
            if not portable:
                def ready():
                    current = state()
                    return next((c for c in current.get('controls', [])
                                 if (c['label'] == label if number is None else c['control'] == str(number)) and c['enabled']), None)
                item = wait(ready, 'Missing Guest control: '+label)
                x, y = item['x']+item['width']/2, item['y']+item['height']/2
            click(x, y)
        def result(action, since=0):
            return next((r for q, r in reversed(calls[since:]) if q['action'] == action), None)
        def text(value):
            control('Leave Guest view', 400, 140, number=41)
            xdo('keydown', 'ctrl'); xdo('key', 'a'); time.sleep(.1); xdo('keyup', 'ctrl')
            xdo('type', '--clearmodifiers', '--delay', '15', '--', value); time.sleep(.2)
        def shoot(name):
            subprocess.run(['import', '-window', window, str(output/name)], check=True, timeout=5)
        def owner():
            wait(lambda: (result('query') and result('query').get('guest_active') == 0) or
                 (result('guest_exit') and result('guest_exit').get('guest_exit_authorized') == 1),
                 'Owner gallery did not return')
            if not portable: wait(lambda: state() and not state().get('guest_active'), 'Native owner controls did not return')
            time.sleep(.5)
        def guest_ready(count):
            wait(lambda: result('query') and result('query').get('guest_active') == 1 and result('query')['count'] == count,
                 'Guest gallery did not load')
            if not portable:
                current = wait(lambda: state() if state().get('guest_active') else None, 'Native Guest latch missing')
                labels = {c['label'] for c in current['controls']}
                assert not labels.intersection({'Photos', 'Albums', 'Hidden', 'Trash', 'Import', 'Sync', 'Find', 'Favorite', 'Export', 'Tag person'})
                assert current['map_markers'] == 0 and current['selection_count'] == 0
        def leave():
            since = len(calls); control('Leave Guest view', 1086, 30); text(password)
            control('Authenticate', 90, 243)
            wait(lambda: result('guest_exit', since) and result('guest_exit', since).get('guest_exit_authorized') == 1, 'Guest exit failed')
            owner(); assert not guest.path(library).exists()
        try:
            launch(); owner()
            control('Open photo', 310, 350, number=100)
            wait(lambda: result('open'), 'Owner photo did not open')
            control('Guest view', 926, 853)
            wait(lambda: result('guest_start') and result('guest_start').get('guest_active') == 1, 'Single-photo Guest view did not start')
            guest_ready(1)
            suffix = 'portable' if portable else 'native'; shoot('guest-single-'+suffix+'.png')
            stop(); launch(); guest_ready(1)
            # Escape and the old sidebar position cannot reopen the owner view.
            xdo('key', 'Escape'); click(90, 137); guest_ready(1)
            since = len(calls); control('Leave Guest view', 1086, 30); text('incorrect-password')
            shoot('guest-password-'+suffix+'.png')
            control('Authenticate', 90, 243)
            failed = wait(lambda: result('guest_exit', since), 'Incorrect Guest password was not checked')
            assert failed['guest_active'] == 1 and 'Incorrect password' in failed['error']
            assert guest.path(library).is_file()
            text(password); control('Authenticate', 90, 243)
            wait(lambda: result('guest_exit', since).get('guest_exit_authorized') == 1, 'Correct Guest password was not accepted')
            owner()
            control('Select', 231, 166); control('Photo', 310, 350, number=100); control('Photo', 553, 350, number=101)
            since = len(calls); control('Guest view', 926, 853)
            selected = wait(lambda: result('guest_start', since), 'Selection Guest view did not start')
            assert selected['guest_active'] == 1 and selected['count'] == 2
            guest_ready(2); shoot('guest-selection-'+suffix+'.png')
            leave()
            control('Albums', 90, 137); control('Family', 420, 220)
            since = len(calls); control('Guest view', 926, 853)
            album = wait(lambda: result('guest_start', since), 'Album Guest view did not start')
            assert album['guest_active'] == 1 and album['count'] == 6
            guest_ready(6); shoot('guest-album-'+suffix+'.png')
            library.edit(ids[5], 'hidden_set', '1'); guest_ready(5)
            leave()
            assert library.query()['total'] == 8
            for path, data in originals.items(): assert path.read_bytes() == data
            print('Private Xvfb Guest '+suffix+': photo, selection, album, app restart, masked authenticated exit, privacy revocation and unchanged originals passed')
        except Exception:
            if child is not None and child.poll() is None: shoot('guest-failed-'+('portable' if portable else 'native')+'.png')
            raise
        finally:
            stop(); service.shutdown(); library.db.close()
