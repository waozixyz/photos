"""Private-display recovery save and restore using synthetic account keys."""
import ast,json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,os.environ.get('PHOTOS_SERVICE_PACKAGE',str(ROOT)))
from photos.library import Library,digest_file,LibraryError
from photos.sync import Client
from photos import recovery
from photos.service import Server
import photos.service as service_module
assert os.environ.get('PHOTOS_PRIVATE_XVFB')=='1'
portable=os.environ.get('PHOTOS_RECOVERY_PORTABLE')=='1'
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
    config=dict(root=str(library.root),node='http://127.0.0.1:18080',node_id='a'*64)
    client=Client(library,config);keys=client.identity_path.read_bytes();device=client.device_path.read_bytes()
    destination=folder/'fixture.photos-recovery';password='synthetic recovery password'
    calls=[];original_process=service_module.process
    def record(request,config):
        try:result=original_process(request,config)
        except LibraryError as error:
            calls.append((request,dict(error=str(error))));raise
        calls.append((request,result));return result
    service_module.process=record
    if portable:
        flags=[] if (ROOT/'ziran.local.toml').is_file() else ['--locked']
        kryon=Path(subprocess.check_output(['ziran','pkg','path','kryon',*flags],cwd=ROOT,text=True).strip())
        command=[os.environ.get('PHOTOS_PLAYER',str(kryon/'build/bin/zib')),os.environ.get('PHOTOS_BUNDLE',str(ROOT/'build/photos-portable.zib'))]
    else:command=[os.environ.get('PHOTOS_BINARY',str(ROOT/'build/photos-desktop'))]
    with Server(config,folder/'kryon/services/photos.sock') as service:
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
            control('Local sync',95,372);time.sleep(.5)
            control('Save recovery file',296,597);text(str(destination));control('Save',250,339)
            text(password)
            if not portable:assert password not in json.dumps(state())
            subprocess.run(['import','-window',window,str(output/('recovery-password-'+('portable' if portable else 'native')+'.png'))],check=True,timeout=5)
            control('Save',250,339);text('mismatched recovery password');control('Save',250,339)
            assert not destination.exists();assert not result('recovery_save')
            text(password);since=len(calls);control('Save',250,339)
            saved=wait(lambda:result('recovery_save',since),'Recovery file did not save');assert 'error' not in saved,saved.get('error')
            assert destination.read_bytes().startswith(recovery.MAGIC)
            peer=Library(folder/'recovered')
            try:
                recovery.restore(peer,config,{},str(destination),password)
                assert Client(peer,config).identity==client.identity
            finally:peer.db.close()
            control('Restore backup keys',489,597);text(str(destination));control('Save',250,339)
            text(password);since=len(calls);control('Save',250,339)
            refused=wait(lambda:result('recovery_restore',since),'Existing-library restore was not checked')
            assert 'new empty library' in refused.get('error','')
            control('Cancel',350,339)
            # Switch this disposable service to a new library and exercise
            # actual UI restore, including retry after an incorrect password.
            restored=Library(folder/'ui-restored')
            config['root']=str(restored.root)
            try:
                control('Restore backup keys',489,597);text(str(destination));control('Save',250,339)
                text('incorrect recovery fixture password');since=len(calls);control('Save',250,339)
                rejected=wait(lambda:result('recovery_restore',since),'Incorrect recovery password was not checked')
                assert 'Incorrect recovery password' in rejected.get('error','')
                assert not (restored.root/'private/identity.json').exists()
                text(password);since=len(calls);control('Save',250,339)
                restored_result=wait(lambda:result('recovery_restore',since),'New-library recovery did not finish')
                assert 'error' not in restored_result,restored_result.get('error')
                assert Client(restored,config).identity==client.identity
            finally:restored.db.close()
            control('Save recovery file',296,597);text(str(folder/'cancelled.photos-recovery'));control('Save',250,339)
            text(password);control('Cancel',350,339)
            assert not (folder/'cancelled.photos-recovery').exists()
            assert client.identity_path.read_bytes()==keys and client.device_path.read_bytes()==device
            for path,data in originals.items():assert path.read_bytes()==data
            time.sleep(.5)
            subprocess.run(['import','-window',window,str(output/('recovery-'+('portable' if portable else 'native')+'.png'))],check=True,timeout=5)
            print('Private Xvfb '+('portable' if portable else 'native')+': masked recovery password, confirmation, encrypted key file, restore refusal, cancellation and unchanged originals/keys passed')
        except Exception:
            if child.poll() is None:
                subprocess.run(['import','-window',window,str(output/('recovery-failed-'+('portable' if portable else 'native')+'.png'))],check=True,timeout=5)
            print('Synthetic recovery requests:',[(q.get('action'),r.get('error')) for q,r in calls])
            raise
        finally:
            if child.poll() is None:child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            child.stderr.close();service.shutdown();library.db.close()
