"""A real signed-app player talks only to this test's private Photos service."""
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
from photos.library import Library
from photos.service import Server

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    album = folder / 'source/Synthetic'
    album.mkdir(parents=True)
    Image.new('RGB', (700, 500), '#438e7c').save(album / 'Fixture.jpg')
    library = Library(folder / 'library')
    library.import_folders([album.parent])
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder)}
    with Server(dict(root=str(library.root)), folder / 'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever, daemon=True).start()
        player = Path(os.environ.get('PHOTOS_PLAYER', ROOT.parent / 'kryon/build/bin/zib'))
        process = subprocess.Popen([str(player), str(ROOT / 'build/photos-portable.zib')], env=env)
        def xdo(*args):
            return subprocess.check_output(['xdotool', *map(str, args)], text=True).strip()
        try:
            window = ''
            for _ in range(100):
                assert process.poll() is None
                result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--pid', str(process.pid)], capture_output=True, text=True)
                if result.returncode == 0:
                    window = result.stdout.splitlines()[0]
                    break
                time.sleep(.1)
            assert window
            xdo('windowsize', window, 1180, 880)
            shot = ROOT / 'build/screenshots/portable.png'
            shot.parent.mkdir(parents=True, exist_ok=True)
            for _ in range(50):
                subprocess.run(['import', '-window', window, str(shot)], check=True)
                with Image.open(shot) as captured:
                    pixel = captured.convert('RGB').getpixel((260, 220))
                if max(abs(a-b) for a,b in zip(pixel, (67,142,124))) <= 4:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable Photos did not render its local thumbnail')
            xdo('mousemove', '--window', window, 260, 220)
            xdo('click', 1)
            time.sleep(.5)
            xdo('mousemove', '--window', window, 503, 166)
            xdo('click', 1)
            for _ in range(50):
                if library.db.execute('SELECT SUM(favorite) FROM photos').fetchone()[0] == 1:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable favorite edit did not reach the local library')
            print('Portable Photos: private service, real thumbnail rendering and persisted favorite verified')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
            service.shutdown()
            library.db.close()
