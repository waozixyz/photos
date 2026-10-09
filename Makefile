.DEFAULT_GOAL := build
.PHONY: original-importer
original-importer:
	LDLIBS='-lglib-2.0' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry import_original:main --exe -o build/import-original src/import_original.zi
.PHONY: library-editor
library-editor:
	LDLIBS='-l:libsqlite3.so.0 -lglib-2.0' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry library_edit:main --exe -o build/library-editor src/library_edit.zi
.PHONY: motion-engine
motion-engine:
	LDLIBS='-lxml2 -l:libheif.so.1 -lglib-2.0' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry motion_photo:main --exe -o build/motion-photo src/motion_photo.zi
.PHONY: motion-window-test motion-portable-test
motion-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/motion_window.py
motion-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_MOTION_PORTABLE=1 $(PYTHON) tests/motion_window.py
.PHONY: collage-window-test collage-portable-test
collage-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/collage_window.py
collage-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_COLLAGE_PORTABLE=1 $(PYTHON) tests/collage_window.py
ZIRAN ?= ziran
PYTHON ?= build/venv/bin/python
PREFIX ?= $(HOME)/.local
PACKAGE_FLAGS := $(if $(wildcard ziran.local.toml),,--locked)
.PHONY: build bundle image-editor video-editor vault-auth library-query photo-decoder test sync-test window-test video-window-test video-portable-test playback-window-test playback-portable-test portable-test hosted-test install
.PHONY: semantic-engine semantic-models text-engine text-models qr-engine qr-window-test qr-portable-test
qr-engine:
	LDLIBS='-l:libzbar.so.0 -lgdk_pixbuf-2.0 -lgobject-2.0' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry detect_qr:main --exe -o build/detect-qr src/detect_qr.zi
qr-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/qr_window.py
qr-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_QR_PORTABLE=1 $(PYTHON) tests/qr_window.py
.PHONY: face-engine face-models people-window-test people-portable-test
face-engine:
	LDLIBS='-ldl -lm' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry face_infer:main --exe -o build/face-infer src/face_infer.zi
	LDLIBS='-lm -lgdk_pixbuf-2.0 -lgobject-2.0' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry face_pixels:main --exe -o build/face-pixels src/face_pixels.zi
	$(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry face_match:main --exe -o build/face-match src/face_match.zi
face-models:
	$(PYTHON) scripts/face-models.py
people-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/people_window.py
people-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_PEOPLE_PORTABLE=1 $(PYTHON) tests/people_window.py
.PHONY: discovery-window-test discovery-portable-test
discovery-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/discovery_window.py
discovery-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_DISCOVERY_PORTABLE=1 $(PYTHON) tests/discovery_window.py
.PHONY: slideshow-window-test slideshow-portable-test
slideshow-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/slideshow_window.py
slideshow-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_SLIDESHOW_PORTABLE=1 $(PYTHON) tests/slideshow_window.py
text-engine:
	LDLIBS='-l:libtesseract.so.5 -lgdk_pixbuf-2.0 -lgobject-2.0' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry detect_text:main --exe -o build/detect-text src/detect_text.zi
text-models:
	$(PYTHON) scripts/text-models.py
semantic-engine:
	LDLIBS='-ldl -lm' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry semantic_infer:main --exe -o build/semantic-infer src/semantic_infer.zi
semantic-models:
	$(PYTHON) scripts/semantic-models.py
photo-decoder:
	@set -e; ziran_source=$$($(ZIRAN) pkg path ziran $(PACKAGE_FLAGS)); \
	LDLIBS='-l:libheif.so.1 -l:libraw.so.23 -lgdk_pixbuf-2.0 -lgobject-2.0' "$$ziran_source/build/bin/ziran" build --root src --target=c --entry photo_decode:main --exe -o build/photo-decoder src/photo_decode.zi
video-editor:
	$(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry video_edit:main --exe -o build/video-editor src/video_edit.zi
image-editor:
	@set -e; ziran_source=$$($(ZIRAN) pkg path ziran $(PACKAGE_FLAGS)); \
	LDLIBS='-lgdk_pixbuf-2.0 -lgobject-2.0' "$$ziran_source/build/bin/ziran" build --root src --target=c --entry image_edit:main --exe -o build/image-editor src/image_edit.zi
vault-auth:
	@set -e; ziran_source=$$($(ZIRAN) pkg path ziran $(PACKAGE_FLAGS)); \
	LDLIBS='-l:libsodium.so.23' "$$ziran_source/build/bin/ziran" build --root src --target=c --entry vault_auth:main --exe -o build/vault-auth src/vault_auth.zi
library-query:
	LDLIBS='-l:libsqlite3.so.0' $(ZIRAN) build --project $(PACKAGE_FLAGS) --root src --target=c --entry library_query:main --exe -o build/library-query src/library_query.zi
build: original-importer library-editor image-editor video-editor vault-auth library-query photo-decoder semantic-engine text-engine face-engine qr-engine motion-engine
	$(ZIRAN) build $(PACKAGE_FLAGS)
	cp scripts/worker.py build/
	cp -r photos build/
bundle: original-importer library-editor image-editor video-editor vault-auth library-query photo-decoder semantic-engine text-engine face-engine qr-engine motion-engine
	$(ZIRAN) tool kryon build portable $(PACKAGE_FLAGS)
test: original-importer library-editor image-editor video-editor vault-auth library-query photo-decoder semantic-engine text-engine face-engine qr-engine motion-engine
	$(PYTHON) -m unittest discover -s tests -v
sync-test:
	$(PYTHON) tests/sync_node.py
window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_PYTHON=$(abspath $(PYTHON)) $(PYTHON) tests/window.py
portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/portable.py
hosted-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/hosted.py
video-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/video_window.py
video-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_VIDEO_PORTABLE=1 $(PYTHON) tests/video_window.py
playback-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/playback_window.py
playback-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_PLAYBACK_PORTABLE=1 $(PYTHON) tests/playback_window.py
install: build
	python3 scripts/install.py --prefix "$(PREFIX)"

.PHONY: crop-window-test crop-portable-test
crop-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/crop_window.py
crop-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_CROP_PORTABLE=1 $(PYTHON) tests/crop_window.py

.PHONY: viewer-window-test viewer-portable-test
viewer-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/viewer_window.py
viewer-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_VIEWER_PORTABLE=1 $(PYTHON) tests/viewer_window.py

.PHONY: map-window-test map-portable-test
map-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/map_window.py
map-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_MAP_PORTABLE=1 $(PYTHON) tests/map_window.py

.PHONY: codec-window-test codec-portable-test
codec-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/codec_window.py
codec-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_CODEC_PORTABLE=1 $(PYTHON) tests/codec_window.py

.PHONY: places-window-test places-portable-test
places-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/places_window.py
places-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_PLACES_PORTABLE=1 $(PYTHON) tests/places_window.py

.PHONY: semantic-window-test semantic-portable-test
semantic-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/semantic_window.py
semantic-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_SEMANTIC_PORTABLE=1 $(PYTHON) tests/semantic_window.py

.PHONY: text-window-test text-portable-test
text-window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/text_window.py
text-portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_TEXT_PORTABLE=1 $(PYTHON) tests/text_window.py
