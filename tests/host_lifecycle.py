"""Actual Photos through Harmony's isolated supervisor, on owned Xvfb only."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

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


def wait(predicate, message):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    raise AssertionError(message)


def verify(mode, arguments, output):
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS'):
        assert not os.environ.get(key), f'{key} must be scrubbed before this test'
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('SESSION_MANAGER', 'LD_PRELOAD')
                   and not key.startswith(('PHOTOS_', 'KRYON_', 'HARMONY_'))}
    environment.update(YUE_DESKTOP_RECOVERY='0', APP_NO_TRAY='1', SDL_AUDIODRIVER='dummy',
                       SDL_VIDEODRIVER='x11', LIBGL_ALWAYS_SOFTWARE='1')
    with tempfile.TemporaryDirectory(prefix='photos-host-') as temporary:
        root = Path(temporary)
        project = root / 'Projects/waozixyz/breeze'
        executable = project / 'build/bin/linux/breeze-linux-x86_64'
        executable.parent.mkdir(parents=True)
        if mode == 'native':
            shutil.copy2(arguments.native, executable)
        else:
            # A test launcher passes this exact local bundle to the player;
            # exec retains the supervisor's exact PID and startup adapter.
            executable.write_text('#!/bin/sh\nexec ' + shlex.quote(str(arguments.player)) +
                                  ' ' + shlex.quote(str(arguments.bundle)) + '\n')
            executable.chmod(0o700)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        environment.update(HOME=str(root), PROJECTS_ROOT=str(root / 'Projects'),
                           XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=str(root / 'config'),
                           XDG_DATA_HOME=str(root / 'data'), XDG_CACHE_HOME=str(root / 'cache'),
                           XDG_STATE_HOME=str(root / 'state'), HARMONY_APP_CONFIG=str(root / 'absent.json'))
        source = root / 'source'
        source.mkdir()
        original = source / 'Synthetic.jpg'
        Image.new('RGB', (700, 500), '#438e7c').save(original)
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        library = Library(root / 'library')
        library.import_folders([source])
        endpoint = runtime / 'kryon/services/photos.sock'
        endpoint.parent.mkdir(parents=True, mode=0o700)
        received = threading.Event()
        release = threading.Event()
        requests = []
        worker = service_module.process

        def process(request, config):
            requests.append(dict(request))
            if request.get('action') == 'album_create':
                received.set()
                assert release.wait(10), 'Accepted Save was not released'
            return worker(request, config)

        service_module.process = process
        server = host = foreign = None
        thread = None
        with (output / (mode + '.log')).open('w+b') as log:
            try:
                server = subprocess.Popen(['Xvfb', '-displayfd', '1', '-screen', '0', '1600x1000x24',
                                           '-nolisten', 'tcp'], env=environment,
                                          stdout=subprocess.PIPE, stderr=log)
                number = server.stdout.readline().decode().strip()
                server.stdout.close()
                assert number.isdecimal(), 'Owned Xvfb failed to start'
                environment['DISPLAY'] = ':' + number

                def xdo(*args):
                    return subprocess.check_output(['xdotool', *map(str, args)], env=environment, text=True).strip()

                def find_window(pid):
                    found = subprocess.run(['xdotool', 'search', '--pid', str(pid)], env=environment,
                                           capture_output=True, text=True, timeout=3)
                    return found.stdout.splitlines()[0] if found.returncode == 0 else None

                foreign_directory = root / 'foreign'
                foreign_directory.mkdir()
                foreign = subprocess.Popen([str(arguments.fixture)], cwd=foreign_directory,
                                           env=environment, stdout=log, stderr=log)
                foreign_window = wait(lambda: find_window(foreign.pid), 'Foreign fixture did not open')
                foreign_geometry = subprocess.check_output(['xwininfo', '-id', foreign_window],
                                                          env=environment, text=True)
                with Server(dict(root=str(library.root), sources=[]), endpoint) as service:
                    thread = threading.Thread(target=service.serve_forever, daemon=True)
                    thread.start()
                    host = subprocess.Popen([str(arguments.host)], env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)

                    def command(value):
                        assert host.poll() is None, 'Owned supervisor exited'
                        host.stdin.write(value.encode())
                        host.stdin.flush()

                    def status():
                        command('r')
                        ready, _, _ = select.select([host.stdout], [], [], 5)
                        assert ready, 'Owned supervisor did not answer'
                        return json.loads(host.stdout.readline())

                    parent = wait(lambda: find_window(host.pid), 'Owned host did not open')
                    xdo('windowsize', parent, 1180, 880)
                    command('o')

                    def shown():
                        snapshot = status()
                        return snapshot if snapshot['pid'] and snapshot['embedded'] and snapshot['shown'] else None

                    child = wait(shown, 'Actual Photos did not embed')['pid']
                    window = wait(lambda: find_window(child), 'Owned Photos window was not found')
                    xdo('windowfocus', window)

                    def state():
                        raw = subprocess.check_output(['xprop', '-id', window, '_HARMONY_APP_STATE'],
                                                      env=environment, text=True)
                        return json.loads(ast.literal_eval(raw.split(' = ', 1)[1])) if ' = ' in raw else {}

                    def button(label):
                        snapshot = state()
                        if snapshot.get('loading'):
                            return None
                        return next((control for control in snapshot.get('controls', [])
                                     if control['label'] == label and control['enabled']), None)

                    def click(control):
                        xdo('mousemove', '--window', window, int(control['x'] + control.get('width', 0) / 2),
                            int(control['y'] + control.get('height', 0) / 2), 'mousedown', '1')
                        time.sleep(.12)
                        xdo('mouseup', '1')

                    if mode == 'native':
                        click(wait(lambda: button('Albums'), 'Albums control did not load'))
                        click(wait(lambda: button('New album'), 'New album control did not load'))
                        field = wait(lambda: next((control for control in state().get('controls', [])
                                                   if control['control'] == '41'), None), 'Editor field was not found')
                        click(field)
                    else:
                        capture = output / 'portable-owned.png'

                        def rendered():
                            subprocess.run(['import', '-window', window, str(capture)], env=environment, check=True)
                            with Image.open(capture) as image:
                                pixel = image.convert('RGB').getpixel((260, 300))
                            return max(abs(a-b) for a, b in zip(pixel, (67, 142, 124))) <= 4

                        wait(rendered, 'Portable hosted Photos did not render the synthetic gallery')
                        click(dict(x=95, y=137))
                        wait(lambda: any(request.get('view') == 'albums' for request in requests), 'Album view was not requested')
                        time.sleep(.4)
                        click(dict(x=248, y=166))
                        time.sleep(.4)
                        click(dict(x=500, y=234))
                    name = 'Hosted pending Save ' + mode
                    xdo('type', '--delay', '20', '--clearmodifiers', '--', name)
                    command('h')
                    wait(lambda: not status()['shown'], 'Hidden editor was not clipped')
                    time.sleep(.3)
                    assert status()['pid'] == child, 'Hidden editor lost its unfinished draft'
                    command('o')
                    assert wait(shown, 'Draft did not reopen')['pid'] == child
                    xdo('windowfocus', window)
                    if mode == 'native':
                        assert state()['editor'] == 'album_create'
                        click(wait(lambda: button('Save'), 'Reopened editor could not Save'))
                    else:
                        click(dict(x=251, y=339))
                    wait(received.is_set, 'Reopened editor did not send its exact Save')
                    command('h')
                    wait(lambda: not status()['shown'], 'Pending Save was not hidden')
                    time.sleep(2.2)
                    assert status()['pid'] == child, 'Accepted Save was killed during navigation'
                    release.set()
                    wait(lambda: status()['pid'] == 0, 'Completed hidden Photos was not reaped')
                    assert not Path(f'/proc/{child}').exists(), 'Hidden Photos still consumes process CPU/RAM'
                    assert library.album_details(name)['name'] == name, 'Exact draft/Save was not preserved'
                    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
                    reopen_request_start = len(requests)
                    command('o')
                    reopened = wait(shown, 'Reclaimed Photos did not reopen')['pid']
                    assert reopened != child
                    wait(lambda: any(request.get('action') == 'query' for request in requests[reopen_request_start:]),
                         'Reopened library did not query')
                    time.sleep(.4)
                    command('h')
                    wait(lambda: status()['pid'] == 0, 'Idle reopened Photos did not exit when hidden')
                    assert not Path(f'/proc/{reopened}').exists()
                    assert foreign.poll() is None
                    assert subprocess.check_output(['xwininfo', '-id', foreign_window],
                                                   env=environment, text=True) == foreign_geometry
                    command('q')
                    assert host.wait(timeout=10) == 0
                thread.join(5)
                assert not thread.is_alive(), 'Owned Photos service did not stop'
                return dict(mode=mode, embedded=True, draft_preserved=True, accepted_save_preserved=True,
                            exact_original_preserved=True, hidden_process_reaped=True, reopened=True,
                            foreign_window_untouched=True, no_deployment=True)
            finally:
                release.set()
                for process_ in (host, foreign, server):
                    stop(process_)
                if host is not None:
                    host.stdin.close()
                    host.stdout.close()
                service_module.process = worker
                library.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument('--fixture', type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument('--player', type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument('--native', type=lambda value: Path(value).resolve(), default=ROOT / 'build/photos-desktop')
    parser.add_argument('--bundle', type=lambda value: Path(value).resolve(), default=ROOT / 'build/photos-portable.zib')
    arguments = parser.parse_args()
    output = ROOT / 'build/host-lifecycle'
    output.mkdir(parents=True, exist_ok=True)
    results = [verify(mode, arguments, output) for mode in ('native', 'portable')]
    receipt = dict(results=results, private_display=True, synthetic_media=True, deployment=False,
                   owned_processes_stopped=True,
                   inputs={str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in (arguments.host, arguments.fixture, arguments.player,
                                        arguments.native, arguments.bundle)})
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('Actual native and portable Photos: hidden exit/reopen, exact pending Save and original preservation passed in the isolated Harmony supervisor')


if __name__ == '__main__':
    main()
