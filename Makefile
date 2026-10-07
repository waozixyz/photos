.DEFAULT_GOAL := build
ZIRAN ?= ziran
PYTHON ?= build/venv/bin/python
PREFIX ?= $(HOME)/.local
.PHONY: build bundle test sync-test window-test portable-test hosted-test install
build:
	$(ZIRAN) build
	cp scripts/worker.py build/
	cp -r photos build/
bundle:
	$(ZIRAN) tool kryon build portable
test:
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
