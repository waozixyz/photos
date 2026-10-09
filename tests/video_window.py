"""Native and portable video editors on synthetic private Xvfb windows only."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from photos import video
from photos.library import Library,digest_file
from photos.service import Server
import photos.service as service_module
from test_video import fixture

assert os.environ.get('PHOTOS_PRIVATE_XVFB')=='1'
portable=os.environ.get('PHOTOS_VIDEO_PORTABLE')=='1'
output=ROOT/'build/screenshots'
output.mkdir(parents=True,exist_ok=True)


def wait(check,reason,timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=check()
        if result:return result
        time.sleep(.1)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as folder:
    folder=Path(folder);source=folder/'source/Synthetic/Fixture.mp4';fixture(source)
    original=source.read_bytes();library=Library(folder/'library');library.import_folders([source.parents[1]])
    photo_id=digest_file(source);calls=[]
    original_process=service_module.process
    def record(request,config):
        result=original_process(request,config)
        calls.append((request,result))
        return result
    service_module.process=record
    env={**os.environ,'KRYON_SERVICE_ROOT':str(folder)}
    if portable:
        flags=[] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon=Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        command=[str(kryon/'build/bin/zib'),str(ROOT/'build/photos-portable.zib')]
    else:
        command=[str(ROOT/'build/photos-desktop')]
    with Server(dict(root=str(library.root)),folder/'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever,daemon=True).start()
        child=subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def xdo(*args):
            return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()
        def locate():
            assert child.poll() is None,child.stderr.read().decode()
            result=subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode==0 else None
        try:
            window=wait(locate,'Video test app did not open')
            xdo('windowsize',window,1180,880);xdo('windowmove',window,0,0);xdo('windowfocus',window)
            wait(lambda:calls,'Photos query was not sent');time.sleep(.6)
            def click(x,y):
                xdo('mousemove','--window',window,x,y);xdo('mousedown',1);time.sleep(.12);xdo('mouseup',1);time.sleep(.4)
            def request(action,before=0):
                return next((r for q,r in reversed(calls[before:]) if q.get('action')==action),None)
            click(260,320);wait(lambda:request('open'),'Video did not open')
            click(250,210);wait(lambda:request('video_begin'),'Video editor did not open');time.sleep(.4)
            click(680,208);wait(lambda:request('video_preview'),'Rotate video preview was not requested')
            before=len(calls);click(1020,208);response=wait(lambda:request('video_preview',before),'Crop preview was not requested')
            assert 'error' not in response,response
            with Image.open(response['edit_preview']) as frame:assert frame.size==(1280,1280)
            click(350,208);click(500,234)
            xdo('keydown','ctrl');xdo('key','a');time.sleep(.1);xdo('keyup','ctrl')
            xdo('type','--delay',60,'--clearmodifiers','--','1.000, 3.000');time.sleep(.2)
            before=len(calls);click(251,339)
            response=wait(lambda:request('video_preview',before),'Trim range was not previewed')
            assert 'error' not in response,response
            assert any(q.get('value')=='[1000,3000,90,1]' for q,r in calls),calls
            time.sleep(.5)
            shot=output/('video-portable.png' if portable else 'video-native.png')
            subprocess.run(['import','-window',window,str(shot)],check=True)
            with Image.open(shot) as frame:
                assert frame.size==(1180,880)
                pixels=list(frame.convert('RGB').crop((400,300,900,600)).get_flattened_data())
                assert sum(max(p)-min(p)>60 for p in pixels)>10000,'Frame preview was not visibly rendered'
            before=len(calls);click(680,783)
            result=wait(lambda:request('video_save',before),'Save video copy did not start')
            assert result.get('video_working')==1,result
            wait(lambda:library.query()['total']==2,'Video copy was not saved')
            wait(lambda:any(r.get('saved_copy') for q,r in calls[before:] if q.get('action')=='video_status'),
                 'UI did not poll background video completion')
            copy=next(library.photo(row[0]) for row in library.db.execute('SELECT id FROM photos') if row[0]!=photo_id)
            info=video.probe(library.original(copy))
            assert (info['width'],info['height'])==(80,80) and info['audio'] and abs(info['duration_ms']-2000)<150
            assert source.read_bytes()==original and library.original(library.photo(photo_id)).read_bytes()==original
            # Reopening and cancelling is also a real UI action, with no extra copy.
            time.sleep(.5);before=len(calls);click(250,210)
            wait(lambda:request('video_begin',before),'Video editor did not reopen');click(1010,783)
            wait(lambda:request('image_close',before),'Video editor cancellation did not reach service')
            assert library.query()['total']==2
            print(('Portable' if portable else 'Native')+' video editor: rendered rotate/crop/trim frame, background save with audio and metadata, cancel and original preservation passed on private Xvfb')
        finally:
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            service.shutdown();library.db.close()
