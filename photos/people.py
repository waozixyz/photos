"""Opt-in private face grouping. Native Ziran owns pixels, models and matching."""
import base64
import binascii
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import threading
import time
from datetime import date

from .library import Library, LibraryError, digest_file, packed
from .semantic import runtime

MODEL = 'yunet-sface-v1'
_mutex = threading.Lock()
_jobs = {}
_verified = {}
HASH = re.compile('[0-9a-f]{64}')


def helper(name):
    package = Path(__file__).resolve().parents[1]
    paths = (package/name.replace('-', '_'), package/'build'/name/name.replace('-', '_'))
    result = next((p for p in paths if p.is_file()), None)
    if result is None:
        raise LibraryError('Build or install the local face recognition engine')
    return result


def models():
    package = Path(__file__).resolve().parents[1]
    folder = package/('models/face' if (package/'models/face').is_dir() else 'build/models/face')
    try:
        manifest = json.loads((package/'assets/FACE-MODELS.json').read_bytes())
        if manifest['model'] != MODEL:
            raise ValueError()
        for item in manifest['files']:
            path = folder/item['name']
            stamp = (str(path), path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
            if stamp not in _verified:
                if path.is_symlink() or stamp[1] != item['bytes'] or digest_file(path) != item['sha256']:
                    raise ValueError()
                _verified[stamp] = True
    except (OSError, KeyError, ValueError):
        raise LibraryError('Install verified local face models before enabling People') from None
    return folder


def prepare():
    models(); runtime()
    for name in ['face-infer', 'face-pixels', 'face-match']:
        helper(name)


def run(arguments):
    environment = {k:v for k,v in os.environ.items() if k not in {
        'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
    environment.update(OMP_NUM_THREADS='2', ORT_DISABLE_TELEMETRY='1')
    try:
        result = subprocess.run(['prlimit','--as=2147483648','--cpu=60','--',*map(str,arguments)],
                                env=environment,capture_output=True,timeout=65)
        if result.returncode:
            raise ValueError()
        return result.stdout
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise LibraryError('Local face processing could not finish; your original remains safe') from None


def valid_vector(blob):
    if not isinstance(blob, bytes) or len(blob) != 512:
        return False
    values = struct.unpack('<128f',blob)
    return all(math.isfinite(v) and abs(v)<=1.001 for v in values) and .99<=sum(v*v for v in values)<=1.01


def valid_rect(rect):
    return (isinstance(rect,list) and len(rect)==4 and all(type(v) is int for v in rect)
            and 0<=rect[0]<10000 and 0<=rect[1]<10000 and 0<rect[2]<=10000-rect[0]
            and 0<rect[3]<=10000-rect[1])


def visible(library, photo_id):
    from . import live
    photo = library.photo(photo_id)
    return photo['kind']=='photo' and all(not p['hidden'] and not p['trashed'] for p in live.components(library,photo))


def member_visible(library, photo_id):
    from . import live
    photo = library.photo(photo_id)
    # A Live Photo's motion belongs to its still rather than a second person item.
    link=json.loads(photo['metadata']).get('photosLive',{})
    return not (isinstance(link,dict) and link.get('photo')) and all(
        not p['hidden'] and not p['trashed'] for p in live.components(library,photo))


def tag(library, ids, name, remove=False):
    if not isinstance(ids,list) or not 0<len(ids)<=1000 or any(not isinstance(i,str) or not HASH.fullmatch(i) for i in ids):
        raise LibraryError('Select up to 1000 photos or videos to tag')
    if not isinstance(name,str) or not name.strip() or len(name.strip().encode())>512 or any(ord(c)<32 or ord(c)==127 for c in name):
        raise LibraryError('Enter one person name of at most 512 bytes')
    from . import native_edit
    return native_edit.people_change(library,'untag' if remove else 'tag',ids=ids,value=name.strip())['id']


def infer(library, photo, folder):
    from . import decoding
    original = library.original(photo)
    if not original.is_file():
        raise LibraryError('Original is not present locally')
    source = decoding.display(library,photo) if decoding.supported(photo['extension']) else original
    tensor, detections = folder/'input',folder/'detected'
    run([helper('face-pixels'),source,tensor,'-','prepare'])
    run([helper('face-infer'),runtime(),'detect',models()/'face_detection_yunet_2023mar.onnx',tensor,detections])
    faces = json.loads(run([helper('face-pixels'),source,folder,detections,'align']))['faces']
    if not isinstance(faces,list) or len(faces)>32:
        raise LibraryError('Too many faces for one photo; save a cropped copy to index this area')
    result = []
    for i, face in enumerate(faces):
        if not valid_rect(face.get('rect')):
            raise LibraryError('Invalid detected face area')
        target = folder/'embedding'
        run([helper('face-infer'),runtime(),'embed',models()/'face_recognition_sface_2021dec.onnx',folder/f'face-{i}.tensor',target])
        blob = target.read_bytes()
        if not valid_vector(blob):
            raise LibraryError('Invalid local face embedding')
        result.append(dict(rect=face['rect'],embedding=blob,portrait=folder/f'face-{i}.png'))
    return result


def store(library, photo_id, detected, folder, expected=None):
    from . import native_edit
    photo = dict(library.photo(photo_id)) if expected is None else expected
    if not isinstance(detected,list) or len(detected)>32:
        raise LibraryError('Choose up to 32 valid detected faces')
    records=[]
    for face in detected:
        if not isinstance(face,dict) or not valid_rect(face.get('rect')) or not valid_vector(face.get('embedding')):
            raise LibraryError('Choose valid detected face areas and vectors')
        records.append(dict(rect=face['rect'],vector=face['embedding'].hex()))
    saved=native_edit.people_change(library,'store',photo=photo_id,expected=struct.pack('<d',photo['updated']).hex(),faces=records)['saved']
    if saved:
        destination=library.root/'private/faces';destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        for index,face in enumerate(detected):
            portrait=Path(face['portrait']) if face.get('portrait') else None
            if portrait and portrait.is_file() and portrait.parent.resolve()==Path(folder).resolve():
                face_id=hashlib.sha256(f'{MODEL}:{photo_id}:{index}'.encode()).hexdigest()
                os.replace(portrait,destination/f'{face_id}.png')
    return bool(saved)


def enable(library, enabled):
    if enabled:
        prepare(); library.set_setting('people_enabled',True)
    library.set_setting('people_processing',bool(enabled))
    if enabled:
        from . import auto_albums
        auto_albums.apply(library)
        kick(library)


def kick(library):
    if not library.setting('people_enabled',False) or not library.setting('people_processing',False):
        return
    root = str(library.root)
    with _mutex:
        if root in _jobs:
            return
        thread = threading.Thread(target=_run,args=(root,),name='Photos-People',daemon=True)
        _jobs[root] = thread; thread.start()


def _run(root):
    library = None; lock = None
    try:
        library = Library(root)
        private = library.root/'private/people'; private.mkdir(parents=True,exist_ok=True,mode=0o700)
        lock = (private/'index.lock').open('a')
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        prepare()
        while library.setting('people_processing',False) and library.setting('people_enabled',False):
            from . import native_edit
            identity=native_edit.people_change(library,'next')['id']
            if not identity: break
            photo=dict(library.photo(identity))
            with tempfile.TemporaryDirectory(prefix='.infer-',dir=private) as temporary:
                folder=Path(temporary)
                try:
                    if not visible(library,photo['id']): raise LibraryError('Private component')
                    faces=infer(library,photo,folder)
                except Exception:
                    native_edit.people_change(library,'failure',photo=photo['id'],expected=struct.pack('<d',photo['updated']).hex())
                else:
                    store(library,photo['id'],faces,folder,expected=photo)
    except Exception:
        if library is not None:
            library.set_setting('people_status','Face indexing paused; check local models and runtime')
            library.set_setting('people_processing',False)
    finally:
        if lock is not None: lock.close()
        if library is not None: library.db.close()
        with _mutex: _jobs.pop(root,None)


def state(library):
    enabled = bool(library.setting('people_enabled',False)); processing = enabled and bool(library.setting('people_processing',False))
    total,indexed,failed = library.db.execute('''SELECT count(*),coalesce(sum(s.error=''),0),coalesce(sum(s.error<>''),0)
        FROM photos p LEFT JOIN face_scans s ON s.photo_id=p.id AND s.model=?
        WHERE p.kind='photo' AND p.hidden=0 AND p.trashed=0''',(MODEL,)).fetchone()
    with _mutex: active = str(library.root) in _jobs
    status = ('Enable People to group faces locally' if not enabled else
              f'{"Indexing locally" if active else ("Indexing paused" if not processing else "Local face indexing")} · {indexed} of {total} photos · {failed} unavailable')
    return dict(people_enabled=int(enabled),people_processing=int(processing),people_working=int(active),
                people_status=library.setting('people_status','') or status)


def catalog(library, search='', mode='active', offset=0, named_only=False):
    if not isinstance(search,str) or len(search.encode())>512 or mode not in {'active','ignored'}:
        raise LibraryError('Choose an active or ignored people view')
    rows = library.db.execute('''SELECT g.*,count(DISTINCT f.photo_id) count FROM people g
        JOIN person_photos f ON f.person_id=g.id JOIN photos p ON p.id=f.photo_id
        WHERE p.hidden=0 AND p.trashed=0 AND g.ignored=? GROUP BY g.id
        ORDER BY (g.name=''),g.name COLLATE NOCASE,g.id''',(int(mode=='ignored'),)).fetchall()
    result = []
    for group in rows:
        if named_only and not group['name']: continue
        if search.casefold() not in group['name'].casefold(): continue
        faces = library.db.execute('SELECT id,photo_id FROM faces WHERE person_id=? ORDER BY id',(group['id'],)).fetchall()
        visible_faces = [f for f in faces if visible(library,f['photo_id'])]
        members=[r[0] for r in library.db.execute('SELECT photo_id FROM person_photos WHERE person_id=? ORDER BY photo_id',(group['id'],)) if member_visible(library,r[0])]
        if not members:continue
        if visible_faces:
            face=visible_faces[0];portrait=library.root/'private/faces'/f'{face["id"]}.png'
            thumbnail=portrait if portrait.is_file() else library.thumbnail(face['photo_id'])
        else:thumbnail=library.thumbnail(members[0])
        result.append(dict(id=group['id'],name=group['name'] or 'Unnamed person',
                           count=len(members),thumbnail=str(thumbnail) if thumbnail.is_file() else '',
                           ignored=group['ignored'],pinned=0,archived=0,smart=0,place=0))
    return result[max(0,int(offset)):max(0,int(offset))+60],len(result)


def group(library, person):
    if not isinstance(person,str) or not HASH.fullmatch(person): raise LibraryError('Choose a person')
    row = library.db.execute('SELECT * FROM people WHERE id=?',(person,)).fetchone()
    if row is None: raise LibraryError('Person not found')
    return dict(row)


def edit(library, action, person, value='', ids=None):
    from . import native_edit
    if action=='person_name':
        if not isinstance(value,str) or not value.strip() or len(value.strip().encode())>512 or any(ord(c)<32 for c in value):
            raise LibraryError('Enter a person name of at most 512 bytes')
        value=value.strip()
    return native_edit.people_change(library,action,person=person,value=value,ids=ids)['id']


def valid_birth(value):
    if not isinstance(value,str): return False
    if value=='': return True
    try:
        return bool(re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',value)) and date(1600,1,1)<=date.fromisoformat(value)<=date.today()
    except ValueError: return False


def export_index(library, photo_id):
    scan = library.db.execute('SELECT * FROM face_scans WHERE photo_id=? AND model=? AND error=?',(photo_id,MODEL,'')).fetchone()
    tags=[group(library,r[0]) for r in library.db.execute('SELECT person_id FROM person_tags WHERE photo_id=? ORDER BY person_id',(photo_id,))]
    edited=library.db.execute('SELECT 1 FROM person_tag_edits WHERE photo_id=?',(photo_id,)).fetchone()
    if not scan and not tags and not edited:return None
    rows = library.db.execute('SELECT * FROM faces WHERE photo_id=? ORDER BY id',(photo_id,)).fetchall() if scan else []
    result = []
    for face in rows:
        result.append(dict(id=face['id'],rect=json.loads(face['rect']),vector=base64.b64encode(face['embedding']).decode('ascii'),
                           person=group(library,face['person_id']),excluded=json.loads(face['excluded'])))
    return dict(model=MODEL,faces=result,scanned=bool(scan),tags=tags)


def validate_person(person):
    if not isinstance(person,dict) or not isinstance(person['id'],str) or not HASH.fullmatch(person['id']):raise ValueError()
    if not isinstance(person['name'],str) or len(person['name'].encode())>512 or any(ord(c)<32 for c in person['name']):raise ValueError()
    if type(person['ignored']) is not int or person['ignored'] not in (0,1):raise ValueError()
    if type(person['updated']) not in (int,float) or not 0<=person['updated']<32503680000:raise ValueError()
    if not valid_birth(person.get('birth','')):raise ValueError()


def accept_person(library,person):
    prior=library.db.execute('SELECT updated,birth FROM people WHERE id=?',(person['id'],)).fetchone()
    if prior is None or prior[0]<person['updated']:
        birth=person.get('birth',prior['birth'] if prior else '')
        library.db.execute('INSERT OR REPLACE INTO people(id,name,ignored,updated,birth) VALUES(?,?,?,?,?)',
            (person['id'],person['name'],person['ignored'],person['updated'],birth))


def accept_index(library, photo_id, value):
    if value is None: return
    try:
        if not isinstance(value,dict) or value.get('model')!=MODEL or not isinstance(value.get('faces'),list) or len(value['faces'])>32:
            raise ValueError()
        scanned=value.get('scanned',True)
        if type(scanned) is not bool or (not scanned and value['faces']):raise ValueError()
        tags=value.get('tags')
        if tags is not None:
            if not isinstance(tags,list) or len(tags)>64:raise ValueError()
            tagged=set()
            for person in tags:
                validate_person(person)
                if not person['name'] or person['id'] in tagged:raise ValueError()
                tagged.add(person['id'])
        records = []; seen = set()
        for face in value['faces']:
            identity, person = face['id'],face['person']
            if not isinstance(identity,str) or not HASH.fullmatch(identity) or identity in seen: raise ValueError()
            seen.add(identity)
            if not valid_rect(face['rect']) or not isinstance(face['vector'],str) or len(face['vector'])!=684: raise ValueError()
            blob = base64.b64decode(face['vector'],validate=True)
            if not valid_vector(blob): raise ValueError()
            validate_person(person)
            excluded = face['excluded']
            if not isinstance(excluded,list) or len(excluded)>128 or any(not isinstance(i,str) or not HASH.fullmatch(i) for i in excluded): raise ValueError()
            prior = library.db.execute('SELECT photo_id FROM faces WHERE id=?',(identity,)).fetchone()
            if prior and prior[0]!=photo_id: raise ValueError()
            records.append((face,blob))
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise ValueError('Invalid private face index') from None
    if scanned:library.db.execute('DELETE FROM faces WHERE photo_id=?',(photo_id,))
    for face,blob in records:
        person = face['person']
        accept_person(library,person)
        library.db.execute('INSERT INTO faces VALUES(?,?,?,?,?,?)',(face['id'],photo_id,person['id'],packed(face['rect']),blob,packed(face['excluded'])))
    if scanned:library.db.execute('INSERT OR REPLACE INTO face_scans VALUES(?,?,?)',(photo_id,MODEL,''))
    if tags is not None:
        library.db.execute('DELETE FROM person_tags WHERE photo_id=?',(photo_id,))
        for person in tags:
            accept_person(library,person)
            library.db.execute('INSERT INTO person_tags VALUES(?,?)',(photo_id,person['id']))
        library.db.execute('INSERT OR IGNORE INTO person_tag_edits VALUES(?)',(photo_id,))
