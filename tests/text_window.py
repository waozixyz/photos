"""Real OCR selection, clipboard and revocation on a synthetic private display."""
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from photos.library import Library, digest_file
from photos.service import Server
import photos.service as service_module
from test_ocr import text_image

assert os.environ.get('PHOTOS_PRIVATE_XVFB')=='1'
portable=os.environ.get('PHOTOS_TEXT_PORTABLE')=='1'
output=ROOT/'build/screenshots';output.mkdir(parents=True,exist_ok=True)


def wait(check,reason,timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=check()
        if value:return value
        time.sleep(.05)
    raise AssertionError(reason)


with tempfile.TemporaryDirectory() as directory:
    folder=Path(directory);source=folder/'source';source.mkdir()
    photo=source/'Text.png';text_image(photo);original=photo.read_bytes()
    library=Library(folder/'library');library.import_folders([source]);photo_id=digest_file(photo)
    calls=[];original_process=service_module.process
    def record(request,config):
        result=original_process(request,config);calls.append((request,result));return result
    service_module.process=record
    env={**os.environ,'KRYON_SERVICE_ROOT':str(folder)}
    if portable:
        flags=[] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon=Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        command=[os.environ.get('PHOTOS_PLAYER',str(kryon/'build/bin/zx')),
                 os.environ.get('PHOTOS_BUNDLE',str(ROOT/'build/photos-portable.zx'))]
    else:command=[os.environ.get('PHOTOS_BINARY',str(ROOT/'build/photos-desktop'))]
    with Server(dict(root=str(library.root)),folder/'kryon/services/photos.sock') as server:
        threading.Thread(target=server.serve_forever,daemon=True).start()
        child=subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def xdo(*args):return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()
        def locate():
            assert child.poll() is None, child.stderr.read().decode()
            result=subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode==0 else None
        try:
            window=wait(locate,'OCR app did not open')
            xdo('windowsize',window,1180,880);xdo('windowmove',window,0,0);xdo('windowfocus',window)
            def click(x,y):
                xdo('mousemove','--window',window,x,y);xdo('mousedown',1);time.sleep(.12);xdo('mouseup',1);time.sleep(.25)
            def state():
                raw=subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
                return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}
            def control(label,x,y):
                if not portable:
                    item=wait(lambda:next((c for c in state().get('controls',[]) if c['label']==label and c['enabled']),None),'Control missing: '+label)
                    x=item['x']+item['width']/2;y=item['y']+item['height']/2
                click(x,y)
            def shoot(name):
                path=output/name;subprocess.run(['import','-window',window,str(path)],check=True,timeout=5)
                with Image.open(path) as frame:return frame.convert('RGB').copy()
            wait(lambda:calls,'Gallery missing');time.sleep(.6);click(260,320)
            wait(lambda:any(q.get('action')=='open' and r.get('detail',{}).get('id')==photo_id for q,r in calls),'Photo did not open')
            time.sleep(.5);control('Detect text',1105,570)
            result=wait(lambda:next((r for q,r in reversed(calls) if q.get('action')=='detect_text' and r.get('text_words')),None),'OCR results missing')
            time.sleep(.5)
            words=result['text_words'];assert 'HELLO' in [w['text'] for w in words]
            if not portable:wait(lambda:state().get('detected_words')==len(words),'Overlay did not receive words')
            shoot('text-overlay-portable.png' if portable else 'text-overlay-native.png')
            canvas=(192,326,976,380)
            if not portable:
                c=next(c for c in state()['controls'] if c['label']=='Detected text area');canvas=(c['x'],c['y'],c['width'],c['height'])
            x,y,w,h=canvas;scale=min(w/result['text_width'],h/result['text_height'])
            sw,sh=result['text_width']*scale,result['text_height']*scale;sx=x+(w-sw)/2;sy=y+(h-sh)/2
            def point(word):return sx+sw*(word['x']+word['width']/2)/10000,sy+sh*(word['y']+word['height']/2)/10000
            hello=next(w for w in words if w['text']=='HELLO');photos=next(w for w in words if w['text']=='PHOTOS')
            click(*point(hello));control('Copy selection',680,220)
            copied=subprocess.check_output(['xclip','-selection','clipboard','-o'],text=True,timeout=5)
            assert copied=='HELLO',repr(copied)
            start=point(hello);end=point(photos)
            xdo('mousemove','--window',window,*start);xdo('mousedown',1);time.sleep(.15)
            xdo('mousemove','--window',window,*end);time.sleep(.2);xdo('mouseup',1);time.sleep(.4)
            control('Copy selection',680,220)
            copied=subprocess.check_output(['xclip','-selection','clipboard','-o'],text=True,timeout=5)
            assert copied=='HELLO PHOTOS',repr(copied)
            # Double-clicking one word selects its recognized line.
            xdo('mousemove','--window',window,*point(hello));xdo('mousedown',1);time.sleep(.08);xdo('mouseup',1)
            time.sleep(.08);xdo('mousedown',1);time.sleep(.08);xdo('mouseup',1);time.sleep(.4)
            control('Copy selection',680,220)
            copied=subprocess.check_output(['xclip','-selection','clipboard','-o'],text=True,timeout=5)
            assert copied=='HELLO PHOTOS',repr(copied)
            control('Select all',350,220);control('Copy selection',680,220)
            copied=subprocess.check_output(['xclip','-selection','clipboard','-o'],text=True,timeout=5)
            assert 'HELLO PHOTOS' in copied and 'LOCAL 12345' in copied,repr(copied)
            # Recognition never starts automatically for an entire library.
            assert sum(q.get('action')=='detect_text' for q,r in calls)==1
            at=len(calls);control('Language: English',286,264)
            wait(lambda:any(q.get('action')=='detect_text' and q.get('value')=='jpn' for q,r in calls[at:]),'Language switch did not rescan locally')
            wait(lambda:any(q.get('action')=='text_check' for q,r in calls[at:]),'Text access was not rechecked')
            time.sleep(.4);shoot('text-selected-portable.png' if portable else 'text-selected-native.png')
            # A concurrent hide must revoke the viewer's extracted text.
            library.edit(photo_id,'hidden_set','1')
            if not portable:
                wait(lambda:state().get('editor')!='text' and state().get('detected_words')==0,'Hidden text remained visible')
            else:
                wait(lambda:any(q.get('action')=='text_check' and r.get('error') for q,r in calls),'Portable hidden revocation was not requested')
                time.sleep(.6)
                # Back in the viewer, the old Copy control must disappear.
                at=len(calls);click(680,220);time.sleep(.5)
                assert not any(q.get('action')=='detect_text' for q,r in calls[at:])
            assert photo.read_bytes()==original
            assert library.original(library.photo(photo_id)).read_bytes()==original
            print(('Portable' if portable else 'Native')+' OCR: actual local recognition, word/drag/line/all selection, clipboard, language switch, privacy revocation and original preservation passed')
        finally:
            if sys.exc_info()[0] is not None and child.poll() is None:
                try:shoot('text-failed-portable.png' if portable else 'text-failed-native.png')
                except (OSError,subprocess.SubprocessError):pass
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            if child.returncode not in (0,-15):print('OCR process exit:',child.returncode,child.stderr.read().decode()[-3000:])
            server.shutdown();library.db.close()
