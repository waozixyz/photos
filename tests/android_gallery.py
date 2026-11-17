"""Verify Android gallery/JNI against synthetic media on a private emulator."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import shutil
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
    # Standard emulator ports are automatically scanned by every ADB server.
    # Explicit ports outside that range keep this guest private to our server.
    for number in range(20000, 20128, 2):
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
    """Check every granted original using the released bytes and Mesh adapter."""
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


def start_node(folder, seed, binary):
    """A disposable Mesh node with the fixture's synthetic node identity."""
    folder.mkdir(mode=0o700)
    node_port = port()
    origin = f"http://127.0.0.1:{node_port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MESH_", "DAOCHI_")) and k not in
           {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER"}}
    env.update(MESH_ADDR=f"127.0.0.1:{node_port}", MESH_BASE_URL=origin, MESH_DB=str(folder / "node.sqlite"),
               MESH_ADMIN_TOKEN="fixture-admin", MESH_KNOWN_NODES="", MESH_LAN_DISCOVERY="0",
               MESH_NODE_SYNC_INTERVAL_SECONDS="0", MESH_TOKEN_SECRET_HEX="42" * 32,
               MESH_NODE_IDENTITY_PRIVATE_KEY_HEX=seed.hex())
    log = (folder / "node.log").open("w")
    process = subprocess.Popen([str(Path(binary).resolve())], env=env, stdout=log, stderr=log)
    log.close()
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(200):
        try:
            node = json.load(opener.open(origin + "/api/v1/node", timeout=1))
            break
        except Exception:
            if process.poll() is not None:
                raise RuntimeError("Disposable Mesh node stopped")
            time.sleep(.05)
    else:
        stop(process)
        raise RuntimeError("Disposable Mesh node did not start")
    registration = json.dumps(dict(app_id="photos", display_name="Photos", status="active",
                                   collections=[dict(collection_prefix="private.photos.v1.records.*", visibility="private")]))
    opener.open(urllib.request.Request(origin + "/api/v1/apps", method="POST", data=registration.encode(),
                headers={"X-Daochi-Admin": "fixture-admin", "Content-Type": "application/json"})).close()
    return process, origin, node_port, node


def restore_uploaded_originals(origin, node_id, identity, seeds, folder, expected):
    """A separate desktop library restores the phone's uploads from the node."""
    from photos.library import Library, packed
    from photos.sync import Client
    from owner_fixture import ACCOUNT

    library = Library(folder)
    private = library.root / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    (private / "identity.json").write_bytes(packed(dict(
        public=identity["identity"]["public"], private=identity["identity"]["private"],
        content_key=identity["identity"]["content_key"])).encode())
    os.chmod(private / "identity.json", 0o600)
    client = Client(library, dict(node=origin, node_id=node_id), account=ACCOUNT)
    if client.sync() != 0:
        raise RuntimeError("Desktop restore left queued records")
    rows = library.db.execute("SELECT id,name,kind,manifest FROM photos").fetchall()
    known = set(seeds.values())
    if len(rows) != expected or any(row["id"] not in known for row in rows):
        raise RuntimeError("Desktop restore does not match the phone's uploaded originals")
    previews = videos = 0
    for row in rows:
        restored = Path(client.materialize(row["id"])).read_bytes()
        if hashlib.sha256(restored).hexdigest() != row["id"]:
            raise RuntimeError("Uploaded original did not restore byte-for-byte")
        if json.loads(row["manifest"]).get("preview") and Path(client.materialize(row["id"], preview_only=True)).stat().st_size:
            previews += 1
        videos += row["kind"] == "video"
    if previews < expected - 1 or videos != 1:
        raise RuntimeError("Uploaded previews or video kind are missing")
    return dict(originals=len(rows), restored_exact=True, previews=previews, videos=videos)


def main():
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(key):
            raise RuntimeError(key + " must be scrubbed before this test")
    sdk = Path(os.environ.get("ANDROID_HOME", "/home/wao/Android/Sdk"))
    adb = sdk / "platform-tools/adb"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    RECEIPT.mkdir(parents=True, exist_ok=True)
    for name in ("probe-logcat.txt", "logcat.txt", "transport-diagnostic.json"):
        (OUTPUT / name).write_text("")
    report = dict(passed=False, deployment=False, private_display=True, synthetic_media=True)
    lock_only = os.environ.get("PHOTOS_ANDROID_STAGE") == "lock"
    if os.environ.get("PHOTOS_ANDROID_STAGE", "all") not in {"all", "lock"}:
        raise RuntimeError("Unknown Android verification stage")
    with tempfile.TemporaryDirectory(prefix=".runtime-", dir=OUTPUT) as temporary:
        root = Path(temporary)
        server_port, console_port = port(), emulator_port()
        serial = "emulator-" + str(console_port)
        env = dict(os.environ, ANDROID_USER_HOME=str(root / "android"),
                   ANDROID_EMULATOR_HOME=str(root / "android"), ANDROID_AVD_HOME=str(root / "avd"),
                   ANDROID_SDK_ROOT=str(sdk), ANDROID_HOME=str(sdk),
                   ANDROID_ADB_SERVER_PORT=str(server_port), ADB_SERVER_SOCKET="tcp:127.0.0.1:" + str(server_port),
                   ADB_LOCAL_TRANSPORT_MAX_PORT="0", ADB_MDNS_AUTO_CONNECT="",
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
        display = server = emulator = diagnostic = None
        with (OUTPUT / "emulator.log").open("w") as log:
            def command(*args, check=True, binary=False, data=None, timeout=30):
                if server is None or server.poll() is not None:
                    raise RuntimeError("Test-owned ADB server stopped")
                return subprocess.run([str(adb), "-P", str(server_port), *args], env=env,
                                      capture_output=True, text=not binary, input=data, check=check, timeout=timeout)

            def device(*args, **kwargs):
                if args and args[0] == "shell":
                    # Fixed commands do not use terminal input. Only provider
                    # writes receive the synthetic bytes supplied by the test.
                    options = ("-T",) if kwargs.get("data") is not None else ("-n", "-T")
                    args = ("shell", *options, shlex.join(args[1:]))
                result = command("-s", serial, *args, **kwargs)
                if kwargs.get("check", True) and not kwargs.get("binary", False):
                    if "Error while accessing provider" in result.stderr or "[ERROR]" in result.stdout:
                        raise RuntimeError("Synthetic provider command failed: " + result.stdout + result.stderr)
                return result

            def stage(name):
                print("Checking " + name + " gallery access", flush=True)
                try:
                    result = device("shell", "am", "instrument", "-w", "-e", "stage", name,
                                    PACKAGE + ".test/xyz.waozi.photos.PhotosGalleryProbe", timeout=120)
                except subprocess.TimeoutExpired as error:
                    partial = error.stdout or b""
                    if isinstance(partial, bytes):
                        partial = partial.decode(errors="replace")
                    (OUTPUT / (name + "-instrumentation.log")).write_text(partial + "\n" + str(error) + "\n")
                    raise
                (OUTPUT / (name + "-instrumentation.log")).write_text(result.stdout + result.stderr)
                if "INSTRUMENTATION_CODE: -1" not in result.stdout or "INSTRUMENTATION_RESULT: error=" in result.stdout:
                    # A failed secure-window check can leave screenshot/ADB
                    # diagnostics unavailable. Preserve the primary failure.
                    try:
                        picture = device("exec-out", "run-as", PACKAGE, "cat", "files/probe-failure.png",
                                         binary=True, check=False, timeout=5).stdout
                        if picture.startswith(b"\x89PNG"):
                            (OUTPUT / "probe-failure.png").write_bytes(picture)
                    except (subprocess.SubprocessError, OSError) as diagnostic:
                        (OUTPUT / (name + "-diagnostic.log")).write_text(str(diagnostic) + "\n")
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
                emulator = subprocess.Popen([str(sdk / "emulator/emulator"), "-avd", "photos_gallery",
                                             "-ports", str(console_port) + "," + str(console_port + 1),
                                             "-show-kernel",
                                             "-no-window", "-no-snapshot", "-no-audio", "-no-boot-anim", "-no-metrics", "-gpu", "software",
                                             "-cores", "4", "-memory", "4096", "-accel", "on", "-feature", "-Vulkan"],
                                            env=env, stdout=log, stderr=log)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if emulator.poll() is not None:
                        raise RuntimeError("Disposable emulator stopped")
                    try:
                        completed = device("shell", "getprop", "sys.boot_completed", check=False,
                                           timeout=min(10, max(1, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        print("Owned emulator transport is still initializing", flush=True)
                        continue
                    if completed.stdout.strip() == "1":
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Disposable emulator did not boot")
                if device("shell", "getprop", "ro.kernel.qemu").stdout.strip() != "1" or device("shell", "getprop", "ro.boot.qemu.avd_name").stdout.strip() != "photos_gallery":
                    raise RuntimeError("Refusing an emulator not owned by this test")
                diagnostic_log = (OUTPUT / "probe-logcat.txt").open("w")
                diagnostic = subprocess.Popen([str(adb), "-P", str(server_port), "-s", serial,
                    "logcat", "-v", "threadtime", "PhotosProbe:I", "PhotosLock:D",
                    "PhotosActivity:I", "AndroidRuntime:E", "MediaProvider:I",
                    "ModernMediaScanner:W", "ActivityManager:E", "*:S"], env=env,
                    stdout=diagnostic_log, stderr=diagnostic_log)
                diagnostic_log.close()
                device("shell", "wm", "dismiss-keyguard")
                # A fresh Android image reports boot completion before the
                # provider finishes its initial database recovery and scan.
                # A bounded readiness query may time out during that setup;
                # retry within the same allowance as the initial cold boot.
                deadline = time.monotonic() + 180
                ready_text = "MediaStore is still initializing"
                while time.monotonic() < deadline:
                    try:
                        ready = device("shell", "content", "query", "--uri", "content://media/external/file",
                                       "--projection", "_id", check=False,
                                       timeout=min(30, max(1, deadline - time.monotonic())))
                        ready_text = ready.stdout + ready.stderr
                    except subprocess.TimeoutExpired as error:
                        ready_text = "MediaStore readiness query timed out during cold-boot setup"
                        print(ready_text, flush=True)
                        diagnostic_path = OUTPUT / "transport-diagnostic.json"
                        if not diagnostic_path.read_text():
                            # Compare the noninteractive raw command service
                            # while this server and guest are still supervised.
                            transport = dict(shell_stdout=(error.stdout or b"").decode(errors="replace"),
                                             shell_stderr=(error.stderr or b"").decode(errors="replace"))
                            try:
                                raw = device("exec-out", "getprop", "sys.boot_completed", timeout=5)
                                transport.update(exec_status=raw.returncode, exec_stdout=raw.stdout)
                            except (subprocess.SubprocessError, OSError) as failure:
                                transport["exec_error"] = str(failure)
                            diagnostic_path.write_text(json.dumps(transport, indent=2) + "\n")
                        continue
                    if "No result found" in ready.stdout or "Row:" in ready.stdout:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Synthetic MediaStore provider did not become ready: " + ready_text)
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
                    # Publication scans the real file. Capture time comes from
                    # its EXIF; DATE_TAKEN is a read-only provider column.
                    device("shell", "content", "update", "--uri", uri, "--bind", "is_pending:i:0")
                    seeds[uri] = hashlib.sha256(image.read_bytes()).hexdigest()
                    print("Published synthetic image " + str(i + 1), flush=True)
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
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "1",
                                "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1",
                                "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(video)],
                               env=env, check=True, timeout=30)
                device("shell", "content", "insert", "--uri", "content://media/external_primary/video/media",
                             "--bind", "_display_name:s:video.mp4", "--bind", "mime_type:s:video/mp4",
                             "--bind", "relative_path:s:Movies/PhotosFixtureVideo", "--bind", "is_pending:i:1")
                uri = inserted_uri("content://media/external_primary/video/media")
                device("shell", "content", "write", "--uri", uri, binary=True, data=video.read_bytes())
                device("shell", "content", "update", "--uri", uri, "--bind", "is_pending:i:0")
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
                if not lock_only:
                    # Forward only this guest's loopback port to a disposable
                    # node pinned by the recovered identity; nothing else.
                    binary = os.environ.get("PHOTOS_MESH") or shutil.which("mesh")
                    if not binary or not Path(binary).is_file():
                        raise RuntimeError("Set PHOTOS_MESH for the pinned upload check")
                    node_process, origin, node_port, node = start_node(root / "node",
                        (ROOT / "build/android/node-seed.bin").read_bytes(), binary)
                    try:
                        if node["node_id"] != identity["node_id"]:
                            raise RuntimeError("Disposable node does not match the recovered pin")
                        device("reverse", "tcp:18080", "tcp:" + str(node_port))
                        for permission in ("READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO"):
                            device("shell", "pm", "grant", PACKAGE, "android.permission." + permission)
                        stage("upload")
                        # A photo taken after the app's own upload is handled
                        # only by Android's periodic job, with Photos idle.
                        image = root / "camera.png"
                        Image.new("RGB", (640, 480), (250, 40, 90)).save(image)
                        device("shell", "content", "insert", "--uri", "content://media/external_primary/images/media",
                               "--bind", "_display_name:s:camera-new.png", "--bind", "mime_type:s:image/png",
                               "--bind", "relative_path:s:Pictures/PhotosFixture", "--bind", "is_pending:i:1")
                        uri = inserted_uri("content://media/external_primary/images/media")
                        device("shell", "content", "write", "--uri", uri, binary=True, data=image.read_bytes())
                        device("shell", "content", "update", "--uri", uri, "--bind", "is_pending:i:0")
                        seeds[uri] = hashlib.sha256(image.read_bytes()).hexdigest()
                        stage("background")
                        device("reverse", "--remove", "tcp:18080", check=False)
                        report["android_pinned_upload"] = restore_uploaded_originals(
                            origin, node["node_id"], identity, seeds, root / "desktop", 36)
                    finally:
                        device("reverse", "--remove-all", check=False)
                        stop(node_process)
                remaining = 0
                for uri, digest in seeds.items():
                    exists = device("shell", "content", "query", "--uri", uri, "--projection", "_id").stdout
                    if "Row:" not in exists:
                        continue
                    pixels = device("exec-out", "content", "read", "--uri", uri, binary=True).stdout
                    if hashlib.sha256(pixels).hexdigest() != digest:
                        raise RuntimeError("Synthetic original changed during gallery use")
                    remaining += 1
                if remaining != (2 if lock_only else 36):
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
                stop(diagnostic)
                stop(emulator)
                stop(server)
                stop(display)
                report["owned_processes_stopped"] = all(p is None or p.poll() is not None for p in (emulator, server, display, diagnostic))
                (RECEIPT / "emulator.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Android native gallery, folders, viewer, permissions, privacy and original preservation passed")


if __name__ == "__main__":
    main()
