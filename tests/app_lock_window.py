"""Opening/manual app lock and service loss on synthetic private windows."""
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
from photos.library import Library
from photos import app_lock, vault
from photos.service import Server
from photos.sync import Client
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_APP_LOCK_PORTABLE') == '1'
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
    originals = {}
    for i in range(3):
        path = source/f'Photo {i}.png'; Image.new('RGB', (300, 200), (70+i*50, 150, 100)).save(path)
        originals[path] = path.read_bytes()
    library = Library(folder/'library'); library.import_folders([source.parent])
    Client(library, dict(node='http://127.0.0.1:1', node_id='42'*32))
    keys = (library.root/'private/identity.json').read_bytes()
    password = 'synthetic-app-lock-password'; hidden = vault.authenticate(library, password, setup=True)
    calls = []; stall = threading.Event(); entered = threading.Event(); release = threading.Event()
    original_process = service_module.process
    def record(request, config):
        if request.get('action') == 'app_lock_check' and stall.is_set():
            entered.set(); release.wait(10)
        result = original_process(request, config)
        calls.append((dict(action=request.get('action')), result))
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
            window = wait(locate, 'App lock test app did not open')
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
                                 if (c['label'] == label and c['control'] != '41' if number is None else c['control'] == str(number)) and c['enabled']), None)
                item = wait(ready, 'Missing app lock control: '+label)
                x, y = item['x']+item['width']/2, item['y']+item['height']/2
            click(x, y)
        def result(action, since=0):
            return next((r for q, r in reversed(calls[since:]) if q['action'] == action), None)
        def text(value, y=158):
            control('Password', 400, y, number=41)
            xdo('keydown', 'ctrl'); xdo('key', 'a'); time.sleep(.1); xdo('keyup', 'ctrl')
            xdo('type', '--clearmodifiers', '--delay', '15', '--', value); time.sleep(.2)
        def shoot(name):
            subprocess.run(['import', '-window', window, str(output/name)], check=True, timeout=5)
        def unlock():
            since = len(calls); text(password); control('Unlock Photos', 108, 226)
            wait(lambda: result('app_lock_unlock', since) and result('app_lock_unlock', since).get('app_lock_authorized') == 1, 'App password was not accepted')
            if not portable: wait(lambda: not state().get('app_locked'), 'Owner controls did not return')
            time.sleep(.5)
        try:
            launch(); wait(lambda: result('query') and result('query')['total'] == 3, 'Owner gallery did not load')
            control('App lock', 95, 781); control('Turn on app lock', 114, 222)
            text(password, 172); control('Enable app lock', 114, 236)
            wait(lambda: result('app_lock_enable') and result('app_lock_enable')['app_locked'] == 1, 'App lock did not enable')
            assert not vault.unlocked(library, hidden)
            suffix = 'portable' if portable else 'native'; shoot('app-lock-'+suffix+'.png')
            since = len(calls); text('incorrect-password'); control('Unlock Photos', 108, 226)
            failed = wait(lambda: result('app_lock_unlock', since), 'Wrong app password was not checked')
            assert failed['app_locked'] == 1 and 'Incorrect password' in failed['error']
            if not portable:
                current = state(); assert current['nodes'] == 0 and current['selection_count'] == 0
                assert not {c['label'] for c in current['controls']}.intersection({'Photos', 'Albums', 'Hidden', 'Import', 'Export'})
                assert password not in json.dumps(current)
            unlock(); stop(); launch()
            wait(lambda: result('query') and result('query')['app_locked'] == 1, 'Restart did not require app authentication')
            unlock(); control('App lock', 95, 781); control('Lock now', 98, 222)
            wait(lambda: result('app_lock_lock') and result('app_lock_lock')['app_locked'] == 1, 'Manual lock did not finish')
            unlock()
            # A stalled permission heartbeat must clear the native viewer, and
            # returning that stale answer must not silently unlock the app.
            stall.set(); wait(entered.is_set, 'App lock heartbeat did not start')
            if not portable:
                wait(lambda: state().get('app_locked') and state()['nodes'] == 0, 'Expired app access left owner photos displayed')
            else: time.sleep(3)
            shoot('app-lock-stalled-'+suffix+'.png')
            release.set(); stall.clear(); time.sleep(.8)
            if not portable: assert state()['app_locked'], 'A stale heartbeat silently unlocked Photos'
            unlock(); control('App lock', 95, 781); control('Turn off app lock', 278, 222)
            text(password, 172); control('Disable app lock', 114, 236)
            wait(lambda: result('app_lock_disable') and result('app_lock_disable')['app_lock_enabled'] == 0, 'App lock did not disable')
            assert not app_lock.enabled(library)
            assert (library.root/'private/identity.json').read_bytes() == keys
            for path, data in originals.items(): assert path.read_bytes() == data
            print('Private Xvfb '+suffix+' app lock: enable, masked password, wrong password, restart, manual lock, stalled heartbeat, disable and originals/keys passed')
        except Exception:
            if child is not None and child.poll() is None:
                shoot('app-lock-failed-'+('portable' if portable else 'native')+'.png')
                print('App lock requests:', [(q['action'], r.get('error'), r.get('app_locked')) for q, r in calls])
                if not portable: print('App lock window state:', state())
            raise
        finally:
            release.set(); stop(); service.shutdown(); library.db.close()
