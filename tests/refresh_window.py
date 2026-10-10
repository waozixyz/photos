"""Elapsed-time refresh at idle cadence, on synthetic private windows only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from photos.library import Library
from photos.service import Server
import photos.service as service_module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--portable", action="store_true")
    args = parser.parse_args()
    if os.environ.get("PHOTOS_PRIVATE_XVFB") != "1":
        raise RuntimeError("Run this check on an owned private Xvfb display")
    output = ROOT / "build/refresh-test"
    output.mkdir(parents=True, exist_ok=True)
    probe = output / "frame_probe.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O2", str(ROOT / "tests/scroll_frame_probe.c"),
                    "-ldl", "-o", str(probe)], check=True)
    frames = output / "frames.txt"
    polls = output / "polls.txt"
    if frames.exists():
        frames.unlink()
    if polls.exists():
        polls.unlink()
    with tempfile.TemporaryDirectory(prefix="photos-refresh-") as temporary:
        folder = Path(temporary)
        source = folder / "source"
        source.mkdir()
        original = source / "Synthetic.jpg"
        Image.new("RGB", (700, 500), "#438e7c").save(original)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        queries = []
        process_request = service_module.process

        def record(request, config):
            result = process_request(request, config)
            if request.get("action") == "query":
                queries.append(time.monotonic())
            return result

        service_module.process = record
        library = Library(folder / "library")
        try:
            library.import_folders([source])
            config = dict(root=str(library.root), sources=[])
        finally:
            library.db.close()
        env = dict(os.environ, KRYON_SERVICE_ROOT=str(folder),
                   LD_PRELOAD=str(probe), PHOTOS_FRAME_PROBE=str(frames), PHOTOS_POLL_PROBE=str(polls))
        config_file = folder / "config.json"
        config_file.write_text(json.dumps(config))
        env["PHOTOS_CONFIG"] = str(config_file)
        if args.portable:
            player = os.environ.get("PHOTOS_PLAYER")
            if not player:
                flags = [] if (ROOT / "ziran.local.toml").exists() else ["--locked"]
                kryon = subprocess.check_output(["ziran", "pkg", "path", "kryon", *flags],
                                               cwd=ROOT, text=True).strip()
                player = str(Path(kryon) / "build/bin/zib")
            command = [player, os.environ.get("PHOTOS_BUNDLE", str(ROOT / "build/photos-portable.zib"))]
        else:
            command = [os.environ.get("PHOTOS_BINARY", str(ROOT / "build/photos-desktop"))]
        with Server(config, folder / "kryon/services/photos.sock") as service:
            threading.Thread(target=service.serve_forever, daemon=True).start()
            with (output / "app.log").open("w") as log:
                child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 15
                    window = None
                    while time.monotonic() < deadline:
                        if child.poll() is not None:
                            raise AssertionError("Photos stopped before its synthetic window was ready")
                        found = subprocess.run(["xdotool", "search", "--onlyvisible", "--pid", str(child.pid)],
                                               capture_output=True, text=True)
                        if found.returncode == 0 and queries:
                            window = found.stdout.splitlines()[0]
                            break
                        time.sleep(.05)
                    assert window, "The test-owned Photos window did not load"
                    subprocess.run(["xdotool", "windowfocus", window], check=True)
                    subprocess.run(["xdotool", "mousemove", "--window", window, "600", "600"], check=True)
                    while len(queries) < 2 and time.monotonic() - queries[0] < 7:
                        assert child.poll() is None, "Photos stopped during idle refresh"
                        time.sleep(.05)
                    assert len(queries) >= 2, "Five-second library refresh depended on rendered-frame count"
                    elapsed = queries[1] - queries[0]
                    assert 4.5 <= elapsed <= 6.5, ("Unexpected elapsed-time refresh", elapsed)
                    samples = [float(line.split()[0]) for line in frames.read_text().splitlines()]
                    start, end = queries[0] + 1.5, queries[1] - .25
                    presented = sum(start <= stamp < end for stamp in samples) / (end - start)
                    polled = sum(start <= float(stamp) < end for stamp in polls.read_text().splitlines()) / (end - start)
                    assert 10 <= polled <= 20, ("The check did not exercise idle timer polling", polled)
                    assert presented <= polled + 1, ("Idle presentation exceeded timer polling", presented, polled)
                    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest, "Synthetic original changed"
                    print(json.dumps(dict(portable=args.portable, refresh_seconds=elapsed,
                                          idle_poll_fps=polled, idle_present_fps=presented,
                                          unchanged_original=True, private_display=True, deployment=False)))
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)
        service_module.process = process_request


if __name__ == "__main__":
    main()
