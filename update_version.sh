#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 - "$@" <<'PY'
import json
import re
import sys
from pathlib import Path

if sys.argv[1:] == ['--portable']:
    changelog = Path('portable/CHANGELOG.md').read_text()
    match = re.search(r'^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$', changelog, re.M)
    if not match:
        raise SystemExit('Add a numeric release to portable/CHANGELOG.md first')
    path = Path('portable/versions.json')
    ledger = json.loads(path.read_text())
    component = ledger['node_apps']['photos']
    version = match[1]
    if tuple(map(int, version.split('.'))) < tuple(map(int, component['version'].split('.'))):
        raise SystemExit('Portable Photos version cannot decrease')
    if component['version'] != version:
        component['version'] = version
        component['sequence'] += 1
    if not 0 < component['sequence'] <= 2**31 - 1:
        raise SystemExit('Portable Photos sequence is out of range')
    path.write_text(json.dumps(ledger, indent=2) + '\n')
    print(f"Photos {version}, sequence {component['sequence']}; native version retained")
    raise SystemExit(0)
if sys.argv[1:]:
    raise SystemExit('Usage: update_version.sh [--portable]')

match=re.search(r'^## (\d+\.\d+\.\d+)\s*$',Path('CHANGELOG.md').read_text(),re.M)
if not match:
    raise SystemExit('Add a numeric release to CHANGELOG.md first')
Path('VERSION').write_text(match[1]+'\n')
PY
