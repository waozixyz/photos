"""Android upload requests are exact, sealed and refuse malformed input."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from photos.library import packed
from photos.sync import decode
from photos.sync_crypto import decrypt

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = ROOT / "build/backup-upload-test/backup_upload_probe"
STAGING = ROOT / "build/backup-staging-test/backup_staging_probe"
NODE = bytes(range(100, 132))
PUBLIC, PRIVATE = bytes(range(256)) * 5 + bytes(32), bytes(reversed(range(256))) * 10
ACCOUNT = hashlib.sha256(PUBLIC).digest()
KEY = bytes(range(7, 39))
IDENTITY = NODE + ACCOUNT + PUBLIC + PRIVATE + KEY
COLLECTION = "private.photos.v1.records.media"


def native(mode, *fields, tail=b"", identity=IDENTITY):
    data = bytes([mode]) + identity + b"".join(struct.pack("!I", len(f)) + f for f in fields) + tail
    result = subprocess.run(["prlimit", "--core=0", "--as=268435456", "--", str(UPLOAD)],
                            input=data, capture_output=True, timeout=30)
    assert not result.stderr, result.stderr
    return result.returncode, result.stdout


class BackupUploadTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="upload-", dir=ROOT / "build/backup-upload-test")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "IMG_1.jpg"
        self.source.write_bytes(b"\xff\xd8synthetic camera original\xff\xd9")
        self.staged = self.root / "staged"
        self.staged.mkdir(mode=0o700)
        result = subprocess.run([str(STAGING), str(self.source), str(self.staged), str(self.source.stat().st_size)],
                                input=ACCOUNT + KEY, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.manifest = result.stdout
        self.id = json.loads(self.manifest)["id"]

    def request(self, name="IMG_1.jpg", mime="image/jpeg", media=1, taken=1700000000999, width=4032, height=3024,
                since=7, manifest=None, preview=b"", blob=b"", client=b"photos-android-0123456789abcdef01234567"):
        return native(1, client, name.encode(), mime.encode(), self.manifest if manifest is None else manifest,
                      preview, blob, tail=struct.pack("!qqqqq", media, taken, width, height, since))

    def open(self, body):
        request = json.loads(body)
        self.assertEqual(set(request), {"app_id", "client_clock", "client_id", "encrypted_records",
                                        "encrypted_records_only", "protocol_version", "record_limit",
                                        "since_server_version", "user_id_hash"})
        self.assertEqual((request["protocol_version"], request["app_id"], request["user_id_hash"], request["record_limit"],
                          request["encrypted_records_only"]), (6, "photos", ACCOUNT.hex(), 1, True))
        [record] = request["encrypted_records"]
        self.assertEqual((record["collection"], record["id"], record["key_id"], record["schema_version"], record["updated_at"]),
                         (COLLECTION, self.id, "main", 1, "1970-01-01T00:00:00Z"))
        envelope = json.loads(decode(record["ciphertext"]))
        aad = dict(account_id=ACCOUNT.hex(), app_id="photos", collection=COLLECTION, id=self.id, schema="photos.media.v1")
        self.assertEqual((envelope["profile"], envelope["alg"], envelope["key_id"], envelope["aad"], envelope["nonce"]),
                         ("daochi-record-v1", "xchacha20poly1305", "main", aad, record["nonce"]))
        plain = decrypt(decode(envelope["ciphertext"]), packed(aad).encode(), decode(envelope["nonce"]), KEY)
        return request, json.loads(plain)

    def test_request_seals_a_desktop_compatible_device_baseline_record(self):
        code, body = self.request()
        self.assertEqual(code, 0)
        request, photo = self.open(body)
        self.assertEqual((request["since_server_version"], request["client_clock"]), (7, 7))
        self.assertEqual(photo, dict(albums=[], archived=0, caption="", edits={}, extension=".jpg", favorite=0, height=3024,
                                     hidden=0, id=self.id, kind="photo", manifest=json.loads(self.manifest)["manifest"],
                                     metadata={}, name="IMG_1.jpg", size=self.source.stat().st_size, tags="",
                                     taken=1700000000, trashed=0, updated=0, width=4032))
        for secret in (KEY, PRIVATE[:64], b"IMG_1", b"synthetic camera"):
            self.assertNotIn(secret.hex().encode() if secret in (KEY, PRIVATE[:64]) else secret, body)
        # Each request seals with a fresh nonce.
        self.assertNotEqual(json.loads(self.request()[1])["encrypted_records"][0]["nonce"],
                            json.loads(body)["encrypted_records"][0]["nonce"])

    def test_extension_comes_from_supported_name_or_provider_type(self):
        cases = [("IMG.HEIC", "image/heic", 1, ".heic", "photo"), ("photo", "image/png", 1, ".png", "photo"),
                 ("clip.MOV", "video/quicktime", 3, ".mov", "video"), ("clip", "video/mp4", 3, ".mp4", "video"),
                 ("名前 \"quoted\" \\ é.jpg", "image/jpeg", 1, ".jpg", "photo")]
        for name, mime, media, extension, kind in cases:
            with self.subTest(name=name):
                code, body = self.request(name=name, mime=mime, media=media)
                self.assertEqual(code, 0)
                photo = self.open(body)[1]
                self.assertEqual((photo["name"], photo["extension"], photo["kind"]), (name, extension, kind))

    def test_preview_is_content_addressed_and_opens_with_the_desktop_reader(self):
        jpeg = b"\xff\xd8preview pixels\xff\xd9"
        code, reply = native(2, jpeg)
        self.assertEqual(code, 0)
        preview_id, blob_id, sealed = reply[:64].decode(), reply[64:128].decode(), reply[128:]
        self.assertEqual((preview_id, blob_id), (hashlib.sha256(jpeg).hexdigest(), hashlib.sha256(sealed).hexdigest()))
        aad = packed(dict(account_id=ACCOUNT.hex(), app_id="photos", id=preview_id, part="preview",
                          schema="photos.media.v1")).encode()
        self.assertEqual(decrypt(sealed[24:], aad, sealed[:24], KEY), jpeg)
        self.assertEqual(native(2, jpeg)[1], reply)  # resumable: identical retries
        code, body = self.request(preview=preview_id.encode(), blob=blob_id.encode())
        self.assertEqual(code, 0)
        manifest = self.open(body)[1]["manifest"]
        self.assertEqual((manifest["preview"], manifest["preview_id"]), (blob_id, preview_id))
        self.assertEqual(native(2, b"")[0], 1)
        self.assertEqual(self.request(preview=preview_id.encode())[0], 1)
        self.assertEqual(self.request(preview=b"A" * 64, blob=blob_id.encode())[0], 1)

    def test_malformed_or_unsupported_inputs_build_no_request(self):
        manifest = json.loads(self.manifest)
        changed = dict(manifest, size=manifest["size"] + 8388608)
        extra = dict(manifest, manifest=dict(manifest["manifest"], chunks=manifest["manifest"]["chunks"] * 2))
        upper = dict(manifest, id=manifest["id"].upper())
        bad_chunk = dict(manifest, manifest=dict(chunk_bytes=8388608, chunks=["0" * 63]))
        for label, kwargs in [("unsupported type", dict(name="doc.pdf", mime="application/pdf")),
                              ("video named as photo", dict(name="clip", mime="video/mp4", media=1)),
                              ("control name", dict(name="a\nb.jpg")), ("empty name", dict(name="")),
                              ("long name", dict(name="a" * 1025 + ".jpg")),
                              ("negative size", dict(width=-1)), ("huge height", dict(height=1000001)),
                              ("year 10000", dict(taken=253402300800000)), ("negative cursor", dict(since=-1)),
                              ("quoted client", dict(client=b'photos"x')), ("empty client", dict(client=b"")),
                              ("size mismatch", dict(manifest=packed(changed).encode())),
                              ("chunk mismatch", dict(manifest=packed(extra).encode())),
                              ("uppercase id", dict(manifest=packed(upper).encode())),
                              ("short chunk", dict(manifest=packed(bad_chunk).encode())),
                              ("not json", dict(manifest=b"{")), ("trailing", dict(manifest=self.manifest + b"{}"))]:
            with self.subTest(label):
                self.assertEqual(self.request(**kwargs), (1, b""))

    def test_confirmation_requires_an_ok_encrypted_records_reply_at_or_after_the_cursor(self):
        def confirmed(value, since=5):
            code, reply = native(3, value if type(value) is bytes else packed(value).encode(), tail=struct.pack("!q", since))
            self.assertEqual(code, 0)
            return struct.unpack("!q", reply)[0]
        good = dict(status="ok", server_version=9, encrypted_records_only=True, changes=dict(encrypted_records=[]))
        self.assertEqual(confirmed(good), 9)
        self.assertEqual(confirmed(dict(good, full_snapshot_required=False)), 9)
        for label, value in [("error", dict(good, status="error")), ("snapshot", dict(good, full_snapshot_required=True)),
                             ("legacy", dict(good, encrypted_records_only=False)), ("behind", dict(good, server_version=4)),
                             ("float", dict(good, server_version=9.5)), ("missing", dict(status="ok")),
                             ("array", [good]), ("text", b"ok")]:
            with self.subTest(label):
                self.assertEqual(confirmed(value), -1)

    def test_only_the_pinned_node_without_outward_photos_replication_is_allowed(self):
        def check(value):
            code, reply = native(4, packed(value).encode())
            self.assertEqual(code, 0)
            return reply[0]
        node = dict(node_id=NODE.hex(), known_nodes=[dict(sync=dict(direction="pull", apps=["photos"]))])
        self.assertEqual(check(node), 0)
        self.assertEqual(check(dict(node, node_id=bytes(32).hex())), 6)
        self.assertEqual(check(dict(node, known_nodes=[dict(sync=dict(direction="push", apps=["photos"]))])), 7)
        self.assertEqual(check(dict(node, known_nodes=[dict(sync=None)])), 7)
        self.assertEqual(check(dict(node_id=NODE.hex())), 5)


if __name__ == "__main__":
    unittest.main()
