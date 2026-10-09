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
    Image.new('RGB', (700, 501), '#438e7c').save(album / 'Second.jpg')
    library = Library(folder / 'library')
    library.import_folders([album.parent])
    env = {**os.environ, 'KRYON_SERVICE_ROOT': str(folder)}
    with Server(dict(root=str(library.root)), folder / 'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever, daemon=True).start()
        locked = [] if (ROOT / 'ziran.local.toml').exists() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran', 'pkg', 'path', 'kryon', *locked], cwd=ROOT, text=True).strip())
        player = Path(os.environ.get('PHOTOS_PLAYER', kryon / 'build/bin/zib'))
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
            xdo('windowmove', window, 0, 0)
            xdo('windowfocus', window)
            shot = ROOT / 'build/screenshots/portable.png'
            shot.parent.mkdir(parents=True, exist_ok=True)
            for _ in range(50):
                subprocess.run(['import', '-window', window, str(shot)], check=True)
                with Image.open(shot) as captured:
                    pixel = captured.convert('RGB').getpixel((260, 300))
                if max(abs(a-b) for a,b in zip(pixel, (67,142,124))) <= 4:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable Photos did not render its local thumbnail')
            xdo('mousemove', '--window', window, 260, 300)
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
            # Exercise the same Ziran selection and editor in the portable VM.
            def click(x, y):
                xdo('mousemove', '--window', window, x, y); xdo('click', 1); time.sleep(.4)
            click(225, 166)  # Back
            click(230, 166)  # Select
            click(343, 166)  # Select loaded
            click(266, 210)  # Favorite all, including the existing favorite
            for _ in range(50):
                if library.query('favorites')['count'] == 2:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable batch favorite did not persist both photos')
            click(230, 166); click(343, 166)
            click(933, 210)  # Add to album
            click(500, 234)  # Album name editor
            xdo('type', '--delay', 45, '--clearmodifiers', '--', 'Portable album'); time.sleep(.2)
            click(251, 339)  # Save
            for _ in range(50):
                if library.query(album='Portable album')['count'] == 2:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable album editor did not persist the selection')
            print('Portable Photos: private service, thumbnails, favorite, bulk selection and album editor verified')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
            service.shutdown()
            library.db.close()
