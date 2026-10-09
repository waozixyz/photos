"""Exercise real native/portable playback on synthetic private-Xvfb media."""
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
sys.path.insert(0, str(ROOT))
from photos.library import Library, digest_file
from photos.service import Server
import photos.service as service_module

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_PLAYBACK_PORTABLE') == '1'
output = ROOT/'build/screenshots'
output.mkdir(parents=True, exist_ok=True)


def wait(check, reason, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    source = folder/'source/Synthetic/Moving video.mp4'
    source.parent.mkdir(parents=True)
    subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-f','lavfi','-i',
                    'testsrc2=size=160x90:rate=24','-f','lavfi','-i',
                    'sine=frequency=440:sample_rate=44100','-t','18','-c:v',
                    'libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac',str(source)],
                   check=True, capture_output=True)
    original = source.read_bytes()
    library = Library(folder/'library')
    library.import_folders([source.parents[1]])
    photo_id = digest_file(source)
    calls = []
    blocked = threading.Event()
    delay = [False]
    original_process = service_module.process

    def record(request, config):
        if request.get('action') == 'playback_check' and delay[0]:
            blocked.set()
            time.sleep(3.5)
        result = original_process(request, config)
        calls.append((request, result))
        return result

    service_module.process = record
    env = {**os.environ, 'KRYON_SERVICE_ROOT':str(folder), 'KRYON_VIDEO_TEST_SILENT':'1'}
    if portable:
        flags = [] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon = Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        player = Path(os.environ.get('PHOTOS_PLAYER', kryon/'build/bin/zib'))
        bundle = Path(os.environ.get('PHOTOS_BUNDLE', ROOT/'build/photos-portable.zib'))
        command = [str(player), str(bundle)]
    else:
        command = [os.environ.get('PHOTOS_BINARY', str(ROOT/'build/photos-desktop'))]
    with Server(dict(root=str(library.root)), folder/'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever,daemon=True).start()
        child = subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)

        def xdo(*args):
            return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()

        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            result = subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode == 0 else None

        try:
            window = wait(locate, 'Playback app did not open')
            xdo('windowsize',window,1180,880); xdo('windowmove',window,0,0); xdo('windowfocus',window)
            wait(lambda:calls, 'Initial gallery request missing'); time.sleep(.6)

            def click(x,y):
                xdo('mousemove','--window',window,x,y); xdo('mousedown',1)
                time.sleep(.12); xdo('mouseup',1); time.sleep(.15)

            def snapshot():
                raw = subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
                return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}

            def shoot(name):
                target = output/name
                subprocess.run(['import','-window',window,str(target)],check=True)
                with Image.open(target) as image:
                    return image.convert('RGB').copy()

            click(260,320)
            wait(lambda:any(q.get('action')=='open' for q,r in calls), 'Video original did not open')
            click(535,222)
            wait(lambda:any(r.get('playback_path') for q,r in calls), 'Playback was not authorized')
            if not portable:
                wait(lambda:snapshot().get('playback_state')==2,'Native decoder did not start')
            time.sleep(.5)
            first = shoot('playback-first.png').crop((400,280,950,590))
            time.sleep(.3)
            moving = shoot('playback-moving.png').crop((400,280,950,590))
            assert ImageChops.difference(first,moving).getbbox(), 'Video did not render moving frames'
            click(309,722) # Pause.
            if not portable:
                wait(lambda:snapshot().get('playback_state')==3,'Pause did not take effect')
            time.sleep(.2)
            paused = shoot('playback-paused.png').crop((400,280,950,590))
            time.sleep(.3)
            still = shoot('playback-still.png').crop((400,280,950,590))
            assert not ImageChops.difference(paused,still).getbbox(), 'Paused video continued moving'
            xdo('key','Right'); time.sleep(.3)
            sought = shoot('playback-sought.png').crop((400,280,950,590))
            assert ImageChops.difference(still,sought).getbbox(), ('Seeking did not update the paused frame', snapshot() if not portable else 'portable')
            click(1033,722) # 1.5x.
            click(309,770) # Mute.
            if not portable:
                state = wait(lambda:snapshot() if snapshot().get('playback_speed')==150 else None,'Speed did not change')
                assert state['playback_volume']==0, state
            click(1060,178) # Expand.
            time.sleep(.3)
            expanded = shoot('playback-expanded.png')
            assert expanded.size == (1180,880)
            xdo('key','Escape'); time.sleep(.3)
            click(309,722) # Resume.
            if not portable:
                wait(lambda:snapshot().get('playback_state')==2,'Resume did not start')
            delay[0] = True
            wait(blocked.is_set,'Authorization heartbeat was not sent')
            time.sleep(2.8)
            if not portable:
                assert snapshot()['playback_state']==0, 'A stalled library request kept playing'
            time.sleep(1); delay[0] = False
            # A late response cannot resurrect a revoked player. Open a new one.
            click(535,222)
            wait(lambda:len([r for q,r in calls if r.get('playback_path')])>=3,'Playback did not reopen')
            time.sleep(.6)
            library.edit(photo_id,'hidden')
            wait(lambda:any('error' in r and q.get('action')=='playback_check' for q,r in calls),
                 'Concurrent hiding did not revoke video access')
            time.sleep(.4)
            if not portable:
                assert snapshot()['playback_state']==0
            shot = shoot('playback-portable.png' if portable else 'playback-native.png')
            assert source.read_bytes()==original
            assert library.original(library.photo(photo_id)).read_bytes()==original
            print(('Portable' if portable else 'Native')+' playback: moving/pause/seek frames, speed/mute controls, expanded view, stalled authorization and concurrent Hidden revocation passed; originals retained')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            service.shutdown(); library.db.close()
