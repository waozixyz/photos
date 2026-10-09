"""Prepare verified public model assets; no photo library is opened or sent."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, help='Copy already downloaded models without network access')
parser.add_argument('--destination', type=Path, default=root / 'build/models/face')
args = parser.parse_args()
args.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
manifest = json.loads((root / 'assets/FACE-MODELS.json').read_bytes())
for item in manifest['files']:
    target = args.destination / item['name']
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == item['sha256']:
        continue
    fd, name = tempfile.mkstemp(prefix='.model-', dir=args.destination)
    temporary = Path(name)
    try:
        incoming = (args.source / item['name']).open('rb') if args.source else urllib.request.urlopen(item['source'], timeout=60)
        sha, total = hashlib.sha256(), 0
        with incoming, os.fdopen(fd, 'wb') as output:
            for block in iter(lambda: incoming.read(1024 * 1024), b''):
                total += len(block)
                if total > item['bytes']:
                    raise ValueError('Model exceeds its verified size')
                sha.update(block); output.write(block)
            output.flush(); os.fsync(output.fileno())
        if total != item['bytes'] or sha.hexdigest() != item['sha256']:
            raise ValueError('Model integrity check failed')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
shutil.copyfile(root / 'assets/SFACE-LICENSE.txt', args.destination / 'SFACE-LICENSE.txt')
shutil.copyfile(root / 'assets/YUNET-LICENSE.txt', args.destination / 'YUNET-LICENSE.txt')
print('Local face recognition models verified and ready')
