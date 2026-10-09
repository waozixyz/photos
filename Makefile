.DEFAULT_GOAL := build
ZIRAN ?= ziran
PYTHON ?= build/venv/bin/python
PREFIX ?= $(HOME)/.local
PACKAGE_FLAGS := $(if $(wildcard ziran.local.toml),,--locked)
.PHONY: build bundle image-editor video-editor vault-auth library-query test sync-test window-test video-window-test video-portable-test playback-window-test playback-portable-test portable-test hosted-test install
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
build: image-editor video-editor vault-auth library-query
	$(ZIRAN) build $(PACKAGE_FLAGS)
	cp scripts/worker.py build/
	cp -r photos build/
bundle: image-editor video-editor vault-auth library-query
	$(ZIRAN) tool kryon build portable $(PACKAGE_FLAGS)
test: image-editor video-editor vault-auth library-query
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
