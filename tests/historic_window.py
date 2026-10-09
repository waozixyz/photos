"""Historic date editing, search and jumps on synthetic private-display windows."""
import ast,json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,os.environ.get('PHOTOS_SERVICE_PACKAGE',str(ROOT)))
from photos.library import Library,digest_file
from photos.service import Server
import photos.service as service_module
assert os.environ.get('PHOTOS_PRIVATE_XVFB')=='1'
portable=os.environ.get('PHOTOS_HISTORIC_PORTABLE')=='1'
output=ROOT/'build/screenshots';output.mkdir(exist_ok=True)
def wait(check,reason):
    end=time.monotonic()+20
    while time.monotonic()<end:
        value=check()
        if value:return value
        time.sleep(.05)
    raise AssertionError(reason)
with tempfile.TemporaryDirectory() as temporary:
    folder=Path(temporary);source=folder/'source/Family';source.mkdir(parents=True)
    ids=[];originals={}
    for i in range(2):
        path=source/f'Historic {i}.png';Image.new('RGB',(300+i,200),(70+i*50,150,100)).save(path)
        ids.append(digest_file(path));originals[path]=path.read_bytes()
    library=Library(folder/'library');library.import_folders([source.parent])
    for identity,date in zip(ids,['1969-07-03T12:00:00Z','1900-03-01T12:00:00Z']):library.edit(identity,'taken',date)
    calls=[];original_process=service_module.process
    def record(request,config):
        result=original_process(request,config);calls.append((request,result));return result
    service_module.process=record
    if portable:
        flags=[] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon=Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        command=[os.environ.get('PHOTOS_PLAYER',str(kryon/'build/bin/zib')),os.environ.get('PHOTOS_BUNDLE',str(ROOT/'build/photos-portable.zib'))]
    else:command=[os.environ.get('PHOTOS_BINARY',str(ROOT/'build/photos-desktop'))]
    with Server(dict(root=str(library.root)),folder/'kryon/services/photos.sock') as service:
        threading.Thread(target=service.serve_forever,daemon=True).start()
        child=subprocess.Popen(command,env={**os.environ,'KRYON_SERVICE_ROOT':str(folder)},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def xdo(*args):return subprocess.check_output(['xdotool',*map(str,args)],text=True).strip()
        def locate():
            assert child.poll() is None,child.stderr.read().decode()
            result=subprocess.run(['xdotool','search','--onlyvisible','--pid',str(child.pid)],capture_output=True,text=True)
            return result.stdout.splitlines()[0] if result.returncode==0 else None
        def state():
            raw=subprocess.check_output(['xprop','-id',window,'_HARMONY_APP_STATE'],text=True)
            return json.loads(ast.literal_eval(raw.split(' = ',1)[1].strip())) if ' = ' in raw else {}
        def control(label,x,y):
            if not portable:
                def ready():
                    current=state()
                    return next((c for c in current.get('controls',[]) if c['label']==label and c['enabled']),None) if not current.get('loading') else None
                item=wait(ready,'Missing historic control: '+label);x=item['x']+item['width']/2;y=item['y']+item['height']/2
            xdo('mousemove','--window',window,int(x),int(y));xdo('mousedown',1);time.sleep(.12);xdo('mouseup',1);time.sleep(.35)
        def text(value,search=False):
            xdo('mousemove','--window',window,400,30 if search else 244);xdo('click',1)
            # The portable player samples key state once per frame. Hold the
            # shortcut long enough to observe both its modifier and key.
            xdo('keydown','ctrl');time.sleep(.12);xdo('keydown','a');time.sleep(.12)
            xdo('keyup','a');xdo('keyup','ctrl');time.sleep(.12)
            xdo('type','--clearmodifiers','--delay','30','--',value);time.sleep(.2)
        def result(action,since=0):return next((r for q,r in reversed(calls[since:]) if q.get('action')==action),None)
        try:
            window=wait(locate,'Historic test did not open');xdo('windowsize',window,1180,880);xdo('windowmove',window,0,0);xdo('windowfocus',window)
            wait(lambda: result('query'),'Historic gallery did not load');time.sleep(.5)
            control('Historic 0.png',300,310);wait(lambda:result('open'),'Historic original did not open')
            control('Edit date',270,792);text('1890-04-30T23:00:00-02:00');since=len(calls);control('Save',250,339)
            edited=wait(lambda:result('taken',since),'Historic date edit did not finish');assert 'error' not in edited,edited.get('error')
            assert library.details(ids[0])['taken']=='1890-05-01T01:00:00+00:00'
            control('Back',240,166);time.sleep(.5);text('year:1890',search=True);since=len(calls);control('Find',1130,30)
            found=wait(lambda:next((r for q,r in reversed(calls[since:]) if q.get('action')=='query' and q.get('search')=='year:1890'),None),'Historic structured search did not finish')
            assert [p['id'] for p in found['photos']]==ids[:1]
            control('Jump to date',600,166);text('1890-05-01');since=len(calls);control('Save',250,339)
            jumped=wait(lambda:result('jump_date',since),'Historic jump did not finish');assert jumped['photos'][0]['id']==ids[0] and jumped['jump_offset']==0
            time.sleep(.5);suffix='portable' if portable else 'native'
            subprocess.run(['import','-window',window,str(output/('historic-'+suffix+'.png'))],check=True,timeout=5)
            for path,data in originals.items():assert path.read_bytes()==data
            print('Private Xvfb '+suffix+': historic date correction, explicit timezone, structured year search, date jump and unchanged originals passed')
        except Exception:
            if child.poll() is None:
                subprocess.run(['import','-window',window,str(output/('historic-failed-'+('portable' if portable else 'native')+'.png'))],check=True,timeout=5)
            print('Synthetic historic requests:',[(q.get('action'),r.get('error')) for q,r in calls])
            raise
        finally:
            if child.poll() is None:child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            child.stderr.close();service.shutdown();library.db.close()
