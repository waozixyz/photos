"""Play synthetic embedded camera clips on a private display."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('PHOTOS_SERVICE_PACKAGE', str(ROOT)))
from photos.library import Library, digest_file
from photos.service import Server
import photos.service as service_module
from motion_fixtures import jpeg, heif
from test_live import movie

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_MOTION_PORTABLE') == '1'
output = ROOT/'build/screenshots'; output.mkdir(parents=True,exist_ok=True)


def wait(check, reason, timeout=20):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as directory:
    folder = Path(directory); source = folder/'source/Camera'; source.mkdir(parents=True)
    movie(folder/'clip.mp4'); clip = (folder/'clip.mp4').read_bytes()
    jpeg(source/'MomentMP.jpg',clip)
    heif(source/'SecondMP.heic',clip)
    heif(source/'ThirdMP.avif',clip,4)
    originals = {p:p.read_bytes() for p in source.iterdir()}
    ids = [digest_file(source/name) for name in ['MomentMP.jpg','SecondMP.heic','ThirdMP.avif']]
    library = Library(folder/'library'); library.import_folders([source.parent])
    for index,photo_id in enumerate(ids):
        library.edit(photo_id,'taken',f'2025-06-0{3-index}T12:00:00Z')
    calls = []; original_process = service_module.process
    def record(request,config):
        result = original_process(request,config); calls.append((request,result)); return result
    service_module.process = record
    env = {**os.environ,'KRYON_SERVICE_ROOT':str(folder),'KRYON_VIDEO_TEST_SILENT':'1'}
    if portable:
        flags = [] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        command = [os.environ.get('PHOTOS_PLAYER',str(kryon/'build/bin/zib')),
                   os.environ.get('PHOTOS_BUNDLE',str(ROOT/'build/photos-portable.zib'))]
    else:
        command = [os.environ.get('PHOTOS_BINARY',str(ROOT/'build/photos-desktop'))]
    with Server(dict(root=str(library.root)),folder/'kryon/services/photos.sock') as server:
        threading.Thread(target=server.serve_forever,daemon=True).start()
        child = subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def xdo(*args):
            return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()
        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            reply = subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return reply.stdout.splitlines()[0] if reply.returncode == 0 else None
        try:
            window = wait(locate,'Motion app did not open')
            xdo('windowsize',window,1180,880); xdo('windowmove',window,0,0); xdo('windowfocus',window)
            wait(lambda:calls,'Gallery missing'); time.sleep(.6)
            def click(x,y):
                xdo('mousemove','--window',window,x,y); xdo('mousedown',1); time.sleep(.12); xdo('mouseup',1); time.sleep(.25)
            def state():
                value = subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
                return json.loads(ast.literal_eval(value.split(' = ',1)[1].strip())) if ' = ' in value else {}
            def shoot(name):
                path = output/('motion-'+('portable-' if portable else 'native-')+name+'.png')
                subprocess.run(['import','-window',window,str(path)],check=True,timeout=5)
                with Image.open(path) as image: return image.convert('RGB').copy()
            click(260,320)
            for index,photo_id in enumerate(ids):
                wait(lambda:any(q.get('action')=='open' and q.get('id')==photo_id for q,r in calls),'Camera still did not open')
                reply = next(r for q,r in reversed(calls) if q.get('action')=='open' and q.get('id')==photo_id)
                assert reply['detail']['live'] == 1
                click(725,178)
                wait(lambda:any(r.get('playback_path') and r.get('playback_id')==photo_id for q,r in calls),'Embedded motion not authorized')
                if not portable: wait(lambda:state().get('playback_state')==2,'Embedded motion did not start')
                time.sleep(.1); first = shoot(f'{index}-first').crop((400,280,950,590))
                time.sleep(.3); second = shoot(f'{index}-second').crop((400,280,950,590))
                assert ImageChops.difference(first,second).getbbox(), 'Embedded video pixels did not advance'
                xdo('key','Escape'); time.sleep(.3)
                if index < 2: xdo('key','Right'); time.sleep(.3)
            click(875,178)
            wait(lambda:any(q.get('action')=='live_separate' and r.get('count')==4 for q,r in calls),'Motion copy separation missing')
            assert all(p.read_bytes()==data for p,data in originals.items())
            assert all(library.original(library.photo(digest_file(p))).read_bytes()==data for p,data in originals.items())
            print(('Portable' if portable else 'Native')+' JPEG/HEIC/AVIF embedded motion: animated pixels, audio-capable playback, navigation and retained-original separation passed')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            server.shutdown(); library.db.close()
