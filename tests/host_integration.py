"""Real Photos and the full Harmony host, with owned fixtures/display only.

All source stays in canonical checkouts. Temporary project directories hold
generated executables, synthetic media and runtime state, never source copies.
"""
import argparse
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from photos.library import Library
from photos.service import Server
import photos.service as service_module


def stop(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait(predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(message)


def verify(mode, args, output):
    excluded = ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS',
                'SESSION_MANAGER', 'LD_PRELOAD')
    for name in excluded[:4]:
        assert not os.environ.get(name), name + ' must be scrubbed before verification'
    env = {key: value for key, value in os.environ.items()
           if key not in excluded and not key.startswith(('PHOTOS_', 'KRYON_', 'HARMONY_', 'YUE_'))}
    env.update(YUE_DESKTOP_RECOVERY='0', APP_NO_TRAY='1', NOTIFICATIONS_DISABLED='1',
               SDL_AUDIODRIVER='dummy', SDL_VIDEODRIVER='x11', LIBGL_ALWAYS_SOFTWARE='1',
               HARMONY_PREVIEW_CHAT_BUBBLES='1')
    sys.path.insert(0, str(args.harmony / 'tests'))
    from app_packages_test import Node, signed_release
    with tempfile.TemporaryDirectory(prefix='photos-full-host-') as directory:
        root = Path(directory)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        env.update(HOME=str(root), PROJECTS_ROOT=str(root / 'Projects'),
                   XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=str(root / 'config'),
                   XDG_DATA_HOME=str(root / 'data'), XDG_CACHE_HOME=str(root / 'cache'),
                   XDG_STATE_HOME=str(root / 'state'), KRYON_SERVICE_ROOT=str(runtime),
                   HARMONY_APP_CONTROL_ROOT=str(root / 'control'),
                   HARMONY_APP_CONFIG=str(root / 'apps.json'))
        # Copy only built runtime artifacts, leaving the owner's binaries alone.
        host_dir = root / 'host'
        host_dir.mkdir()
        for source in (args.host, args.host.parent / 'libapp-start.so', args.player, args.updater):
            name = 'zib' if source == args.player else source.name
            shutil.copy2(source, host_dir / name)
        config = dict(app_id='photos', label='Photos', renderer='window',
                      project='waozixyz/photos', executable='photos', build_path='build/photos-desktop')
        source = root / 'source'
        source.mkdir()
        originals = {}
        for index in range(90):
            path = source / ('Synthetic-%03d.png' % index)
            Image.new('RGB', (700 + index, 500), (67, 142, 124)).save(path)
            originals[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        library = Library(root / 'library')
        library.import_folders([source])
        endpoint = runtime / 'kryon/services/photos.sock'
        endpoint.parent.mkdir(parents=True, mode=0o700)
        requests = []
        original_process = service_module.process

        def process(request, settings):
            requests.append(dict(request))
            return original_process(request, settings)

        service_module.process = process
        node = display = host = foreign = None
        child_pids = {}
        counter = 0
        states = []
        viewports = []
        overlay = {}
        with (output / (mode + '.log')).open('w') as log:
            try:
                if mode == 'portable':
                    key = Ed25519PrivateKey.generate()
                    node = Node('photos')
                    payload = args.bundle.read_bytes()
                    node.latest = signed_release(key, payload, 1, app_id='photos', version='0.1.58')
                    node.files[node.latest['sha256'] + '.zib'] = payload
                    config.update(bundle_mode='portable', key_id='release',
                                  public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex())
                    (root / 'apps.json').write_text(json.dumps(dict(node=node.url, apps=[config])))
                    subprocess.run([str(host_dir / args.updater.name), '--install', str(root / 'apps.json'),
                                    str(root / 'data/harmony/apps'), 'photos'], env=env,
                                   stdout=log, stderr=log, check=True, timeout=30)
                    installed = root / 'data/harmony/apps/photos/current.json'
                    assert json.loads(installed.read_text())['sha256'] == hashlib.sha256(payload).hexdigest()
                else:
                    executable = root / 'Projects/waozixyz/photos/build/photos-desktop'
                    executable.parent.mkdir(parents=True)
                    shutil.copy2(args.native, executable)
                    config['bundle_mode'] = 'app-host'
                    (root / 'apps.json').write_text(json.dumps(dict(apps=[config])))
                display = subprocess.Popen(['Xvfb', '-displayfd', '1', '-screen', '0', '1600x1000x24',
                                            '-nolisten', 'tcp'], env=env, stdout=subprocess.PIPE, stderr=log)
                number = display.stdout.readline().decode().strip()
                display.stdout.close()
                assert number.isdecimal(), 'Owned display did not start'
                env['DISPLAY'] = ':' + number

                def xdo(*arguments):
                    return subprocess.check_output(['xdotool', *map(str, arguments)], env=env, text=True).strip()

                def window(pid):
                    result = subprocess.run(['xdotool', 'search', '--pid', str(pid)], env=env,
                                            capture_output=True, text=True, timeout=3)
                    return result.stdout.splitlines()[0] if result.returncode == 0 else None

                def geometry(target):
                    text = subprocess.check_output(['xwininfo', '-id', str(target)], env=env, text=True)
                    return {key: int(re.search(pattern, text)[1]) for key, pattern in {
                        'x': r'Relative upper-left X:\s*(-?\d+)', 'y': r'Relative upper-left Y:\s*(-?\d+)',
                        'width': r'Width:\s*(\d+)', 'height': r'Height:\s*(\d+)'}.items()}

                def parent(target):
                    text = subprocess.check_output(['xwininfo', '-id', str(target), '-tree'], env=env, text=True)
                    return re.search(r'Parent window id:\s*(0x[0-9a-f]+)', text)[1]

                def shape_contains(target, x, y):
                    class Rectangle(c.Structure):
                        _fields_ = [('x', c.c_short), ('y', c.c_short),
                                    ('width', c.c_ushort), ('height', c.c_ushort)]
                    x11 = c.CDLL('libX11.so.6')
                    extension = c.CDLL('libXext.so.6')
                    x11.XOpenDisplay.argtypes = [c.c_char_p]
                    x11.XOpenDisplay.restype = c.c_void_p
                    x11.XCloseDisplay.argtypes = [c.c_void_p]
                    x11.XFree.argtypes = [c.c_void_p]
                    extension.XShapeGetRectangles.argtypes = [c.c_void_p, c.c_ulong, c.c_int,
                                                              c.POINTER(c.c_int), c.POINTER(c.c_int)]
                    extension.XShapeGetRectangles.restype = c.POINTER(Rectangle)
                    connection = x11.XOpenDisplay(env['DISPLAY'].encode())
                    assert connection, 'Owned shape query failed'
                    try:
                        count = c.c_int()
                        ordering = c.c_int()
                        rectangles = extension.XShapeGetRectangles(connection, int(target, 0), 0,
                                                                   c.byref(count), c.byref(ordering))
                        try:
                            return any(rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height
                                       for rect in rectangles[:count.value])
                        finally:
                            x11.XFree(rectangles)
                    finally:
                        x11.XCloseDisplay(connection)

                foreign = subprocess.Popen([str(args.fixture)], cwd=root, env=env, stdout=log, stderr=log)
                foreign_window = wait(lambda: window(foreign.pid), 'Owned foreign fixture did not open')
                xdo('windowmove', foreign_window, 1300, 850)
                foreign_geometry = geometry(foreign_window)
                foreign_parent = parent(foreign_window)
                with Server(dict(root=str(library.root), sources=[]), endpoint) as server:
                    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                    server_thread.start()
                    host = subprocess.Popen([str(host_dir / args.host.name), '--preview', '--no-tray'],
                                            cwd=root, env=env, stdout=log, stderr=log)
                    socket_path = root / 'control/control.sock'
                    wait(socket_path.exists, 'Full private Harmony host did not expose control')
                    host_window = wait(lambda: window(host.pid), 'Private Harmony window did not open')
                    time.sleep(.7)
                    xdo('windowsize', host_window, 1180, 880)

                    def control(action, app='photos', **fields):
                        nonlocal counter
                        counter += 1
                        request = dict(action=action, app=app, **fields)
                        if action == 'open':
                            request['request_id'] = 'owned-photos-%s-%d' % (mode, counter)
                        with socket.socket(socket.AF_UNIX) as peer:
                            peer.settimeout(5)
                            peer.connect(str(socket_path))
                            peer.sendall(json.dumps(request).encode() + b'\n')
                            data = b''
                            while b'\n' not in data:
                                block = peer.recv(262144)
                                assert block, 'Private host closed its reply'
                                data += block
                        return json.loads(data)

                    row = next(row for row in control('catalog')['apps'] if row['id'] == 'photos')
                    assert row['available'], row
                    opened = control('open')
                    assert opened['ok'], opened

                    def ready():
                        state = control('state')
                        states.append(state)
                        return state if state.get('ready') and state.get('selected') and state.get('window') else None

                    initial = wait(ready, 'Photos failed full-host navigation readiness')
                    child = initial['window']
                    child_pid = int(xdo('getwindowpid', child))
                    child_pids[child_pid] = Path('/proc/%d/stat' % child_pid).read_text().rsplit(')', 1)[1].split()[19]
                    assert parent(parent(child)) == hex(int(host_window))
                    wait(lambda: any(request.get('action') == 'query' for request in requests),
                         'Hosted Photos did not query the isolated library')

                    def painted():
                        capture = output / (mode + '-gallery.png')
                        subprocess.run(['import', '-window', str(child), str(capture)], env=env,
                                       check=True, timeout=5)
                        with Image.open(capture) as image:
                            pixel = image.convert('RGB').getpixel((260, 300))
                        return max(abs(a - b) for a, b in zip(pixel, (67, 142, 124))) <= 4

                    wait(painted, 'Actual Photos did not paint the synthetic thumbnail inside the full host')
                    time.sleep(.5)
                    assert ready(), 'Host navigation did not remain selected'
                    container = parent(child)
                    for width in (960, 1280, 800):
                        xdo('windowsize', host_window, width, 880)

                        def rail_reserved():
                            pane = geometry(container)
                            return pane if pane['x'] + pane['width'] == width - 76 else None

                        pane = wait(rail_reserved,
                             'Right chat rail was not reserved while hosting Photos')
                        assert geometry(child)['width'] == pane['width']
                        viewports.append(dict(width=width, pane=pane, right_rail=76))
                        assert ready(), 'Resize lost the selected Photos view'
                        subprocess.run(['import', '-window', host_window, str(output / (mode + '-%d.png' % width))],
                                       env=env, check=True, timeout=5)
                    xdo('windowsize', host_window, 1180, 880)

                    def viewport_restored():
                        pane = geometry(container)
                        return pane['x'] + pane['width'] == 1104 and geometry(child)['width'] == pane['width']

                    wait(viewport_restored, 'Host did not restore its viewport')
                    pane = geometry(container)
                    settled = geometry(child)
                    host_size = geometry(host_window)
                    overlay_x = host_size['width'] - 180 - pane['x']
                    overlay_y = host_size['height'] - 180 - pane['y']
                    assert shape_contains(container, overlay_x, overlay_y)
                    navigation = pane['y'] if mode == 'native' else pane['y'] - 64
                    xdo('mousemove', '--window', host_window, host_size['width'] - 43,
                        39 + navigation, 'click', '1')
                    wait(lambda: not shape_contains(container, overlay_x, overlay_y),
                         'Right chat bubble did not expose an interactive mini chat over Photos')
                    overlay.update(before=settled, opened=geometry(child), state=control('state'))
                    assert overlay['opened'] == settled and ready(), 'Mini chat changed Photos geometry or navigation'
                    assert Path('/proc/%d' % child_pid).exists(), 'Partly visible Photos was stopped'
                    subprocess.run(['import', '-window', host_window, str(output / (mode + '-mini-chat.png'))],
                                   env=env, check=True, timeout=5)
                    xdo('mousemove', '--window', host_window, host_size['width'] - 106,
                        max(12, host_size['height'] - 560 - 12) + 22, 'click', '1')
                    wait(lambda: shape_contains(container, overlay_x, overlay_y),
                         'Closing the mini chat did not restore Photos input surface')
                    # The preview fixture has several unread chat bubbles.
                    # Reading one retains the rail for the other bubbles after
                    # the mini chat closes; the child must keep its viewport.
                    closed = wait(lambda: geometry(child) if geometry(child) == settled else None,
                                  'Closing one mini chat changed Photos geometry while other bubbles remain')
                    overlay['closed'] = closed
                    assert ready(), 'Closing the mini chat lost Photos navigation'
                    # Navigation replaces the app view; the idle child must disappear.
                    assert control('open', 'devices')['ok']
                    wait(lambda: not Path('/proc/%d' % child_pid).exists(),
                         'Unviewed Photos remained running in the full host')
                    assert not control('state').get('running'), 'Catalog retained a stopped child'
                    assert control('open')['ok']
                    reopened = wait(ready, 'Photos did not reopen after full-host reclamation')
                    reopened_pid = int(xdo('getwindowpid', reopened['window']))
                    child_pids[reopened_pid] = Path('/proc/%d/stat' % reopened_pid).read_text().rsplit(')', 1)[1].split()[19]
                    assert reopened_pid != child_pid
                    assert foreign.poll() is None
                    assert geometry(foreign_window) == foreign_geometry and parent(foreign_window) == foreign_parent
                    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == value
                               for path, value in originals.items())
                    # Preserve the existing narrower pending Save/draft gate;
                    # this check adds full navigation and real right-rail integration.
                    assert control('open', 'devices')['ok']
                    wait(lambda: not Path('/proc/%d' % reopened_pid).exists(), 'Reopened idle Photos did not stop')
                server_thread.join(5)
                assert not server_thread.is_alive()
                return dict(mode=mode, full_host_navigation=True, selected_ready_stable=True,
                            right_rail_widths=[960, 1280, 800], hidden_process_reaped=True,
                            mini_chat_input_exposed=True, overlay_geometry_stable=True,
                            partly_visible_app_preserved=True,
                            reopened=True, originals_preserved=True, foreign_window_untouched=True,
                            signed_bundle_installed_privately=mode == 'portable', deployment=False)
            finally:
                (output / (mode + '-states.json')).write_text(json.dumps(states, indent=2) + '\n')
                (output / (mode + '-viewports.json')).write_text(json.dumps(viewports, indent=2) + '\n')
                (output / (mode + '-overlay.json')).write_text(json.dumps(overlay, indent=2) + '\n')
                # Host and child processes all belong to this isolated fixture.
                stop(host)
                for pid, started in child_pids.items():
                    try:
                        if Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[19] == started:
                            os.kill(pid, 15)
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                stop(foreign)
                stop(display)
                if node is not None:
                    node.close()
                service_module.process = original_process
                library.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--harmony', type=Path, required=True)
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--updater', type=Path, required=True)
    parser.add_argument('--player', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--native', type=Path, default=ROOT / 'build/photos-desktop')
    parser.add_argument('--bundle', type=Path, default=ROOT / 'build/photos-portable.zib')
    parser.add_argument('--mode', choices=('native', 'portable', 'both'), default='both')
    args = parser.parse_args()
    for name in ('harmony', 'host', 'updater', 'player', 'fixture', 'native', 'bundle'):
        setattr(args, name, getattr(args, name).resolve())
    output = ROOT / 'build/host-integration'
    output.mkdir(parents=True, exist_ok=True)
    modes = ('native', 'portable') if args.mode == 'both' else (args.mode,)
    paths = (args.host, args.host.parent / 'libapp-start.so', args.updater,
             args.player, args.native, args.bundle, Path(__file__))
    inputs = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    receipt = dict(status='running', results=[], private_display=True, synthetic_media=True,
                   deployment=False, full_host_build='existing built artifact',
                   current_full_host_source_verified=False, inputs=inputs)

    def save():
        pending = output / 'receipt.pending'
        pending.write_text(json.dumps(receipt, indent=2) + '\n')
        pending.replace(output / 'receipt.json')

    save()
    try:
        for mode in modes:
            receipt['results'].append(verify(mode, args, output))
            save()
        current = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        receipt['source_drift'] = [path for path in inputs if inputs[path] != current[path]]
        assert not receipt['source_drift'], 'Host verification inputs changed'
        receipt['status'] = 'passed'
        receipt['owned_processes_stopped'] = True
        save()
    except Exception as error:
        receipt['status'] = 'failed'
        receipt['error'] = str(error)
        receipt['owned_processes_stopped'] = True
        save()
        raise
    print('Full private Harmony host: actual Photos navigation, right rail, hidden exit/reopen and preservation passed')


if __name__ == '__main__':
    main()
