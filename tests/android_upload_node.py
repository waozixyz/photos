"""Android camera uploads round-trip through an isolated Daochi node.

The phone's native request builder, staged chunks and signatures are driven
here exactly as the Android worker sends them. A fresh desktop library must
then restore the original bytes and preview, while library metadata for an
original the desktop already owns is never replaced by the device baseline.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
from photos.library import Library, packed, digest_file
from photos.sync import Client
from photos import sync_account, sync_http, sync_transaction

UPLOAD = ROOT / "build/backup-upload-test/backup_upload_probe"
STAGING = ROOT / "build/backup-staging-test/backup_staging_probe"


def native(mode, identity, *fields, tail=b""):
    data = bytes([mode]) + identity + b"".join(struct.pack("!I", len(f)) + f for f in fields) + tail
    result = subprocess.run(["prlimit", "--core=0", "--", str(UPLOAD)], input=data, capture_output=True, timeout=60)
    assert not result.stderr, result.stderr
    return result.returncode, result.stdout


def stage(identity, source, root):
    account, key = identity[32:64], identity[-32:]
    result = subprocess.run([str(STAGING), str(source), str(root), str(source.stat().st_size)],
                            input=account + key, capture_output=True, timeout=60)
    assert result.returncode == 0 and not result.stderr, result.stderr
    return result.stdout


class Phone:
    """The Android worker's request sequence, over the desktop loopback pipe."""

    def __init__(self, origin, identity):
        self.origin, self.identity = origin, identity
        self.public, self.private = identity[64:1376], identity[1376:3936]
        self.account = identity[32:64].hex()
        self.seed = os.urandom(32)
        self.client = "photos-android-" + os.urandom(12).hex()
        self.token = None
        self.since = 0

    def node(self):
        code, reply = native(4, self.identity, sync_http.request(self.origin, "/api/v1/node", limit=1048576))
        assert code == 0 and reply == b"\0", (code, reply)

    def login(self):
        challenge = sync_http.request(self.origin, "/api/v1/sync/challenge?user_id=" + self.account)
        body, signature = sync_account.login(self.public, self.private, self.client, challenge)
        reply = sync_http.request(self.origin, "/api/v1/sync/login", "POST", body,
                                  {"Content-Type": "application/json", "X-Daochi-User": self.account,
                                   "X-Daochi-Signature": signature})
        self.token = json.loads(reply)["auth_token"]
        body = sync_account.registration(self.public, self.private, self.seed, self.client, int(time.time()))
        sync_http.request(self.origin, "/api/v1/account/devices", "POST", body,
                          {"Content-Type": "application/json", "Authorization": "Bearer " + self.token})

    def send(self, path, method, body=b""):
        header = sync_transaction.header(self.public, self.private, self.seed, method, path, body, int(time.time()) + 120)
        return sync_http.request(self.origin, path, method, body if method in {"POST", "PUT"} else None,
                                 {"Authorization": "Bearer " + self.token, "X-Daochi-User": self.account,
                                  "X-Daochi-Tx": header,
                                  "Content-Type": "application/json" if method == "POST" else "application/octet-stream"})

    def blob(self, digest, data):
        assert hashlib.sha256(data).hexdigest() == digest
        path = "/api/v1/blobs/photos/" + digest
        try:
            self.send(path, "HEAD")
            return False
        except urllib.error.HTTPError as error:
            assert error.code == 404, error.code
        self.send(path, "PUT", data)
        self.send(path, "HEAD")
        return True

    def preview(self, jpeg):
        code, reply = native(2, self.identity, jpeg)
        assert code == 0 and len(reply) == 128 + len(jpeg) + 40
        preview_id, blob_id, sealed = reply[:64].decode(), reply[64:128].decode(), reply[128:]
        assert preview_id == hashlib.sha256(jpeg).hexdigest()
        self.blob(blob_id, sealed)
        return preview_id, blob_id

    def request(self, manifest, name, mime, video, taken_ms, width, height, preview=("", "")):
        numbers = struct.pack("!qqqqq", 3 if video else 1, taken_ms, width, height, self.since)
        return native(1, self.identity, self.client.encode(), name.encode(), mime.encode(), manifest,
                      preview[0].encode(), preview[1].encode(), tail=numbers)

    def upload(self, folder, name, mime, video=False, taken_ms=1700000000123, width=640, height=480, jpeg=None):
        manifest = (folder / "manifest.json").read_bytes()
        chunks = json.loads(manifest)["manifest"]["chunks"]
        for index, digest in enumerate(chunks):
            self.blob(digest, (folder / f"{index}.blob").read_bytes())
        preview = self.preview(jpeg) if jpeg else ("", "")
        code, body = self.request(manifest, name, mime, video, taken_ms, width, height, preview)
        assert code == 0 and len(body) <= 750000, code
        reply = self.send("/api/v1/sync", "POST", body)
        code, version = native(3, self.identity, reply, tail=struct.pack("!q", self.since))
        version = struct.unpack("!q", version)[0]
        assert code == 0 and version >= self.since, (code, version, reply[:200])
        self.since = version
        return body


def main():
    node_binary = os.environ.get("PHOTOS_DAOCHI") or shutil.which("daochi")
    if not node_binary or not Path(node_binary).is_file():
        raise SystemExit("Set PHOTOS_DAOCHI to the Daochi server executable for this isolated test")
    for program in (UPLOAD, STAGING):
        if not program.is_file():
            raise SystemExit(f"Build {program} first (make android-upload-test)")
    with tempfile.TemporaryDirectory(dir=ROOT / "build", prefix="upload-test-") as folder:
        folder = Path(folder)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("DAOCHI_") and k not in
               {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER"}}
        env.update(DAOCHI_ADDR=f"127.0.0.1:{port}", DAOCHI_BASE_URL=origin, DAOCHI_DB=str(folder / "node.sqlite"),
                   DAOCHI_ADMIN_TOKEN="fixture-admin", DAOCHI_KNOWN_NODES="", DAOCHI_LAN_DISCOVERY="0",
                   DAOCHI_NODE_SYNC_INTERVAL_SECONDS="0", DAOCHI_TOKEN_SECRET_HEX="42" * 32)
        log = (folder / "node.log").open("w")
        process = subprocess.Popen([str(Path(node_binary).resolve())], env=env, stdout=log, stderr=log)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            node = None
            for _ in range(200):
                try:
                    node = json.load(opener.open(origin + "/api/v1/node", timeout=1))
                    break
                except Exception:
                    if process.poll() is not None:
                        raise AssertionError((folder / "node.log").read_text())
                    time.sleep(.05)
            assert node
            registration = dict(app_id="photos", display_name="Photos", status="active",
                                collections=[dict(collection_prefix="private.photos.v1.records.*", visibility="private")])
            opener.open(urllib.request.Request(origin + "/api/v1/apps", method="POST", data=packed(registration).encode(),
                        headers={"X-Daochi-Admin": "fixture-admin", "Content-Type": "application/json"})).close()

            # The desktop already backed up one original and filed it in an album.
            source = folder / "export/Desk"
            source.mkdir(parents=True)
            Image.new("RGB", (400, 300), "#4c9180").save(source / "shared.jpg")
            desktop = Library(folder / "desktop")
            desktop.import_folders([source.parent])
            shared_id = digest_file(source / "shared.jpg")
            desktop.edit(shared_id, "album_add", "Desk album")
            config = dict(node=origin, node_id=node["node_id"])
            desktop_client = Client(desktop, config)
            assert desktop_client.sync() == 0

            identity_json = json.loads(desktop_client.identity_path.read_bytes())
            public, private = bytes.fromhex(identity_json["public"]), bytes.fromhex(identity_json["private"])
            identity = (bytes.fromhex(node["node_id"]) + hashlib.sha256(public).digest() + public + private
                        + bytes.fromhex(identity_json["content_key"]))
            assert len(identity) == 3968

            # The phone holds a new multi-chunk camera original and the same
            # bytes the desktop already owns, under a different device name.
            camera = folder / "camera"
            camera.mkdir()
            Image.new("RGB", (320, 240), "#ce9157").save(camera / "IMG_0001.jpg")
            with (camera / "IMG_0001.jpg").open("ab") as large:
                large.write(os.urandom(9 * 2**20))
            shutil.copyfile(source / "shared.jpg", camera / "IMG_0002.JPG")
            (camera / "VID_0003.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + os.urandom(4096))
            staged_root = folder / "staged"
            staged_root.mkdir(mode=0o700)
            ids = {}
            for name in ("IMG_0001.jpg", "IMG_0002.JPG", "VID_0003.mp4"):
                ids[name] = json.loads(stage(identity, camera / name, staged_root))["id"]
            assert ids["IMG_0002.JPG"] == shared_id
            preview = io.BytesIO()
            Image.new("RGB", (160, 120), "#ce9157").save(preview, "JPEG", quality=85)
            preview = preview.getvalue()

            phone = Phone(origin, identity)
            phone.node()
            # A different pinned node identity is refused before any login.
            wrong = bytes(32) + identity[32:]
            code, reply = native(4, wrong, sync_http.request(origin, "/api/v1/node", limit=1048576))
            assert code == 0 and reply == b"\x06", reply
            phone.login()
            first = phone.upload(staged_root / ids["IMG_0001.jpg"], "IMG_0001.jpg", "image/jpeg", jpeg=preview)
            phone.upload(staged_root / shared_id, "IMG_0002.JPG", "image/jpeg")
            phone.upload(staged_root / ids["VID_0003.mp4"], "VID_0003.mp4", "video/mp4", video=True,
                         taken_ms=-1000, width=0, height=0)
            # Requests carry only ciphertext: no name, keys or plaintext bytes.
            assert b"IMG_0001" not in first and identity_json["content_key"].encode() not in first
            # An interrupted worker can repeat a whole upload idempotently.
            phone.upload(staged_root / ids["IMG_0001.jpg"], "IMG_0001.jpg", "image/jpeg", jpeg=preview)
            # A stale node cursor is reported, not silently accepted.
            ahead = Phone(origin, identity)
            ahead.seed, ahead.client, ahead.token, ahead.since = phone.seed, phone.client, phone.token, 10**12
            code, body = ahead.request((staged_root / shared_id / "manifest.json").read_bytes(), "IMG_0002.JPG",
                                       "image/jpeg", False, 0, 1, 1)
            assert code == 0
            try:
                ahead.send("/api/v1/sync", "POST", body)
                raise AssertionError("cursor ahead of the node was accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 409, error.code

            # A separate desktop restores the phone's originals and previews.
            peer_library = Library(folder / "thinkpad")
            private_dir = peer_library.root / "private"
            private_dir.mkdir(mode=0o700, exist_ok=True)
            shutil.copyfile(desktop_client.identity_path, private_dir / "identity.json")
            os.chmod(private_dir / "identity.json", 0o600)
            peer = Client(peer_library, config)
            assert peer.sync() == 0
            assert peer_library.query()["total"] == 3, peer_library.query()["total"]
            new = peer_library.photo(ids["IMG_0001.jpg"])
            assert (new["name"], new["extension"], new["kind"], new["size"], new["taken"], new["width"], new["height"]) == \
                ("IMG_0001.jpg", ".jpg", "photo", (camera / "IMG_0001.jpg").stat().st_size, 1700000000, 640, 480), dict(new)
            assert json.loads(new["albums"]) == [] and new["favorite"] == 0 and new["trashed"] == 0
            assert Path(peer.materialize(ids["IMG_0001.jpg"], preview_only=True)).read_bytes() == preview
            assert Path(peer.materialize(ids["IMG_0001.jpg"])).read_bytes() == (camera / "IMG_0001.jpg").read_bytes()
            video = peer_library.photo(ids["VID_0003.mp4"])
            assert (video["kind"], video["extension"], video["taken"]) == ("video", ".mp4", -1), dict(video)
            assert Path(peer.materialize(ids["VID_0003.mp4"])).read_bytes() == (camera / "VID_0003.mp4").read_bytes()
            # The desktop's record wins for an original it already owned.
            albums = json.loads(desktop.photo(shared_id)["albums"])
            assert "Desk album" in albums
            shared = peer_library.photo(shared_id)
            assert json.loads(shared["albums"]) == albums and shared["name"] == "shared.jpg", dict(shared)
            assert desktop_client.sync() == 0
            assert json.loads(desktop.photo(shared_id)["albums"]) == albums
            assert desktop.query()["total"] == 3
            print(json.dumps(dict(passed=True, uploaded=3, restored_bytes=True, preview=True,
                                  desktop_metadata_preserved=True, stale_cursor_refused=True)))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            log.close()


if __name__ == "__main__":
    main()
