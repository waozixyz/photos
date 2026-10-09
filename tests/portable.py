"""A real signed-app player talks only to this test's private Photos service."""
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
from photos.library import Library
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    album = folder / 'source/Synthetic'
    album.mkdir(parents=True)
    Image.new('RGB', (700, 500), '#438e7c').save(album / 'Fixture.jpg')
    Image.new('RGB', (700, 501), '#438e7c').save(album / 'Second.jpg')
    library = Library(folder / 'library')
    library.import_folders([album.parent])
    calls = []
    original_process = service_module.process
    def record_request(request, config):
        calls.append(dict(request))
        return original_process(request, config)
    service_module.process = record_request
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
            click(230, 166); click(343, 166)
            click(602, 254)  # Archive selection
            for _ in range(50):
                if library.query('archive')['count'] == 2:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable archive did not persist the selection')
            click(65, 418)  # Archive view
            click(230, 166); click(343, 166)
            click(602, 254)  # Unarchive selection
            for _ in range(50):
                if library.query('archive')['count'] == 0:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable unarchive did not restore the selection')
            click(65, 90)  # Home timeline
            click(1100, 166)  # Oldest first
            for _ in range(50):
                if any(r.get('sort') == 'oldest' for r in calls):
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Portable sort did not reach the library service')
            click(65, 137)  # Albums
            click(240, 166)  # New album
            click(500, 234)
            xdo('type', '--delay', 45, '--clearmodifiers', '--', 'Empty portable'); time.sleep(.2)
            click(251, 339)
            for _ in range(50):
                if any(a['name']=='Empty portable' for a in library.album_list('empty')): break
                time.sleep(.1)
            else: raise AssertionError('Portable empty album was not retained')
            click(310, 210)  # Description
            click(500, 234)
            xdo('type', '--delay', 45, '--clearmodifiers', '--', 'A portable description'); time.sleep(.2)
            click(251, 339)
            assert library.album_details('Empty portable')['description']=='A portable description'
            click(558, 210)  # Pin album
            assert library.album_details('Empty portable')['pinned']==1
            click(65, 371)  # Local sync
            click(452, 484)  # Continuous export
            click(500, 234)
            xdo('type', '--delay', 45, '--clearmodifiers', '--', str(folder/'continuous')); time.sleep(.2)
            click(251, 339)
            assert library.setting('export')['continuous']
            click(620, 484)  # Stop export
            assert not library.setting('export')['continuous']
            def wait_for(check, reason):
                for _ in range(100):
                    if check(): return
                    assert process.poll() is None
                    time.sleep(.05)
                raise AssertionError((reason,[(r['action'],r.get('view')) for r in calls[-5:]]))
            click(65, 90);click(260, 300)
            parent_id = next(r['id'] for r in reversed(calls) if r['action']=='open')
            original_bytes = library.original(library.photo(parent_id)).read_bytes()
            click(255, 210)  # Edit image
            wait_for(lambda: any(r['action']=='image_preview' for r in calls), 'Portable editor did not open')
            click(310, 208)  # Rotate right
            wait_for(lambda: any(r['action']=='image_preview' and json.loads(r['value'])[0]==90 for r in calls), 'Portable rotation did not reach Ziran image processor')
            subprocess.run(['import','-window',window,str(shot.with_name('portable-editing.png'))],check=True)
            click(680, 775)  # Save copy
            wait_for(lambda: library.query()['total']==3, 'Portable edited copy was not saved')
            assert library.original(library.photo(parent_id)).read_bytes()==original_bytes
            click(399, 210)  # Edit location
            click(500, 234)
            xdo('type','--delay',45,'--clearmodifiers','--','-25.3, -57.6');time.sleep(.2);click(251,339)
            wait_for(lambda: library.details(parent_id)['location']=='-25.3, -57.6', 'Portable coordinate edit was not saved')
            library.edit(parent_id,'taken','2024-10-08T12:00:00Z')
            click(65,512)  # Memories
            click(1108,208)  # Choose day
            click(500,234)
            xdo('keydown','ctrl');time.sleep(.1);xdo('key','a');time.sleep(.1);xdo('keyup','ctrl');time.sleep(.1)
            xdo('type','--delay',45,'--clearmodifiers','--','2026-10-08');time.sleep(.2);click(251,339)
            wait_for(lambda: any(r['action']=='memory_date' and r['value']=='2026-10-08' for r in calls), 'Portable memories day was not saved')
            time.sleep(.5)
            subprocess.run(['import','-window',window,str(shot.with_name('portable-memories.png'))],check=True)
            assert library.query('memories',memory_date='2026-10-08')['count']==1
            click(260,330)  # Open the memory
            click(877,748)  # Hide
            wait_for(lambda: library.photo(parent_id)['hidden'], 'Portable hide was not saved')
            click(65,277);click(300,352)  # Hidden, Create password
            click(500,234);xdo('type','--delay',45,'--clearmodifiers','--','Synthetic portable password');time.sleep(.2);click(251,339)
            click(500,234);xdo('type','--delay',45,'--clearmodifiers','--','Synthetic portable password');time.sleep(.2);click(251,339)
            wait_for(lambda: (library.root/'private/hidden-password.json').is_file(), 'Portable Hidden password was not created')
            click(1095,853)  # Lock Hidden
            wait_for(lambda: any(r['action']=='vault_lock' for r in calls), 'Portable Hidden did not lock')
            click(300,312)  # Unlock Hidden
            click(500,234);xdo('type','--delay',45,'--clearmodifiers','--','Synthetic portable password');time.sleep(.2);click(251,339)
            wait_for(lambda: any(r['action']=='vault_unlock' for r in calls), 'Portable Hidden did not unlock')
            click(65,90);click(65,277)
            wait_for(lambda: sum(r['action']=='vault_lock' for r in calls)>=2, 'Portable Hidden did not relock on exit')
            subprocess.run(['import','-window',window,str(shot.with_name('portable-hidden-locked.png'))],check=True)
            print('Portable Photos: organization/export/editor/memories baseline plus Hidden password setup, unlock, explicit lock and leave/reenter lock verified')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
            service.shutdown()
            library.db.close()
