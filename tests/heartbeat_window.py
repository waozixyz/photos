"""Delay a real service heartbeat while editing on an owned private display."""
import ast
import ctypes as c
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from photos.library import Library, packed
from photos.service import receive
from photos.worker import process as library_request


def main():
    assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1', 'Use an owned private Xvfb display'
    with tempfile.TemporaryDirectory(prefix='photos-heartbeat-') as temporary:
        root = Path(temporary)
        source = root / 'source'
        source.mkdir()
        Image.new('RGB', (700, 500), '#438e7c').save(source / 'Synthetic.jpg')
        original = (source / 'Synthetic.jpg').read_bytes()
        library = Library(root / 'library')
        library.import_folders([source])
        config = dict(root=str(library.root), sources=[])
        configuration = root / 'config.json'
        configuration.write_text(json.dumps(config))
        endpoint = root / 'kryon/services/photos.sock'
        endpoint.parent.mkdir(parents=True, mode=0o700)
        stop = threading.Event()
        hold = threading.Event()
        pending = threading.Event()
        release = threading.Event()
        errors = []
        requests = []
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(endpoint))
        endpoint.chmod(0o600)
        listener.listen(8)
        listener.settimeout(.2)

        def serve():
            try:
                while not stop.is_set():
                    try:
                        peer, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with peer:
                        peer.settimeout(5)
                        length, = struct.unpack('!I', receive(peer, 4))
                        assert 0 < length <= 1048576
                        request = json.loads(receive(peer, length))
                        if request['action'] == 'keep_alive':
                            if hold.is_set():
                                pending.set()
                                assert release.wait(10), 'Heartbeat was not released'
                            result = {}
                        else:
                            requests.append(request)
                            result = library_request(request, config)
                        body = packed(result).encode()
                        peer.sendall(struct.pack('!I', len(body)) + body)
            except Exception as error:
                if not stop.is_set():
                    errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        environment = dict(os.environ, KRYON_SERVICE_ROOT=str(root), PHOTOS_CONFIG=str(configuration))
        portable = os.environ.get('PHOTOS_HEARTBEAT_PORTABLE') == '1'
        closing = os.environ.get('PHOTOS_CLOSE_DURING_HEARTBEAT') == '1'
        if portable:
            flags = [] if (ROOT / 'ziran.local.toml').exists() else ['--locked']
            kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *flags], cwd=ROOT, text=True).strip())
            command = [os.environ.get('PHOTOS_PLAYER', str(kryon / 'build/bin/zib')),
                       os.environ.get('PHOTOS_BUNDLE', str(ROOT / 'build/photos-portable.zib'))]
        else:
            command = [os.environ.get('PHOTOS_BINARY', str(ROOT / 'build/photos-desktop'))]
        app = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def wait(predicate, message, allow_exit=False):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not allow_exit:
                    assert app.poll() is None, app.stderr.read().decode()
                assert not errors, errors
                value = predicate()
                if value:
                    return value
                time.sleep(.02)
            raise AssertionError(message)

        def xdo(*arguments):
            return subprocess.check_output(['xdotool', *map(str, arguments)], text=True).strip()

        def request_close(window):
            # ICCCM close for this test-owned window, with no window kill.
            x = c.CDLL('libX11.so.6')
            x.XOpenDisplay.argtypes = [c.c_char_p]
            x.XOpenDisplay.restype = c.c_void_p
            x.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
            x.XInternAtom.restype = c.c_ulong
            x.XSendEvent.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_long, c.c_void_p]
            x.XFlush.argtypes = [c.c_void_p]
            x.XCloseDisplay.argtypes = [c.c_void_p]
            display = x.XOpenDisplay(os.environ['DISPLAY'].encode())
            assert display
            try:
                event = (c.c_ulong * 24)()
                event[0], event[2], event[3], event[4] = 33, 1, display, int(window)
                event[5] = x.XInternAtom(display, b'WM_PROTOCOLS', 0)
                event[6] = 32
                event[7] = x.XInternAtom(display, b'WM_DELETE_WINDOW', 0)
                assert x.XSendEvent(display, int(window), 0, 0, c.byref(event))
                x.XFlush(display)
            finally:
                x.XCloseDisplay(display)

        def find_window():
            result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--pid', str(app.pid)], capture_output=True, text=True)
            return result.stdout.splitlines()[0] if result.returncode == 0 else None

        try:
            window = wait(find_window, 'Photos did not open its owned window')
            xdo('windowsize', window, 1180, 880)
            xdo('windowmove', window, 0, 0)
            xdo('windowfocus', window)

            def state():
                raw = subprocess.check_output(['xprop', '-id', window, '_HARMONY_APP_STATE'], text=True)
                return json.loads(ast.literal_eval(raw.split(' = ', 1)[1])) if ' = ' in raw else {}

            def button(label):
                snapshot = state()
                if snapshot.get('loading'):
                    return None
                return next((control for control in snapshot.get('controls', [])
                             if control['label'] == label and control['enabled']), None)

            def click(control):
                xdo('mousemove', '--window', window, int(control['x'] + control['width'] / 2),
                    int(control['y'] + control['height'] / 2))
                xdo('mousedown', '1')
                time.sleep(.12)
                xdo('mouseup', '1')

            def point(x, y):
                return dict(x=x, y=y, width=0, height=0)

            if portable:
                # Portable apps expose their rendered view rather than the
                # native host's semantic control property. Capture only ours.
                capture = ROOT / 'build/screenshots/heartbeat-portable.png'
                capture.parent.mkdir(parents=True, exist_ok=True)
                def rendered():
                    subprocess.run(['import', '-window', window, str(capture)], check=True)
                    with Image.open(capture) as image:
                        pixel = image.convert('RGB').getpixel((260, 300))
                    return max(abs(a-b) for a, b in zip(pixel, (67, 142, 124))) <= 4
                wait(rendered, 'Portable Photos did not render the synthetic gallery')
                click(point(95, 137))
                wait(lambda: any(request.get('view') == 'albums' for request in requests), 'Album catalog was not requested')
                time.sleep(.3)
                click(point(248, 166))
                time.sleep(.3)
                click(point(500, 234))
            else:
                click(wait(lambda: button('Albums'), 'Albums control did not become ready'))
                wait(lambda: state().get('view') == 'albums' and button('New album'), 'Album catalog did not load')
                click(wait(lambda: button('New album'), 'New album control did not become ready'))
                wait(lambda: state().get('editor') == 'album_create', 'Album editor did not open')
                field = wait(lambda: next((control for control in state().get('controls', [])
                                          if control['control'] == '41'), None), 'Album text field is missing')
                click(field)
            xdo('type', '--delay', '20', '--clearmodifiers', '--', 'Heartbeat album')
            if closing:
                request_close(window)
                time.sleep(.3)
                assert app.poll() is None, 'Close discarded an unfinished editor'
            hold.set()
            wait(pending.is_set, 'Photos did not keep its active service alive')
            if portable:
                click(point(251, 339))
            else:
                snapshot = state()
                assert snapshot['editor'] == 'album_create', snapshot
                assert not snapshot['loading'], 'A service heartbeat disabled editing'
                click(wait(lambda: button('Save'), 'A service heartbeat disabled Save'))
            time.sleep(.2)
            assert not any(request['action'] == 'album_create' for request in requests), requests
            if not portable:
                snapshot = state()
                assert snapshot['loading'], 'A submitted edit did not retain its pending state'
                assert not any(control['label'] == 'Cancel' and control['enabled']
                               for control in snapshot['controls']), 'Cancel remained enabled after submitting Save'
            if closing:
                request_close(window)
                time.sleep(2.2)
                assert app.poll() is None, 'Close discarded an accepted Save behind the heartbeat'
            release.set()
            wait(lambda: any(request['action'] == 'album_create' for request in requests), 'Save was lost behind the heartbeat', closing)
            wait(lambda: library.db.execute('SELECT 1 FROM albums WHERE name=?', ('Heartbeat album',)).fetchone(), 'Album save did not finish', closing)
            if closing:
                assert app.wait(timeout=10) == 0, app.stderr.read().decode()
            elif not portable:
                wait(lambda: state().get('editor') == '' and not state().get('loading'), 'Album editor did not finish')
            assert library.album_details('Heartbeat album')['name'] == 'Heartbeat album'
            assert (source / 'Synthetic.jpg').read_bytes() == original
            assert not errors, errors
            print('Private Photos heartbeat: editing remains enabled, queued Save persists exact text, original unchanged')
            if closing:
                print('Graceful close waited for the unfinished editor and accepted Save, then released the process')
        finally:
            release.set()
            stop.set()
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=5)
            app.stderr.close()
            thread.join(5)
            listener.close()
            library.db.close()
            assert not thread.is_alive(), 'Owned test service did not stop'


if __name__ == '__main__':
    main()
