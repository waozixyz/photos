"""Opt-in local semantic indexing; native Ziran owns inference and ranking."""
import fcntl
import base64
import binascii
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading

from PIL import Image, ImageOps

from .library import Library, LibraryError, digest_file, packed

MODEL = 'clip-vit-b32-int8-v1'
_mutex = threading.Lock()
_jobs = {}
_verified = {}


def models():
    package = Path(__file__).resolve().parents[1]
    root = next((p for p in (package / 'models', package / 'build/models') if p.is_dir()), None)
    if root is None:
        raise LibraryError('Install the local semantic search models before enabling search')
    manifest = json.loads((package / 'assets/SEMANTIC-MODELS.json').read_bytes())
    if manifest['model'] != MODEL:
        raise LibraryError('The semantic search models need an update')
    for item in manifest['files']:
        path = root / item['name']
        try:
            stamp = (str(path), path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        except OSError:
            raise LibraryError('Install all local semantic search models before enabling search') from None
        if stamp not in _verified:
            if path.is_symlink() or stamp[1] != item['bytes'] or digest_file(path) != item['sha256']:
                raise LibraryError('A semantic search model failed its integrity check; reinstall the models')
            _verified[stamp] = True
    return root


def runtime():
    # Finding the library without importing ONNX Runtime avoids its Python
    # initialization/telemetry. The Ziran helper explicitly disables telemetry.
    module = importlib.util.find_spec('onnxruntime')
    if module is None or not module.origin:
        raise LibraryError('Install the local semantic search runtime before enabling search')
    libraries = sorted((Path(module.origin).parent / 'capi').glob('libonnxruntime.so.*'))
    if not libraries:
        raise LibraryError('The local semantic search runtime is unavailable on this host')
    return libraries[-1]


def program():
    package = Path(__file__).resolve().parents[1]
    paths = [package / 'semantic_infer', package / 'build/semantic-infer/semantic_infer']
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        raise LibraryError('Build or install the native semantic search engine')
    return path


def prepare():
    models(); runtime(); program()
    try:
        import numpy
        from tokenizers import Tokenizer
    except ImportError:
        raise LibraryError('Install the local semantic search dependencies') from None


def valid_embedding(blob):
    if not isinstance(blob, bytes) or len(blob) != 2048:
        return False
    values = struct.unpack('<512f', blob)
    return all(math.isfinite(v) and abs(v) <= 1.001 for v in values) and .99 <= sum(v*v for v in values) <= 1.01


def infer(library, mode, tensor):
    root = models()
    engine, library_path = program(), runtime()
    private = library.root / 'private/semantic'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {k: v for k, v in os.environ.items() if k not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    environment.update(OMP_NUM_THREADS='2', ORT_DISABLE_TELEMETRY='1')
    with tempfile.TemporaryDirectory(prefix='.infer-', dir=private) as directory:
        folder = Path(directory)
        source, target = folder / 'tensor', folder / 'embedding'
        source.write_bytes(tensor)
        name = 'vision_model_quantized.onnx' if mode == 'image' else 'text_model_quantized.onnx'
        try:
            result = subprocess.run(['prlimit', '--as=2147483648', '--cpu=60', '--', str(engine),
                str(library_path), mode, str(root / name), str(source), str(target),
                'pixel_values' if mode == 'image' else 'input_ids',
                'image_embeds' if mode == 'image' else 'text_embeds'],
                capture_output=True, env=environment, timeout=65)
            blob = target.read_bytes() if result.returncode == 0 else b''
        except (OSError, subprocess.TimeoutExpired):
            raise LibraryError('Local semantic processing could not finish; originals remain safe') from None
        if not valid_embedding(blob):
            raise LibraryError('Local semantic processing produced an invalid embedding')
        return blob


def text_tensor(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 512 or '\0' in text:
        raise LibraryError('Describe a photo in at most 512 bytes')
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(models() / 'tokenizer.json'))
    tokenizer.enable_truncation(max_length=77)
    tokenizer.enable_padding(length=77, pad_id=49407, pad_token='<|endoftext|>')
    ids = tokenizer.encode(text.strip()).ids
    if len(ids) != 77 or ids[0] != 49406 or 49407 not in ids:
        raise LibraryError('The semantic search tokenizer produced an invalid query')
    return struct.pack('<77q', *ids)


def image_tensor(library, photo):
    import numpy as np
    from . import decoding
    source = decoding.display(library, photo) if decoding.supported(photo['extension']) else library.original(photo)
    with Image.open(source) as original:
        if original.width * original.height > 100000000:
            raise LibraryError('This photo is too large for local semantic indexing')
        image = ImageOps.exif_transpose(original).convert('RGB')
        width, height = image.size
        # CLIP's documented bicubic shortest-edge resize and center crop.
        if width <= height:
            shape = (224, max(224, int(height * 224 / width)))
        else:
            shape = (max(224, int(width * 224 / height)), 224)
        image = image.resize(shape, Image.Resampling.BICUBIC)
        left, top = (image.width - 224) // 2, (image.height - 224) // 2
        image = image.crop((left, top, left + 224, top + 224))
        pixels = np.asarray(image, dtype=np.float32) / 255.0
        pixels = (pixels - np.array([.48145466, .4578275, .40821073], dtype=np.float32)) / np.array([.26862954, .26130258, .27577711], dtype=np.float32)
        return pixels.transpose(2, 0, 1).astype('<f4').tobytes()


def state(library):
    enabled = bool(library.setting('semantic_enabled', False))
    processing = enabled and bool(library.setting('semantic_processing', True))
    total, indexed, failed = library.db.execute('''SELECT COUNT(*),
        coalesce(SUM(s.embedding IS NOT NULL AND length(s.embedding)=2048),0),
        coalesce(SUM(s.error<>''),0) FROM photos p LEFT JOIN semantic_embeddings s
        ON s.photo_id=p.id AND s.model=? WHERE p.kind='photo' AND p.hidden=0 AND p.trashed=0''', (MODEL,)).fetchone()
    with _mutex:
        active = str(library.root) in _jobs
    status = library.setting('semantic_status', '')
    if not enabled:
        status = 'Enable semantic search to describe your photos'
    elif not processing:
        status = f'Indexing paused · {indexed} of {total} photos indexed locally'
    elif active:
        status = f'Indexing locally · {indexed} of {total} photos · {failed} unavailable'
    elif not active and indexed + failed >= total:
        status = f'{indexed} of {total} photos indexed locally' + (f' · {failed} unavailable; retry after restoring originals' if failed else '')
    return dict(semantic_enabled=int(enabled), semantic_processing=int(processing), semantic_working=int(active), semantic_total=total,
                semantic_indexed=indexed, semantic_failed=failed, semantic_status=status or 'Waiting to index local photos')


def enable(library, enabled):
    if enabled:
        prepare()
    if enabled:
        library.set_setting('semantic_enabled', True)
    library.set_setting('semantic_processing', bool(enabled))
    if enabled:
        kick(library)


def retry(library):
    from . import native_edit
    native_edit.semantic_index(library, 'retry')
    enable(library, True)


def kick(library):
    if not library.setting('semantic_enabled', False) or not library.setting('semantic_processing', True):
        return
    root = str(library.root)
    with _mutex:
        if root in _jobs:
            return
        thread = threading.Thread(target=_run, args=(root,), name='Photos-semantic', daemon=True)
        _jobs[root] = thread
        thread.start()


def _run(root):
    library = None
    lock = None
    try:
        library = Library(root)
        (library.root / 'private').mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (library.root / 'private/semantic.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        prepare()
        while library.setting('semantic_enabled', False) and library.setting('semantic_processing', True):
            from . import native_edit
            identity = native_edit.semantic_index(library, 'next')
            if identity is None:
                break
            photo = dict(library.photo(identity))
            if photo['hidden'] or photo['trashed']:
                continue
            library.set_setting('semantic_status', 'Indexing local photos; you can keep browsing')
            blob, error = None, ''
            try:
                if not library.original(photo).is_file():
                    raise LibraryError('Original not present locally')
                blob = infer(library, 'image', image_tensor(library, photo))
            except Exception:
                error = 'Local original could not be indexed'
            native_edit.semantic_index(library, 'failure' if error else 'publish', photo, blob)
        library.set_setting('semantic_status', 'Local semantic indexing complete')
    except Exception:
        if library is not None:
            library.set_setting('semantic_status', 'Semantic indexing paused; check the local models and runtime')
    finally:
        if lock is not None:
            lock.close()
        if library is not None:
            library.db.close()
        with _mutex:
            _jobs.pop(root, None)


def search_page(library, request, page):
    if not library.setting('semantic_enabled', False):
        return dict(count=0, ids=[])
    text = request.get('search', '')
    if not isinstance(text, str) or not text.strip():
        return dict(count=0, ids=[])
    blob = infer(library, 'text', text_tensor(text))
    private = library.root / 'private/semantic'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(prefix='.query-', dir=private) as output:
        output.write(blob); output.flush()
        return page(library, **{**request, 'search': '', 'semantic_tensor': output.name})


def encrypted_index(library, photo_id):
    row = library.db.execute('SELECT embedding FROM semantic_embeddings WHERE photo_id=? AND model=?', (photo_id, MODEL)).fetchone()
    if not row or not valid_embedding(row[0]):
        return None
    # Keep the exact float32 bytes compact inside the encrypted record. Decimal
    # arrays could push a normal 50-photo sync batch above the node's body limit.
    return dict(model=MODEL, vector=base64.b64encode(row[0]).decode('ascii'))


def accept_index(library, photo_id, value):
    if value is None:
        library.db.execute('DELETE FROM semantic_embeddings WHERE photo_id=?', (photo_id,))
        return
    if not isinstance(value, dict) or value.get('model') != MODEL:
        raise ValueError('Invalid semantic search index')
    if isinstance(value.get('vector'), str) and len(value['vector']) == 2732:
        try:
            blob = base64.b64decode(value['vector'], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('Invalid semantic search index') from None
    else:
        # Accept the initial local release's encrypted decimal representation.
        values = value.get('values')
        if not isinstance(values, list) or len(values) != 512 or not all(type(v) in {int, float} and math.isfinite(v) and abs(v) <= 1.001 for v in values):
            raise ValueError('Invalid semantic search index')
        blob = struct.pack('<512f', *values)
    if not valid_embedding(blob):
        raise ValueError('Invalid semantic search index')
    library.db.execute('INSERT OR REPLACE INTO semantic_embeddings VALUES(?,?,?,?)', (photo_id, MODEL, blob, ''))
