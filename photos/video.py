"""Headless video copies and bounded frame previews; never rewrite an original."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import secrets
import subprocess
import tempfile
import threading
import time

from . import editing
from .library import Library, LibraryError, digest_file, packed

jobs = {}
guard = threading.Lock()


def start(library, photo_id, value, vault_token):
    photo = library.photo(photo_id)
    if photo['kind'] != 'video' or photo['trashed']:
        raise LibraryError('Choose a video outside Trash to edit')
    with guard:
        for key, existing in list(jobs.items()):
            if existing['done'] and time.monotonic()-existing['finished'] > 3600:
                jobs.pop(key)
        if any(j['root'] == str(library.root) and not j['done'] for j in jobs.values()):
            raise LibraryError('A video copy is already processing; wait or cancel it first')
        token = secrets.token_hex(16)
        job = dict(root=str(library.root), id=photo_id, vault=vault_token, done=False,
                   cancelled=False, process=None, error='', saved_copy='', finished=0)
        jobs[token] = job
    def run():
        from . import vault
        worker = Library(job['root'])
        try:
            def authorize(photo):
                return not job['cancelled'] and (not photo['hidden'] or vault.unlocked(worker, vault_token))
            if not authorize(worker.photo(photo_id)):
                raise LibraryError('Hidden is locked or video editing was cancelled')
            copy = save(worker, photo_id, value, authorize=authorize, job=job)
            with guard:
                job['saved_copy'] = copy
        except LibraryError as error:
            with guard:
                job['error'] = str(error)
        except Exception:
            with guard:
                job['error'] = 'Video processing could not finish; your original is safe'
        finally:
            worker.db.close()
            with guard:
                job.update(done=True, finished=time.monotonic(), process=None)
    threading.Thread(target=run, daemon=True).start()
    return token


def state(library, token, vault_token='', cancel=False):
    from . import vault
    if not isinstance(token, str) or not editing.TOKEN.fullmatch(token):
        raise LibraryError('Invalid video processing session')
    with guard:
        job = jobs.get(token)
        if job is None or job['root'] != str(library.root):
            return dict(video_job='', video_working=0,
                        error='Video session ended; check the library before retrying')
        hidden = library.photo(job['id'])['hidden'] or (job['saved_copy'] and library.photo(job['saved_copy'])['hidden'])
        if hidden and (vault_token != job['vault'] or not vault.unlocked(library, vault_token)):
            if not job['done']:
                job['cancelled'] = True
                process = job['process']
                if process is not None and process.poll() is None:
                    process.kill()
            return dict(video_job='', video_working=0, error='Hidden is locked; video editing stopped')
        if cancel and not job['done']:
            job['cancelled'] = True
            process = job['process']
            if process is not None and process.poll() is None:
                process.kill()
        return dict(video_job='' if job['done'] else token, video_working=int(not job['done']),
                    saved_copy=job['saved_copy'], error='' if job['cancelled'] else job['error'],
                    status='Edited video copy saved; original retained' if job['saved_copy'] else
                    ('Video editing cancelled; original retained' if job['cancelled'] else 'Processing video copy; originals stay intact'))


def environment():
    return {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}


def probe(path):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
                                 '-show_streams', '-show_format', '-of', 'json', str(path)],
                                env=environment(), capture_output=True, timeout=8)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ValueError()
        info = json.loads(result.stdout)
        stream = next(s for s in info['streams'] if s['codec_type'] == 'video')
        duration = float(stream.get('duration') or info['format']['duration'])
        if not math.isfinite(duration) or not 1 <= duration <= 604800:
            raise ValueError()
        width, height = int(stream['width']), int(stream['height'])
        if not 2 <= width <= 16384 or not 2 <= height <= 16384:
            raise ValueError()
        if stream.get('color_transfer') in {'smpte2084', 'arib-std-b67'} or stream.get('color_primaries') == 'bt2020':
            raise LibraryError('HDR video editing needs color conversion support; your original is retained')
        return dict(duration_ms=int(duration * 1000), width=width, height=height,
                    audio=any(s.get('codec_type') == 'audio' for s in info['streams']))
    except LibraryError:
        raise
    except (OSError, ValueError, KeyError, StopIteration, TypeError, subprocess.TimeoutExpired):
        raise LibraryError('This video cannot be edited; install FFmpeg and choose a video at least one second long') from None


def options(value, duration):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise LibraryError('Choose valid video adjustments') from None
    if not isinstance(value, list) or len(value) != 4 or any(type(v) is not int for v in value):
        raise LibraryError('Choose valid video adjustments')
    start, end, rotation, crop = value
    if not 0 <= start < end <= duration or end - start < 1000 or rotation not in (0, 90, 180, 270) or crop not in range(6):
        raise LibraryError('Choose a trim of at least one second within the video and valid rotation/crop')
    return value


def render(library, photo_id, value, preview=False, job=None):
    photo = library.photo(photo_id)
    if photo['kind'] != 'video' or photo['trashed']:
        raise LibraryError('Choose a video outside Trash to edit')
    info = probe(library.original(photo))
    adjustments = options(value, info['duration_ms'])
    package = Path(__file__).resolve().parents[1]
    program = next((p for p in (package/'video_edit', package/'build/video-editor/video_edit',
                                package/'video-editor/video_edit') if p.is_file()), None)
    if program is None:
        raise LibraryError('Build or install the Photos video editor before editing')
    folder = library.root/'private/edit-previews'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.video-', suffix='.png' if preview else '.mp4', dir=folder)
    os.close(fd)
    output = Path(name)
    try:
        # The Ziran helper replaces itself with FFmpeg, so timeout cleanup
        # kills the codec rather than leaving a detached child.
        with subprocess.Popen([str(program), str(library.original(photo)), str(output),
                               *map(str, adjustments), '1' if preview else '0'], env=environment(),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            if job is not None:
                with guard:
                    job['process'] = process
                    if job['cancelled']:
                        process.kill()
            try:
                code = process.wait(timeout=30 if preview else 1800)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
                raise
            finally:
                if job is not None:
                    with guard:
                        job['process'] = None
        if code or not output.stat().st_size:
            raise LibraryError('Video processing could not finish; your original is safe')
        return output, adjustments, info
    except (OSError, subprocess.TimeoutExpired):
        output.unlink(missing_ok=True)
        raise LibraryError('Video processing could not finish; your original is safe') from None
    except Exception:
        output.unlink(missing_ok=True)
        raise


def preview(library, photo_id, value, token=''):
    if token and not editing.TOKEN.fullmatch(token):
        raise LibraryError('Invalid video editing session')
    output, adjustments, info = render(library, photo_id, value, preview=True)
    try:
        # Reuse the image editor's private session namespace and cleanup rules.
        token = token or secrets.token_hex(16)
        folder = library.root/'private/edit-previews'/token
        if folder.is_symlink():
            raise LibraryError('Invalid video editing session')
        folder.mkdir(exist_ok=True, mode=0o700)
        path = folder/(hashlib.sha256((photo_id+packed(adjustments)).encode()).hexdigest()+'.png')
        os.replace(output, path)
        for old in sorted(folder.glob('*.png'), key=lambda p: p.stat().st_mtime, reverse=True)[2:]:
            old.unlink()
        folder.touch()
        return dict(edit_preview=str(path), edit_session=token, video_duration=info['duration_ms'])
    finally:
        output.unlink(missing_ok=True)


def save(library, photo_id, value, authorize=None, job=None):
    photo = library.photo(photo_id)
    info = probe(library.original(photo))
    adjustments = options(value, info['duration_ms'])
    if adjustments == [0, info['duration_ms'], 0, 0]:
        raise LibraryError('Make an adjustment before saving a copy')
    output, adjustments, info = render(library, photo_id, adjustments, job=job)
    try:
        result = probe(output)
        new_id = digest_file(output)
        with library.db:
            library.db.execute('BEGIN IMMEDIATE')
            photo = library.photo(photo_id)
            if authorize is not None and not authorize(photo):
                raise LibraryError('Hidden is locked; authenticate before saving hidden videos')
            if photo['trashed']:
                raise LibraryError('Restore the video from Trash before editing')
            if library.db.execute('SELECT id FROM photos WHERE id=?', (new_id,)).fetchone():
                return new_id
            metadata = json.loads(photo['metadata'])
            metadata['photosVideoEdit'] = dict(parent=photo_id, adjustments=adjustments)
            name = Path(photo['name']).stem.encode()[:480].decode('utf-8', 'ignore')+'-edited.mp4'
            destination = library.original(dict(id=new_id, extension='.mp4'))
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            size = output.stat().st_size
            # Copy as a binary file on disk; large videos never enter an RPC or memory buffer.
            temporary = destination.with_suffix('.next.mp4')
            try:
                shutil.copyfile(output, temporary)
                os.chmod(temporary, 0o600)
                with temporary.open('rb') as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            library.db.execute('''INSERT INTO photos(id,name,extension,kind,size,taken,width,height,metadata,albums,
                favorite,hidden,archived,caption,tags,updated,edits) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (new_id,name,'.mp4','video',size,photo['taken'],result['width'],result['height'],packed(metadata),
                 photo['albums'],photo['favorite'],photo['hidden'],photo['archived'],photo['caption'],photo['tags'],
                 time.time(),photo['edits']))
        library.preview(library.photo(new_id), destination)
        return new_id
    finally:
        output.unlink(missing_ok=True)
