#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 - <<'PY'
import re
from pathlib import Path
match=re.search(r'^## (\d+\.\d+\.\d+)\s*$',Path('CHANGELOG.md').read_text(),re.M)
if not match:
    raise SystemExit('Add a numeric release to CHANGELOG.md first')
Path('VERSION').write_text(match[1]+'\n')
PY
