"""Verify Android gallery/JNI against synthetic media on a private emulator."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

from PIL import Image
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt as reference_encrypt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE = "xyz.waozi.photos"
OUTPUT = ROOT / "build/android/gallery"
RECEIPT = ROOT / "backup/mobile"


def port():
    with socket.socket() as peer:
        peer.bind(("127.0.0.1", 0))
        return peer.getsockname()[1]


def emulator_port():
    for number in range(5554, 5586, 2):
        peers = [socket.socket(), socket.socket()]
        try:
            for peer, value in zip(peers, (number, number + 1)):
                peer.bind(("127.0.0.1", value))
            return number
        except OSError:
            pass
        finally:
            for peer in peers:
                peer.close()
    raise RuntimeError("No private emulator ports available")


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def restore_staged_originals(device, database, identity, seeds, expected_count, multi_chunk):
    """Check every granted original using the released bytes and Daochi adapter."""
    from photos.library import packed
    from photos.sync_crypto import decrypt

    with sqlite3.connect(database) as queue:
        rows = queue.execute("SELECT volume,media_id,size,staged_id,staged_manifest,confirmed "
                             "FROM items WHERE eligible=1 ORDER BY media_id").fetchall()
    if len(rows) != expected_count:
        raise RuntimeError("Native Android staging did not retain all selected originals")
    chunks = 0
    multiple = 0
    key = bytes.fromhex(identity["identity"]["content_key"])
    for volume, media_id, original_size, photo_id, manifest_text, confirmed in rows:
        if confirmed != 0 or not re.fullmatch(r"[a-f0-9]{64}", photo_id):
            raise RuntimeError("Local staging must retain unconfirmed original identities")
        manifest = json.loads(manifest_text)
        if manifest["chunk_bytes"] != 8 * 1024 * 1024 or len(manifest["chunks"]) != (original_size + manifest["chunk_bytes"] - 1) // manifest["chunk_bytes"]:
            raise RuntimeError("Native Android staging changed the backup chunk format")
        uri = f"content://media/{volume}/file/{media_id}"
        original = device("exec-out", "content", "read", "--uri", uri, binary=True).stdout
        original_hash = hashlib.sha256(original).hexdigest()
        if len(original) != original_size or original_hash != photo_id or original_hash not in seeds.values():
            raise RuntimeError("Native Android staging original is not an unchanged synthetic fixture")
        restored = bytearray()
        try:
            for index, digest in enumerate(manifest["chunks"]):
                blob = device("exec-out", "run-as", PACKAGE, "cat", f"files/camera-backup/staged/{photo_id}/{index}.blob", binary=True).stdout
                if hashlib.sha256(blob).hexdigest() != digest:
                    raise RuntimeError("Native Android staging ciphertext identity mismatch")
                aad = packed(dict(account_id=identity["account"], app_id="photos", id=photo_id,
                                  part=f"chunk:{index}", schema="photos.media.v1")).encode()
                offset = index * manifest["chunk_bytes"]
                plain = original[offset:offset + manifest["chunk_bytes"]]
                nonce = hmac.new(key, aad + hashlib.sha256(plain).digest(), hashlib.sha256).digest()[:24]
                if blob != nonce + reference_encrypt(plain, aad, nonce, key):
                    raise RuntimeError("Native Android staging changed released encrypted backup bytes")
                restored.extend(decrypt(blob[24:], aad, blob[:24], key))
                chunks += 1
            if bytes(restored) != original:
                raise RuntimeError("Native Android staging could not restore the unchanged original on desktop")
        finally:
            restored[:] = b"\0" * len(restored)
        if len(manifest["chunks"]) > 1:
            multiple += 1
    if multi_chunk and multiple < 1:
        raise RuntimeError("Android staging never crossed an original chunk boundary")
    return dict(originals=len(rows), chunks=chunks, multi_chunk_originals=multiple,
                released_ciphertext_compatible=True, remote_confirmations=0)


def main():
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(key):
            raise RuntimeError(key + " must be scrubbed before this test")
    sdk = Path(os.environ.get("ANDROID_HOME", "/home/wao/Android/Sdk"))
    adb = sdk / "platform-tools/adb"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    RECEIPT.mkdir(parents=True, exist_ok=True)
    report = dict(passed=False, deployment=False, private_display=True, synthetic_media=True)
    lock_only = os.environ.get("PHOTOS_ANDROID_STAGE") == "lock"
    if os.environ.get("PHOTOS_ANDROID_STAGE", "all") not in {"all", "lock"}:
        raise RuntimeError("Unknown Android verification stage")
    with tempfile.TemporaryDirectory(prefix=".runtime-", dir=OUTPUT) as temporary:
        root = Path(temporary)
        server_port, console_port = port(), emulator_port()
        serial = "127.0.0.1:" + str(console_port + 1)
        env = dict(os.environ, ANDROID_USER_HOME=str(root / "android"),
                   ANDROID_EMULATOR_HOME=str(root / "android"), ANDROID_AVD_HOME=str(root / "avd"),
                   ANDROID_SDK_ROOT=str(sdk), ANDROID_HOME=str(sdk),
                   ANDROID_ADB_SERVER_PORT=str(server_port), ADB_SERVER_SOCKET="tcp:127.0.0.1:" + str(server_port),
                   YUE_DESKTOP_RECOVERY="0", QT_QPA_PLATFORM="offscreen")
        (root / "android").mkdir()
        (root / "avd").mkdir()
        for relative, name in (("debug/app-debug.apk", PACKAGE),
                               ("androidTest/debug/app-debug-androidTest.apk", PACKAGE + ".test")):
            apk = ROOT / "droid/app/build/outputs/apk" / relative
            inspector = sorted(sdk.glob("build-tools/*/aapt2"))[-1]
            badging = subprocess.check_output([str(inspector), "dump", "badging", str(apk)], text=True)
            if not re.search(r"^package: name='" + re.escape(name) + "'", badging, re.M):
                raise RuntimeError("Unexpected test APK identity")
        subprocess.run([str(sdk / "cmdline-tools/latest/bin/avdmanager"), "create", "avd",
                        "-n", "photos_gallery", "-p", str(root / "avd/photos_gallery.avd"),
                        "-k", "system-images;android-35;google_apis;x86_64"], env=env, input="no\n",
                       capture_output=True, text=True, check=True, timeout=90)
        with (root / "avd/photos_gallery.avd/config.ini").open("a") as configuration:
            configuration.write("\nhw.lcd.width=480\nhw.lcd.height=960\nhw.lcd.density=160\n")
        display = server = emulator = None
        with (OUTPUT / "emulator.log").open("w") as log:
            def command(*args, check=True, binary=False, data=None, timeout=30):
                if server is None or server.poll() is not None:
                    raise RuntimeError("Test-owned ADB server stopped")
                return subprocess.run([str(adb), "-P", str(server_port), *args], env=env,
                                      capture_output=True, text=not binary, input=data, check=check, timeout=timeout)

            def device(*args, **kwargs):
                if args and args[0] == "shell":
                    args = ("shell", shlex.join(args[1:]))
                result = command("-s", serial, *args, **kwargs)
                if kwargs.get("check", True) and not kwargs.get("binary", False):
                    if "Error while accessing provider" in result.stderr or "[ERROR]" in result.stdout:
                        raise RuntimeError("Synthetic provider command failed: " + result.stdout + result.stderr)
                return result

            def stage(name):
                print("Checking " + name + " gallery access", flush=True)
                result = device("shell", "am", "instrument", "-w", "-e", "stage", name,
                                PACKAGE + ".test/xyz.waozi.photos.PhotosGalleryProbe", timeout=120)
                (OUTPUT / (name + "-instrumentation.log")).write_text(result.stdout + result.stderr)
                if "INSTRUMENTATION_CODE: -1" not in result.stdout or "INSTRUMENTATION_RESULT: error=" in result.stdout:
                    picture = device("exec-out", "run-as", PACKAGE, "cat", "files/probe-failure.png", binary=True, check=False).stdout
                    if picture.startswith(b"\x89PNG"):
                        (OUTPUT / "probe-failure.png").write_bytes(picture)
                    raise RuntimeError(name + " checks failed: " + result.stdout)
                report[name] = json.loads(device("exec-out", "run-as", PACKAGE, "cat", "files/" + name + "-checks.json").stdout)
                names = ["locked"] if name == "lock" else [name] if name != "full" else ["gallery", "albums", "viewer", "viewer-zoom", "share", "videos", "refreshed"]
                for picture in names:
                    pixels = device("exec-out", "run-as", PACKAGE, "cat", "files/" + picture + ".png", binary=True).stdout
                    (OUTPUT / (picture + ".png")).write_bytes(pixels)

            def inserted_uri(collection):
                # The Android CLI performs insert without printing its URI.
                # This disposable provider has only this test's serial seeds.
                result = device("shell", "content", "query", "--uri", collection,
                                "--projection", "_id", "--sort", "_id DESC",
                                "--extra", "android\\:query-arg-match-pending:i:1")
                match = re.search(r"_id=(\d+)", result.stdout)
                if match is None:
                    raise RuntimeError("Synthetic media insertion failed: " + result.stdout + result.stderr)
                return collection + "/" + match[1]

            try:
                display = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "800x1000x24", "-nolisten", "tcp"],
                                           env=env, stdout=subprocess.PIPE, stderr=log)
                number = display.stdout.readline().decode().strip()
                display.stdout.close()
                if not number.isdecimal():
                    raise RuntimeError("Private display did not start")
                env["DISPLAY"] = ":" + number
                server = subprocess.Popen([str(adb), "-L", "tcp:" + str(server_port), "nodaemon", "server"], env=env, stdout=log, stderr=log)
                time.sleep(.5)
                emulator = subprocess.Popen([str(sdk / "emulator/emulator"), "-avd", "photos_gallery", "-port", str(console_port),
                                             "-no-window", "-no-snapshot", "-no-audio", "-no-boot-anim", "-no-metrics", "-gpu", "software",
                                             "-cores", "2", "-memory", "2048", "-accel", "on", "-feature", "-Vulkan"],
                                            env=env, stdout=log, stderr=log)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if emulator.poll() is not None:
                        raise RuntimeError("Disposable emulator stopped")
                    command("connect", serial, check=False)
                    if device("shell", "getprop", "sys.boot_completed", check=False).stdout.strip() == "1":
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Disposable emulator did not boot")
                if device("shell", "getprop", "ro.kernel.qemu").stdout.strip() != "1" or device("shell", "getprop", "ro.boot.qemu.avd_name").stdout.strip() != "photos_gallery":
                    raise RuntimeError("Refusing an emulator not owned by this test")
                device("shell", "wm", "dismiss-keyguard")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    ready = device("shell", "content", "query", "--uri", "content://media/external/file", "--projection", "_id", check=False)
                    if "No result found" in ready.stdout or "Row:" in ready.stdout:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Synthetic MediaStore provider did not become ready: " + ready.stdout + ready.stderr)
                print("Owned emulator ready; seeding synthetic provider media", flush=True)
                seeds = {}
                for i in range(1 if lock_only else 35):
                    image = root / "seed.png"
                    pixels = Image.new("RGB", (640, 480), (32 + i * 6, 120, 220 - i * 5))
                    if i == 0:
                        exif = Image.Exif()
                        exif[306] = "2020:01:01 00:00:00"
                        exif[34665] = {36867: "2020:01:01 00:00:00", 36881: "+00:00"}
                        pixels.save(image, "JPEG", exif=exif)
                        # Decoders stop at JPEG's end marker; backup must keep
                        # the complete provider original, including this tail.
                        with image.open("ab") as original:
                            original.write(bytes(range(256)) * 32769)
                    else:
                        pixels.save(image)
                    extension = "jpg" if i == 0 else "png"
                    mime = "image/jpeg" if i == 0 else "image/png"
                    device("shell", "content", "insert", "--uri", "content://media/external_primary/images/media",
                                 "--bind", "_display_name:s:写真-%02d-é." % i + extension, "--bind", "mime_type:s:" + mime,
                                 "--bind", "relative_path:s:Pictures/PhotosFixture", "--bind", "is_pending:i:1")
                    uri = inserted_uri("content://media/external_primary/images/media")
                    device("shell", "content", "write", "--uri", uri, binary=True, data=image.read_bytes())
                    device("shell", "content", "update", "--uri", uri, "--bind", "is_pending:i:0", "--bind", "datetaken:l:" + str(1577836800000 + i * 86400000))
                    seeds[uri] = hashlib.sha256(image.read_bytes()).hexdigest()
                # MediaStore derives capture time from EXIF during its scanner
                # pass. DATE_TAKEN is read-only, so wait for that publication
                # instead of treating a CLI update as the metadata fixture.
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    dates = device("shell", "content", "query", "--uri", next(iter(seeds)),
                                   "--projection", "datetaken").stdout
                    if "datetaken=1577836800000" in dates:
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError("Synthetic EXIF capture date was not indexed: " + dates)
                video = root / "seed.mp4"
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], env=env, check=True, timeout=30)
                device("shell", "content", "insert", "--uri", "content://media/external_primary/video/media",
                             "--bind", "_display_name:s:video.mp4", "--bind", "mime_type:s:video/mp4",
                             "--bind", "relative_path:s:Movies/PhotosFixtureVideo", "--bind", "is_pending:i:1")
                uri = inserted_uri("content://media/external_primary/video/media")
                device("shell", "content", "write", "--uri", uri, binary=True, data=video.read_bytes())
                device("shell", "content", "update", "--uri", uri, "--bind", "is_pending:i:0", "--bind", "datetaken:l:1500000000000")
                seeds[uri] = hashlib.sha256(video.read_bytes()).hexdigest()
                for relative in ("debug/app-debug.apk", "androidTest/debug/app-debug-androidTest.apk"):
                    device("install", str(ROOT / "droid/app/build/outputs/apk" / relative), timeout=90)
                stage("denied")
                for permission in ("READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO"):
                    device("shell", "pm", "grant", PACKAGE, "android.permission." + permission)
                if not lock_only:
                    stage("full")
                device("shell", "locksettings", "set-pin", "2468")
                stage("lock")
                stage("backup")
                # The emulator's recovery identity is synthetic. Restore the
                # native phone ciphertext through the existing desktop format.
                from photos import recovery
                recovery_file = (ROOT / "build/android/fixtures/recovery.bin").read_bytes()
                identity = json.loads(recovery.crypt("Synthetic Android recovery password",
                    recovery_file[len(recovery.MAGIC):], restore=True))
                database = root / "queue.sqlite"
                database.write_bytes(device("exec-out", "run-as", PACKAGE, "cat", "files/camera-backup/queue.sqlite", binary=True).stdout)
                report["android_staged_restore"] = restore_staged_originals(device, database, identity, seeds,
                    expected_count=2 if lock_only else 35, multi_chunk=True)
                report["android_encrypted_staging_desktop_restore"] = True
                for permission in ("READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO"):
                    device("shell", "pm", "revoke", PACKAGE, "android.permission." + permission)
                device("shell", "pm", "revoke", PACKAGE, "android.permission.READ_MEDIA_VISUAL_USER_SELECTED", check=False)
                stage("denied")
                device("shell", "pm", "grant", PACKAGE, "android.permission.READ_MEDIA_VISUAL_USER_SELECTED")
                stage("partial")
                stage("selected")
                remaining = 0
                for uri, digest in seeds.items():
                    exists = device("shell", "content", "query", "--uri", uri, "--projection", "_id").stdout
                    if "Row:" not in exists:
                        continue
                    pixels = device("exec-out", "content", "read", "--uri", uri, binary=True).stdout
                    if hashlib.sha256(pixels).hexdigest() != digest:
                        raise RuntimeError("Synthetic original changed during gallery use")
                    remaining += 1
                if remaining != (2 if lock_only else 35):
                    raise RuntimeError("Unexpected synthetic original count")
                report["unchanged_originals"] = remaining
                trace = device("logcat", "-d", "-v", "threadtime").stdout
                (OUTPUT / "logcat.txt").write_text(trace)
                if "First native frame ready" not in trace or "Fatal signal" in trace or "FATAL EXCEPTION" in trace:
                    raise RuntimeError("Native frame missing or Android crashed")
                report["native_frames"] = True
                device("pull", "/system/fonts/NotoSansCJK-Regular.ttc", str(OUTPUT / "system-cjk.ttc"), check=False)
                report["passed"] = True
            except Exception as error:
                report["error"] = str(error)
                if server is not None and server.poll() is None and emulator is not None and emulator.poll() is None:
                    try:
                        (OUTPUT / "logcat.txt").write_text(device("logcat", "-d", check=False).stdout)
                        (OUTPUT / "failure.png").write_bytes(device("exec-out", "screencap", "-p", binary=True, check=False).stdout)
                    except Exception:
                        pass
                raise
            finally:
                stop(emulator)
                stop(server)
                stop(display)
                report["owned_processes_stopped"] = all(p is None or p.poll() is not None for p in (emulator, server, display))
                (RECEIPT / "emulator.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Android native gallery, folders, viewer, permissions, privacy and original preservation passed")


if __name__ == "__main__":
    main()
