#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
unset DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS SESSION_MANAGER
ziran=${ZIRAN:-ziran}
work=build/gallery-text-test
mkdir -p "$work"
flags=${PACKAGE_FLAGS-}
if test -z "${PACKAGE_FLAGS+x}" && test ! -f ziran.local.toml; then flags=--locked; fi
"$ziran" ir --project $flags --root tests -o "$work/ir" tests/gallery_text_probe.zi
for form in source saved; do
    input=tests/gallery_text_probe.zi
    root=tests
    if test "$form" = saved; then input=$work/ir/gallery_text_probe.zir; root=$work/ir; fi
    "$ziran" bundle --project $flags --root "$root" --bind-host gallery_text_probe --entry gallery_text_probe:main \
        -o "$work/$form.zx" "$input"
    test "$("$ziran" run "$work/$form.zx")" = 0
    "$ziran" build --project $flags --root "$root" --bind-host gallery_text_probe --target=c --entry gallery_text_probe:main \
        --exe -o "$work/$form" "$input"
    "$work/$form/gallery_text_probe"
done
printf '%s\n' 'Gallery text: Unicode, NUL, bounded rejection, original fallback and owned buffers passed native and portable source/saved IR'
