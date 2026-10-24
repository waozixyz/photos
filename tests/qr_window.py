"""Real QR recognition, selection, clipboard and revocation on private Xvfb."""
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
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from photos.library import Library, LibraryError, digest_file
from photos.service import Server
import photos.service as service_module
from qr_fixtures import qr_image, CONTENTS

assert os.environ.get('PHOTOS_PRIVATE_XVFB')=='1'
portable = os.environ.get('PHOTOS_QR_PORTABLE')=='1'
suffix = 'portable' if portable else 'native'
output = ROOT/'build/screenshots';output.mkdir(exist_ok=True)


def wait(check,reason,timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=check()
        if value:return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as directory:
    folder=Path(directory);source=folder/'source';source.mkdir()
    photo=source/'Codes.png';qr_image(photo);original=photo.read_bytes()
    library=Library(folder/'library');library.import_folders([source]);photo_id=digest_file(photo)
    calls=[];hold=threading.Event();held=threading.Event();release=threading.Event();slow=threading.Event()
    original_process=service_module.process
    def record(request,config):
        if (request.get('action')=='qr_check' and hold.is_set()) or (request.get('action')=='detect_qr' and slow.is_set()):
            held.set();release.wait(8)
        try:result=original_process(request,config)
        except LibraryError as error:
            calls.append((request,dict(error=str(error))));raise
        calls.append((request,result));return result
    service_module.process=record
    env={**os.environ,'KRYON_SERVICE_ROOT':str(folder)}
    if portable:
        flags=[] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon=Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        command=[os.environ.get('PHOTOS_PLAYER',str(kryon/'build/bin/zx')),os.environ.get('PHOTOS_BUNDLE',str(ROOT/'build/photos-portable.zx'))]
    else:command=[os.environ.get('PHOTOS_BINARY',str(ROOT/'build/photos-desktop'))]
    with Server(dict(root=str(library.root)),folder/'kryon/services/photos.sock') as server:
        threading.Thread(target=server.serve_forever,daemon=True).start()
        child=subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def xdo(*args):return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()
        def locate():
            assert child.poll() is None,child.stderr.read().decode()
            result=subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode==0 else None
        try:
            window=wait(locate,'QR app did not open')
            xdo('windowsize',window,1180,880);xdo('windowmove',window,0,0);xdo('windowfocus',window)
            def click(x,y):
                xdo('mousemove','--window',window,int(x),int(y));xdo('mousedown',1);time.sleep(.12);xdo('mouseup',1);time.sleep(.25)
            def state():
                raw=subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
                return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}
            def control(label,x,y):
                if not portable:
                    item=wait(lambda:next((c for c in state().get('controls',[]) if c['label']==label and c['enabled']),None),'Control missing: '+label)
                    x=item['x']+item['width']/2;y=item['y']+item['height']/2
                click(x,y)
            def result(action,since=0):return next((r for q,r in reversed(calls[since:]) if q.get('action')==action),None)
            def shot(name):
                path=output/f'qr-{name}-{suffix}.png'
                subprocess.run(['import','-window',window,str(path)],check=True,timeout=5)
                with Image.open(path) as image:return image.convert('RGB').crop((192,636,1160,790)).tobytes()
            def clipboard():return subprocess.check_output(['xclip','-selection','clipboard','-o'],text=True,timeout=5)
            wait(lambda:calls,'Gallery missing');time.sleep(.6);click(260,320)
            wait(lambda:(result('open') or {}).get('detail',{}).get('id')==photo_id,'Photo did not open')
            time.sleep(.5);since=len(calls);control('Detect QR',976,570)
            detected=wait(lambda:(result('detect_qr',since) or {}).get('qr_codes'),'QR results missing')
            response=result('detect_qr',since);assert len(detected)==3;time.sleep(.6)
            if not portable:wait(lambda:state().get('detected_codes')==3,'Overlay did not receive codes')
            shot('overlay')
            canvas=(192,284,976,348)
            if not portable:
                c=next(c for c in state()['controls'] if c['label']=='Detected QR area');canvas=(c['x'],c['y'],c['width'],c['height'])
            x,y,w,h=canvas;scale=min(w/response['qr_width'],h/response['qr_height'])
            sw,sh=response['qr_width']*scale,response['qr_height']*scale;sx=x+(w-sw)/2;sy=y+(h-sh)/2
            for code in detected:
                click(sx+sw*(code['x']+code['width']/2)/10000,sy+sh*(code['y']+code['height']/2)/10000)
                control('Copy contents',680,220);assert clipboard()==code['text'],'Selected payload was not copied intact'
            control('Next code',1010,220);control('Copy contents',680,220)
            assert clipboard()==detected[0]['text'],'Next did not wrap'
            control('Previous code',350,220);control('Copy contents',680,220)
            assert clipboard()==detected[-1]['text'],'Previous did not wrap'
            assert len([q for q,r in calls if q.get('action')=='detect_qr'])==1,'Navigation rescanned the library'
            if not portable:
                inspection=json.dumps(state(),ensure_ascii=False)
                for text in CONTENTS:assert text not in inspection,'QR contents leaked into app inspection'
            xdo('key','Escape');time.sleep(.5)
            if not portable:assert state()['editor']=='' and state()['detected_codes']==0
            # A privacy edit revokes displayed QR payloads.
            since=len(calls);control('Detect QR',976,570)
            wait(lambda:(result('detect_qr',since) or {}).get('qr_codes'),'QR restart missing');time.sleep(.6)
            library.edit(photo_id,'hidden_set','1');since=len(calls)
            wait(lambda:(result('qr_check',since) or {}).get('error'),'Hide did not revoke QR access')
            if not portable:wait(lambda:state().get('editor')!='qr' and state().get('detected_codes')==0,'Hidden QR contents remained visible')
            time.sleep(.6);shot('revoked')
            library.edit(photo_id,'hidden_set','0')
            # A stalled service expires payload access by elapsed time, including Copy.
            since=len(calls);control('Detect QR',976,570)
            wait(lambda:(result('detect_qr',since) or {}).get('qr_codes'),'QR restart after hide missing');time.sleep(.6)
            control('Copy contents',680,220);last=clipboard();before=shot('lease')
            hold.set();wait(held.is_set,'Privacy check did not reach the service');time.sleep(3)
            if not portable:assert state().get('editor')!='qr' and state().get('detected_codes')==0,'Stalled service retained QR payloads'
            assert shot('expired')!=before,'Expired QR preview was still rendered'
            click(680,220);assert clipboard()==last,'Expired Copy changed clipboard'
            hold.clear();release.set();time.sleep(.8)
            if not portable:assert state().get('editor')!='qr' and state().get('detected_codes')==0,'Late access check resurrected QR payloads'
            # Cancel while recognition is in flight; its late result must be discarded.
            held.clear();release.clear();slow.set();control('Detect QR',976,570)
            wait(held.is_set,'Slow scan did not start');xdo('key','Escape');time.sleep(.3)
            slow.clear();release.set();time.sleep(.8)
            if not portable:assert state().get('editor')!='qr' and state().get('detected_codes')==0,'Cancelled scan resurrected QR contents'
            shot('cancelled')
            assert photo.read_bytes()==original
            assert library.original(library.photo(photo_id)).read_bytes()==original
            print(('Portable' if portable else 'Native')+' QR: actual recognition, multiple boxes, Unicode/multiline clipboard, navigation, privacy revocation, elapsed access expiry, cancelled scan and original preservation passed')
        except Exception:
            if child.poll() is None:shot('failure')
            print('Recent synthetic requests:',[(q.get('action'),r.get('error')) for q,r in calls[-8:]],flush=True)
            raise
        finally:
            release.set();child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            if child.returncode not in (0,-15):print('QR process exit:',child.returncode,child.stderr.read().decode()[-3000:])
            server.shutdown();library.db.close()
