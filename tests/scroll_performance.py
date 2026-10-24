"""Measure real standalone presentation during scrolling on an owned Xvfb."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from photos.service import Server
import photos.service as service_module


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def clean_environment():
    blocked = {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
               "SESSION_MANAGER", "LD_PRELOAD"}
    result = {key: value for key, value in os.environ.items()
              if key not in blocked and not key.startswith(("PHOTOS_", "KRYON_", "HARMONY_"))}
    result["YUE_DESKTOP_RECOVERY"] = "0"
    return result


def process_usage(pid):
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    cpu_seconds = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    status = (Path("/proc") / str(pid) / "status").read_text().splitlines()
    memory = {line.split(":", 1)[0]: int(line.split()[1])
              for line in status if line.startswith(("VmRSS:", "VmHWM:"))}
    return cpu_seconds, memory


def sample_bytes(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def measure(name, command, env, output, probe, calls, cpu=False, idle_seconds=0):
    trace = output / (name + ".frames")
    samples = output / (name + ".cpu")
    child = None
    first_call = len(calls)
    before = output / (name + ".before.png")
    after = output / (name + ".png")
    with (output / (name + ".log")).open("w") as log:
        try:
            child_env = dict(env, LD_PRELOAD=str(probe), PHOTOS_FRAME_PROBE=str(trace))
            if cpu:
                child_env["PHOTOS_CPU_PROBE"] = str(samples)
            child = subprocess.Popen(command, env=child_env, stdout=log, stderr=log)
            deadline = time.monotonic() + 20
            window = None
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(name + " stopped before creating its window")
                found = subprocess.run(["xdotool", "search", "--onlyvisible", "--pid", str(child.pid)],
                                       env=env, capture_output=True, text=True)
                if found.returncode == 0:
                    window = found.stdout.splitlines()[0]
                    break
                time.sleep(.1)
            if window is None:
                raise RuntimeError(name + " did not create a test-owned window")
            subprocess.run(["xdotool", "windowsize", window, "1104", "880"], env=env, check=True)
            subprocess.run(["xdotool", "windowmove", window, "0", "0"], env=env, check=True)
            subprocess.run(["xdotool", "windowfocus", window], env=env, check=True)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(name + " stopped while loading the fixture")
                subprocess.run(["import", "-window", window, str(before)], env=env, check=True)
                with Image.open(before) as pixels:
                    pixel = pixels.convert("RGB").getpixel((260, 300))
                if calls[first_call:] and max(abs(a-b) for a, b in zip(pixel, (0, 140, 100))) <= 4:
                    break
                time.sleep(.1)
            else:
                raise RuntimeError(name + " did not render its first synthetic thumbnail")
            subprocess.run(["xdotool", "mousemove", "--window", window, "600", "600"], env=env, check=True)
            idle = None
            if idle_seconds > 0:
                # Let thumbnail replies and pointer motion settle before the
                # stationary interval. Keep idle and scroll CPU samples apart.
                time.sleep(.5)
                idle_cpu_before, _ = process_usage(child.pid)
                idle_sample_start = sample_bytes(samples) if cpu else 0
                idle_start = time.monotonic()
                time.sleep(idle_seconds)
                idle_finish = time.monotonic()
                idle_sample_end = sample_bytes(samples) if cpu else 0
                idle_cpu_after, idle_memory = process_usage(child.pid)
                idle = dict(seconds=idle_finish-idle_start,
                            cpu_seconds=idle_cpu_after-idle_cpu_before,
                            cpu_percent=100*(idle_cpu_after-idle_cpu_before)/(idle_finish-idle_start),
                            rss_kib=idle_memory.get("VmRSS"),
                            cpu_sample_bytes=[idle_sample_start, idle_sample_end] if cpu else None)
            cpu_before, _ = process_usage(child.pid)
            sample_start = sample_bytes(samples) if cpu else 0
            start = time.monotonic()
            subprocess.run(["xdotool", "click", "--repeat", "110", "--delay", "35", "5"],
                           env=env, check=True)
            finish = time.monotonic()
            sample_end = sample_bytes(samples) if cpu else 0
            cpu_after, memory = process_usage(child.pid)
            time.sleep(.5)
            state_text = subprocess.check_output(["xprop", "-id", window, "_HARMONY_APP_STATE"],
                                                 env=env, text=True)
            state = json.loads(ast.literal_eval(state_text.split(" = ", 1)[1].strip())) if " = " in state_text else {}
            (output / (name + ".state.json")).write_text(json.dumps(state, indent=2) + "\n")
            subprocess.run(["import", "-window", window, str(after)],
                           env=env, check=True)
            with Image.open(before) as a, Image.open(after) as b:
                changed = a.crop((192, 192, 1092, 812)).tobytes() != b.crop((192, 192, 1092, 812)).tobytes()
            paged = any(request.get("offset", 0) >= 60 for request in calls[first_call:])
            if not changed or not paged or (state and (state.get("view") != "photos" or state.get("scroll", 0) <= 0)):
                raise RuntimeError(name + " did not actually scroll the synthetic gallery")
            values = [tuple(map(float, line.split())) for line in trace.read_text().splitlines()]
            presented = [stamp for stamp, _ in values if start <= stamp <= finish]
            # A retained window's first presentation can follow a long quiet
            # interval. Count that presentation, but time only intervals wholly
            # inside the scroll measurement rather than treating idle as a stall.
            durations = [duration for stamp, duration in values
                         if start <= stamp-duration/1000 <= stamp <= finish and duration > 0]
            if not durations:
                raise RuntimeError(name + " emitted no presentation samples")
            if idle is not None:
                idle_frames = [duration for stamp, duration in values
                               if idle_start <= stamp <= idle_finish and duration > 0]
                idle["frames"] = len(idle_frames)
                idle["fps"] = len(idle_frames)/idle["seconds"]
            return dict(mode=name, frames=len(presented), seconds=finish-start,
                        fps=len(presented)/(finish-start), median_ms=statistics.median(durations),
                        p95_ms=sorted(durations)[int(len(durations)*.95)], max_ms=max(durations),
                        scroll=state.get("scroll"), nodes=state.get("nodes"), paged=paged,
                        rendered_gallery_changed=changed, deployment=False,
                        cpu_seconds=cpu_after-cpu_before,
                        cpu_percent=100*(cpu_after-cpu_before)/(finish-start),
                        rss_kib=memory.get("VmRSS"), peak_rss_kib=memory.get("VmHWM"),
                        cpu_sampled=cpu,
                        cpu_sample_bytes=[sample_start, sample_end] if cpu else None,
                        idle=idle,
                        source_sha256={str(Path(path)): hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                       for path in command})
        finally:
            stop(child)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-bundle", type=Path)
    parser.add_argument("--bundle", type=Path, help="portable candidate to measure")
    parser.add_argument("--baseline-native", type=Path)
    parser.add_argument("--player", type=Path)
    parser.add_argument("--mode", choices=("native-prior-ui", "standalone-native", "portable-prior-ui", "portable-current-ui"))
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=0,
                        help="measure a stationary interval separately before scrolling")
    args = parser.parse_args()
    if not 0 <= args.idle_seconds <= 60:
        parser.error("--idle-seconds must be between 0 and 60")
    env = clean_environment()
    flags = [] if (ROOT / "ziran.local.toml").is_file() else ["--locked"]
    kryon = Path(subprocess.check_output(["ziran", "pkg", "path", "kryon", *flags],
                                       env=env, cwd=ROOT, text=True).strip())
    player = args.player.resolve() if args.player else kryon / "build/bin/zx"
    bundle = args.bundle.resolve() if args.bundle else ROOT / "build/photos-portable.zx"
    work = ROOT / "build/scroll-performance"
    work.mkdir(parents=True, exist_ok=True)
    output = ROOT / "backup/scroll-performance/current"
    output.mkdir(parents=True, exist_ok=True)
    probe = work / "frame_probe.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O2", str(ROOT / "tests/scroll_frame_probe.c"),
                    "-ldl", "-o", str(probe)], env=env, check=True)
    results = []
    display = None
    # Keep the private Unix endpoint below the host's path length limit.
    # Only generated fixture media/configuration live here, never source.
    with tempfile.TemporaryDirectory(prefix="photos-scroll-") as temporary:
        folder = Path(temporary)
        photos = []
        calls = []
        for index in range(240):
            path = folder / ("thumb-%s.png" % index)
            Image.new("RGB", (256, 200), (index % 200, 140, 100)).save(path)
            photos.append(dict(id="%064x" % index, name="Photo %s" % index, thumbnail=str(path),
                               date="2026-10-09", kind="photo", width=256, height=200))

        def response(request, config):
            calls.append(dict(request))
            start = request.get("offset", 0)
            return dict(total=len(photos), count=len(photos), photos=photos[start:start+60],
                        status="Synthetic benchmark")

        service_module.process = response
        with (output / "display.log").open("w") as log:
            try:
                display = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "1400x1000x24",
                                            "-nolisten", "tcp"], env=env, stdout=subprocess.PIPE, stderr=log)
                number = display.stdout.readline().decode().strip()
                display.stdout.close()
                if not number.isdecimal():
                    raise RuntimeError("Private Xvfb did not start")
                env["DISPLAY"] = ":" + number
                env["KRYON_SERVICE_ROOT"] = str(folder)
                config = folder / "config.json"
                config.write_text(json.dumps(dict(root=str(folder / "library"), sources=[])))
                env["PHOTOS_CONFIG"] = str(config)
                with Server(dict(root=str(folder / "library")), folder / "kryon/services/photos.sock") as server:
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    cases = []
                    if args.baseline_native:
                        cases.append(("native-prior-ui", [str(args.baseline_native.resolve())]))
                    cases.append(("standalone-native", [str(ROOT / "build/photos-desktop")]))
                    if args.baseline_bundle:
                        cases.append(("portable-prior-ui", [str(player), str(args.baseline_bundle.resolve())]))
                    cases.append(("portable-current-ui", [str(player), str(bundle)]))
                    for name, command in cases:
                        if args.mode and name != args.mode:
                            continue
                        result = measure(name, command, env, output, probe, calls,
                                         args.cpu, args.idle_seconds)
                        results.append(result)
                        print(json.dumps({key: value for key, value in result.items() if key != "source_sha256"}), flush=True)
                    server.shutdown()
            finally:
                stop(display)
                (output / "receipt.json").write_text(json.dumps(dict(results=results, private_display=True,
                    synthetic_media=True, deployment=False, owned_display_stopped=display is None or display.poll() is not None),
                    indent=2) + "\n")


if __name__ == "__main__":
    main()
