.DEFAULT_GOAL := build
ZIRAN ?= ziran
PYTHON ?= build/venv/bin/python
PREFIX ?= $(HOME)/.local
PACKAGE_FLAGS := $(if $(wildcard ziran.local.toml),,--locked)
.PHONY: build bundle image-editor test sync-test window-test portable-test hosted-test install
image-editor:
	@set -e; ziran_source=$$($(ZIRAN) pkg path ziran $(PACKAGE_FLAGS)); \
	LDLIBS='-lgdk_pixbuf-2.0 -lgobject-2.0' "$$ziran_source/build/bin/ziran" build --root src --target=c --entry image_edit:main --exe -o build/image-editor src/image_edit.zi
build: image-editor
	$(ZIRAN) build $(PACKAGE_FLAGS)
	cp scripts/worker.py build/
	cp -r photos build/
bundle: image-editor
	$(ZIRAN) tool kryon build portable $(PACKAGE_FLAGS)
test: image-editor
	$(PYTHON) -m unittest discover -s tests -v
sync-test:
	$(PYTHON) tests/sync_node.py
window-test: build
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 PHOTOS_PYTHON=$(abspath $(PYTHON)) $(PYTHON) tests/window.py
portable-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/portable.py
hosted-test: bundle
	env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS -u SESSION_MANAGER YUE_DESKTOP_RECOVERY=0 xvfb-run -a -s '-screen 0 1400x1000x24' env PHOTOS_PRIVATE_XVFB=1 $(PYTHON) tests/hosted.py
install: build
	python3 scripts/install.py --prefix "$(PREFIX)"
