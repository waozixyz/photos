"""Opening/manual app lock and service loss on synthetic private windows."""
import ast
import ctypes as c
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
hosted = os.environ.get('PHOTOS_APP_LOCK_HOSTED') == '1'
assert not hosted or portable
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
    hidden_settings = (library.root/'private/hidden-password.json').read_bytes()
    calls = []; stall = threading.Event(); entered = threading.Event(); release = threading.Event()
    auth_stall = threading.Event(); auth_entered = threading.Event(); auth_release = threading.Event()
    original_process = service_module.process
    def record(request, config):
        if request.get('action') == 'app_lock_check' and stall.is_set():
            entered.set(); release.wait(10)
        result = original_process(request, config)
        if request.get('action') == 'app_lock_unlock' and auth_stall.is_set():
            auth_entered.set(); auth_release.wait(8)
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
            if hosted:
                root_window = c.c_ulong(); parent_window = c.c_ulong(); children = c.POINTER(c.c_ulong)(); count = c.c_uint()
                assert x.XQueryTree(connection,int(window),c.byref(root_window),c.byref(parent_window),c.byref(children),c.byref(count))
                if children: x.XFree(children)
                assert parent_window.value == container, 'Photos opened outside its Harmony container'
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
        def unlock(credential=None):
            since = len(calls); text(password if credential is None else credential); control('Unlock Photos', 108, 226)
            wait(lambda: result('app_lock_unlock', since) and result('app_lock_unlock', since).get('app_lock_authorized') == 1, 'App password was not accepted')
            if not portable: wait(lambda: not state().get('app_locked'), 'Owner controls did not return')
            time.sleep(.5)
        # This focus target belongs to the harness on its private display.
        x = c.CDLL('libX11.so.6')
        x.XOpenDisplay.restype = c.c_void_p
        x.XDefaultRootWindow.argtypes = [c.c_void_p]; x.XDefaultRootWindow.restype = c.c_ulong
        x.XCreateSimpleWindow.argtypes = [c.c_void_p,c.c_ulong,c.c_int,c.c_int,c.c_uint,c.c_uint,c.c_uint,c.c_ulong,c.c_ulong]
        x.XCreateSimpleWindow.restype = c.c_ulong
        x.XMapWindow.argtypes = [c.c_void_p,c.c_ulong]
        x.XSetInputFocus.argtypes = [c.c_void_p,c.c_ulong,c.c_int,c.c_ulong]
        x.XDestroyWindow.argtypes = [c.c_void_p,c.c_ulong]
        x.XInternAtom.argtypes = [c.c_void_p,c.c_char_p,c.c_int]; x.XInternAtom.restype = c.c_ulong
        x.XChangeProperty.argtypes = [c.c_void_p,c.c_ulong,c.c_ulong,c.c_ulong,c.c_int,c.c_int,c.c_void_p,c.c_int]
        x.XQueryTree.argtypes = [c.c_void_p,c.c_ulong,c.POINTER(c.c_ulong),c.POINTER(c.c_ulong),c.POINTER(c.POINTER(c.c_ulong)),c.POINTER(c.c_uint)]
        x.XFree.argtypes = [c.c_void_p]
        x.XFlush.argtypes = [c.c_void_p]; x.XCloseDisplay.argtypes = [c.c_void_p]
        connection = x.XOpenDisplay(None); assert connection
        secondary = x.XCreateSimpleWindow(connection,x.XDefaultRootWindow(connection),1200,900,100,80,0,0,0)
        x.XMapWindow(connection,secondary); x.XFlush(connection)
        parent = None
        if hosted:
            parent = x.XCreateSimpleWindow(connection,x.XDefaultRootWindow(connection),0,0,1180,880,0,0,0)
            container = x.XCreateSimpleWindow(connection,parent,0,0,1180,880,0,0,0)
            pid = (c.c_ulong*1)(os.getpid())
            x.XChangeProperty(connection,parent,x.XInternAtom(connection,b'_NET_WM_PID',0),6,32,0,pid,1)
            x.XMapWindow(connection,parent); x.XMapWindow(connection,container); x.XFlush(connection)
            loader = Path(os.environ.get('PHOTOS_APP_START',Path.home()/'.local/lib/harmony/libapp-start.so'))
            assert loader.is_file()
            env.update(HARMONY_APP_HOST_PID=str(os.getpid()),HARMONY_APP_CONTAINER=str(container),LD_PRELOAD=str(loader),SDL_VIDEODRIVER='x11')
        def background():
            x.XSetInputFocus(connection,secondary,1,0); x.XFlush(connection)
        def foreground():
            xdo('windowfocus', window); time.sleep(.25)
        def delay(label, seconds, x_position):
            control('App lock',95,781)
            since = len(calls); control(label,x_position,398)
            wait(lambda: result('app_lock_delay',since) and result('app_lock_delay',since).get('app_lock_delay') == seconds,
                 'Background delay did not save: '+label)
            time.sleep(.3); assert app_lock.settings(library)['auto_lock'] == seconds
            shoot('app-lock-settings-'+suffix+'.png')
        try:
            launch(); wait(lambda: result('query') and result('query')['total'] == 3, 'Owner gallery did not load')
            control('App lock', 95, 781); control('Turn on app lock', 114, 222)
            text(password, 172); control('Enable app lock', 114, 236)
            wait(lambda: result('app_lock_enable') and result('app_lock_enable')['app_locked'] == 1, 'App lock did not enable')
            assert not vault.unlocked(library, hidden)
            suffix = 'hosted' if hosted else ('portable' if portable else 'native'); shoot('app-lock-'+suffix+'.png')
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
            delay('5 seconds',5,590); since = len(calls)
            background(); time.sleep(1); foreground(); time.sleep(4.5)
            assert not result('app_lock_lock',since), 'Returning before the grace deadline still locked Photos'
            if not portable: assert not state()['app_locked']
            background()
            wait(lambda: result('app_lock_lock',since), 'The background deadline did not revoke app access')
            assert not any(entry[0] == str(library.root) for entry in app_lock.sessions.values())
            if not portable:
                wait(lambda: state()['app_locked'] and state()['nodes'] == 0, 'Background expiry retained owner photos')
            foreground(); shoot('app-lock-background-'+suffix+'.png'); unlock()
            delay('Immediately',0,215); since = len(calls)
            background()
            wait(lambda: result('app_lock_lock',since), 'Immediate focus loss did not lock Photos')
            foreground(); unlock()
            # An auth answer arriving while backgrounded must not reopen photos.
            control('App lock',95,781); since = len(calls); control('Lock now',98,222)
            wait(lambda: result('app_lock_lock',since), 'Manual lock did not complete')
            since = len(calls); auth_stall.set(); text(password); control('Unlock Photos',108,226)
            wait(auth_entered.is_set,'Delayed app authentication did not start')
            background(); auth_release.set(); auth_stall.clear()
            wait(lambda: result('app_lock_lock',since), 'Background authentication was not revoked')
            foreground(); time.sleep(.5)
            if not portable: assert state()['app_locked'] and state()['nodes'] == 0
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
            # A distinct credential must be confirmed without changing Hidden.
            control('App lock', 95, 781); control('Set app PIN', 108, 278)
            text('0042', 172); control('Continue', 108, 236)
            text('0043', 172); control('Save PIN', 108, 236)
            assert not app_lock.enabled(library), 'Mismatched confirmation enabled app lock'
            text('0042', 172); control('Save PIN', 108, 236)
            wait(lambda: result('app_lock_pin') and result('app_lock_pin')['app_lock_method'] == 1, 'Separate PIN did not enable')
            shoot('app-pin-'+suffix+'.png'); unlock('0042'); stop(); launch()
            wait(lambda: result('query') and result('query')['app_locked'] == 1, 'PIN restart did not lock')
            unlock('0042'); control('App lock', 95, 781); control('Change app password', 310, 278)
            credential = 'Separate synthetic app password'
            text(credential, 172); control('Continue', 108, 236)
            text(credential, 172); control('Save password', 108, 236)
            wait(lambda: result('app_lock_password') and result('app_lock_password')['app_lock_method'] == 2, 'Separate password did not enable')
            since = len(calls); text(password); control('Unlock Photos', 108, 226)
            failed = wait(lambda: result('app_lock_unlock', since), 'Previous credential was not checked')
            assert failed['app_locked'] == 1 and 'Incorrect password' in failed['error']
            unlock(credential); control('App lock', 95, 781); control('Turn off app lock', 278, 222)
            text(credential, 172); control('Disable app lock', 108, 236)
            wait(lambda: not app_lock.enabled(library), 'Separate password did not disable')
            assert (library.root/'private/hidden-password.json').read_bytes() == hidden_settings
            assert (library.root/'private/identity.json').read_bytes() == keys
            for path, data in originals.items(): assert path.read_bytes() == data
            print('Private Xvfb '+suffix+' app lock: masked fields, restart, manual lock, background immediate/grace deadlines, early focus return, delayed authentication, stalled heartbeat, confirmed separate PIN/password and originals/keys passed')
        except Exception:
            if child is not None and child.poll() is None:
                shoot('app-lock-failed-'+('portable' if portable else 'native')+'.png')
                print('App lock requests:', [(q['action'], r.get('error'), r.get('app_locked')) for q, r in calls])
                if not portable: print('App lock window state:', state())
            raise
        finally:
            release.set(); auth_release.set(); stop(); service.shutdown(); library.db.close()
            x.XDestroyWindow(connection,secondary)
            if parent is not None: x.XDestroyWindow(connection,parent)
            x.XCloseDisplay(connection)
