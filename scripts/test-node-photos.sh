#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
unset DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS DBUS_SYSTEM_BUS_ADDRESS WAYLAND_SOCKET SESSION_MANAGER XDG_RUNTIME_DIR
export YUE_DESKTOP_RECOVERY=0
export HARMONY_DESKTOP_RECOVERY=0
cd "$root/portable"
ziran=${ZIRAN:-ziran}
flags=--locked
ziran_root=${ZIRAN_ROOT:-$("$ziran" pkg path ziran "$flags")}
apps=$("$ziran" pkg path apps "$flags")
output="$root/build/tests/photos-host"
mkdir -p "$output"
# Render into Photos' private memory fixture; no window or display is opened.
binds="
--bind kryon/font_metrics:MeasureGlyphWidth=component_raster_host:ComponentMeasureGlyphWidth
--bind kryon/font_metrics:MeasureGlyphLineHeight=component_raster_host:ComponentMeasureGlyphLineHeight
--bind kryon/image_raster:ImageWidth=component_raster_host:ComponentImageWidth
--bind kryon/image_raster:ImageHeight=component_raster_host:ComponentImageHeight
--bind kryon/raster_text:RasterText=component_raster_host:ComponentRasterText
--bind kryon/raster_text:RasterTextClipped=component_raster_host:ComponentRasterTextClipped
--bind kryon/raster_text:RasterTextRotated=component_raster_host:ComponentRasterTextRotated
--bind kryon/raster_shape:RasterRoundedRectangle=component_raster_host:ComponentRasterRoundedRectangle
--bind kryon/raster_shape:RasterRoundedRectangleOutline=component_raster_host:ComponentRasterRoundedRectangleOutline
--bind kryon/raster:RasterLine=component_raster_host:ComponentRasterLine
--bind kryon/raster_triangles:RasterTriangles=component_raster_host:ComponentRasterTriangles
--bind kryon/system_clipboard:SystemClipboardOpenHost=component_raster_host:ComponentSystemClipboardOpenHost
--bind kryon/system_clipboard:SystemClipboardByteHost=component_raster_host:ComponentSystemClipboardByteHost
--bind kryon/system_clipboard:SystemClipboardWriteHost=component_raster_host:ComponentSystemClipboardWriteHost
--bind kryon/paint_queue:RasterImage=component_raster_host:ComponentRasterImage
--bind kryon/paint_queue:RasterImageMesh=component_raster_host:ComponentRasterImageMesh"
# shellcheck disable=SC2086
LDLIBS="$ziran_root/build/libziran.a -lm -lpthread" \
    "$ziran" build --project --offline $flags --root tests --module-path tests \
    $binds --target=c --exe \
    --entry photos_host_test:main -o "$output" tests/photos_host_test.zi
cd "$root"
"$output/photos_host_test"
python3 tests/node_photos_package.py "$apps"
