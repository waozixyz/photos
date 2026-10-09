"""Real slideshow timing, privacy and controls on synthetic private windows."""
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
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'tests'))
from photos.library import Library, digest_file, LibraryError
from photos import people
from photos.service import Server
import photos.service as service_module
from test_people import vector

assert os.environ.get('PHOTOS_PRIVATE_XVFB') == '1'
portable = os.environ.get('PHOTOS_SLIDESHOW_PORTABLE') == '1'
output = ROOT/'build/screenshots'; output.mkdir(exist_ok=True)
suffix = 'portable' if portable else 'native'


def wait(check, reason, timeout=20):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        value = check()
        if value: return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as directory:
    folder = Path(directory); source = folder/'source/Family'; source.mkdir(parents=True)
    ids = []; originals = {}
    for i in range(18):
        path = source/f'Photo {i}.png'
        Image.new('RGB', (700+i, 500), (50+i*9, 100+i*4, 130)).save(path)
        ids.append(digest_file(path)); originals[path] = path.read_bytes()
    library = Library(folder/'library'); library.import_folders([source.parent])
    library.set_setting('people_enabled',True); library.set_setting('people_processing',True)
    for i, photo_id in enumerate(ids):
        library.edit(photo_id,'taken',f'{2020+i//6}-07-{1+i%6:02d}T12:00:00Z')
        people.store(library,photo_id,[dict(rect=[1000,1000,5000,6000],embedding=vector())],folder)
    person = library.db.execute('SELECT person_id FROM faces LIMIT 1').fetchone()[0]
    library.set_setting('people_processing',False); people.edit(library,'person_name',person,'Ada')
    library.edit_album('','create','Second'); library.edit(ids[0],'album_add','Second')
    calls=[]; hold=threading.Event(); held=threading.Event(); release=threading.Event()
    original_process=service_module.process
    def record(request,config):
        if request.get('action')=='slide_check' and hold.is_set():
            held.set(); release.wait(8)
        try: result=original_process(request,config)
        except LibraryError as error:
            calls.append((request,dict(error=str(error)),time.monotonic())); raise
        calls.append((request,result,time.monotonic())); return result
    service_module.process=record
    env={**os.environ,'KRYON_SERVICE_ROOT':str(folder)}
    if portable:
        flags=[] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon=Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        command=[os.environ.get('PHOTOS_PLAYER',str(kryon/'build/bin/zib')),os.environ.get('PHOTOS_BUNDLE',str(ROOT/'build/photos-portable.zib'))]
    else: command=[os.environ.get('PHOTOS_BINARY',str(ROOT/'build/photos-desktop'))]
    with Server(dict(root=str(library.root)),folder/'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever,daemon=True).start()
        child=subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def xdo(*args): return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()
        def locate():
            assert child.poll() is None,child.stderr.read().decode()
            result=subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode==0 else None
        try:
            window=wait(locate,'Slideshow app did not open')
            xdo('windowsize',window,1180,880);xdo('windowmove',window,0,0);xdo('windowfocus',window)
            wait(lambda:calls,'Initial gallery missing');time.sleep(.5)
            def state():
                raw=subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
                return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}
            def click(x,y):
                xdo('mousemove','--window',window,x,y);xdo('mousedown',1);time.sleep(.12);xdo('mouseup',1);time.sleep(.3)
            def control(label,x,y):
                if not portable:
                    c=wait(lambda:next((c for c in state().get('controls',[]) if c['label']==label and c['enabled']),None),'Missing control: '+label)
                    x,y=c['x']+c['width']/2,c['y']+c['height']/2
                click(int(x),int(y))
            def result(action,since=0): return next((r for q,r,t in reversed(calls[since:]) if q.get('action')==action),None)
            def shot(name):
                path=output/f'slideshow-{name}-{suffix}.png'
                subprocess.run(['import','-window',window,str(path)],check=True,timeout=5)
                with Image.open(path) as image: return image.convert('RGB').getpixel((590,440))
            control('Albums',90,137);time.sleep(.6);control('Family',450,220);time.sleep(.6)
            since=len(calls);control('Slideshow',585,254)
            first=wait(lambda:result('slide_frame',since),'First slide missing');assert first['slide_id']==ids[0] and first['slide_count']==18
            wait(lambda:result('slide_check',since),'Slide access was not rechecked');time.sleep(.6)
            assert shot('ordered')==(50,100,130)
            control('Pause slideshow',735,802);since=len(calls);time.sleep(5.4)
            assert not result('slide_frame',since),'Paused slideshow advanced'
            since=len(calls);control('Next photo',1027,802)
            wait(lambda:(result('slide_frame',since) or {}).get('slide_id')==ids[1],'Next slide missing')
            since=len(calls);control('Previous photo',445,802)
            wait(lambda:(result('slide_frame',since) or {}).get('slide_id')==ids[0],'Previous slide missing')
            since=len(calls);control('Black background',735,850)
            blurred=wait(lambda:result('slide_frame',since),'Blurred background missing');assert Path(blurred['slide_background']).is_file()
            time.sleep(.5);shot('blurred')
            control('In order',445,850);wait(lambda:any(q.get('slide_shuffle')==1 for q,r,t in calls),'Shuffle missing')
            for label in ['5 seconds','10 seconds','15 seconds','30 seconds','1 minute','5 minutes','10 minutes']:
                control(label,153,850)
            since=len(calls);control('Resume slideshow',735,802)
            resumed=time.monotonic()
            wait(lambda:result('slide_frame',since),'Slideshow did not advance after resuming',timeout=8)
            assert time.monotonic()-resumed>=4.3,'Slideshow used frames instead of real seconds'
            control('Pause slideshow',735,802);control('Hide controls',1027,850)
            click(590,440);control('Stop slideshow',153,802);time.sleep(.7)
            # Choose two albums. Shared membership appears only once.
            control('Albums',90,137);time.sleep(.6);control('Slideshow albums',745,166)
            control('Family',450,220);control('Second',450,288)
            since=len(calls);control('Play selected albums',915,166)
            union=wait(lambda:result('slide_frame',since),'Selected album presentation missing');assert union['slide_count']==18
            assert set(next(q for q,r,t in calls[since:] if q.get('action')=='slide_frame')['slide_albums'])=={'Family','Second'}
            control('Stop slideshow',153,802);time.sleep(.7)
            control('People',90,698);time.sleep(.6);control('Ada',450,328);time.sleep(.6);control('Memory Lane',285,288);time.sleep(.6)
            since=len(calls);control('Play Memory Lane',1050,182)
            lane=wait(lambda:result('slide_frame',since),'Memory Lane presentation missing');assert lane['slide_count']==18 and '2020' in lane['slide_caption']
            control('Pause slideshow',735,802);time.sleep(.5);shot('lane')
            # A privacy mutation revokes an already displayed image.
            displayed=next(r['slide_id'] for q,r,t in reversed(calls) if q.get('action')=='slide_frame')
            library.edit(displayed,'hidden_set','1');since=len(calls)
            wait(lambda:(result('slide_check',since) or {}).get('error'),'Concurrent hide did not revoke slideshow')
            if not portable: wait(lambda:not state().get('slideshow_active'),'Private slide remained active')
            time.sleep(.6);shot('revoked')
            library.edit(displayed,'hidden_set','0');time.sleep(.7)
            since=len(calls);control('Play Memory Lane',1050,182);restart=wait(lambda:result('slide_frame',since),'Lane restart missing');time.sleep(.8)
            before_expiry=shot('lease')
            # An unavailable/blocked service must expire the display lease.
            hold.set();wait(held.is_set,'Privacy request did not reach the service');time.sleep(3)
            if not portable: assert not state().get('slideshow_active'),'Stalled service retained the slide'
            assert shot('expired')!=before_expiry,'Stalled service retained the rendered slide'
            release.set();hold.clear()
            for path,data in originals.items(): assert path.read_bytes()==data
            print(('Portable' if portable else 'Native')+' slideshow: actual timing, pause/resume, previous/next, all intervals, shuffle, blur, multiple albums, Memory Lane, privacy revocation and service expiry passed')
        except Exception:
            subprocess.run(['import','-window',window,str(output/f'slideshow-failure-{suffix}.png')],timeout=5)
            print('Recent synthetic requests:',[(q.get('action'),q.get('view'),r.get('error')) for q,r,t in calls[-10:]],flush=True)
            raise
        finally:
            release.set();child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            service.shutdown()
    library.db.close()
