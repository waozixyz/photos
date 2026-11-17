#!/bin/sh
# The caller holds the shared build queue, including all nested package work.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/portable"
ziran=${ZIRAN:-ziran}
flags=--locked
mingcute=$("$ziran" pkg path mingcute "$flags")
python3 "$root/scripts/prepare-node-assets.py" "$mingcute"
mkdir -p "$root/build/node-apps"
# shellcheck disable=SC2086
"$ziran" bundle --project --offline $flags --root . --define KRYON_HOSTED_UI \
    --asset-dir "assets=$root/build/package-assets/photos/assets" \
    --entry photos:Main -o "$root/build/node-apps/photos.zx" photos.zi
