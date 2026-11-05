#!/usr/bin/env python3
"""Temporary Python oracle for synthetic store and crypto migration fixtures.

This never opens an installed library, account database, config or node. C14
loads the SQL files into disposable libraries and compares their persisted state.
Protocol and encrypted recovery goldens are deliberately outside --store-only.
"""
import argparse
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from photos.library import Library, packed

STAMP = 1700000000.125
PHOTO = hashlib.sha256(b"Photos synthetic store fixture").hexdigest()
PERSON = hashlib.sha256(b"Photos synthetic person fixture").hexdigest()[:32]
MISSING = {
    "photos": ("edits", "archived"),
    "albums": ("filter", "auto_people", "entity", "record"),
    "people": ("birth",),
}


def sql_dump(database):
    return "\n".join(database.iterdump()) + "\n"


def snapshot(database):
    """Preserve SQL values and BLOB bytes, with deterministic row ordering."""
    tables = {}
    for (name,) in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        assert name.replace("_", "").isalnum()
        columns = [row[1] for row in database.execute(f'PRAGMA table_info("{name}")')]
        values = []
        for row in database.execute(f'SELECT * FROM "{name}"'):
            values.append({column: {"blob_hex": value.hex()} if isinstance(value, bytes)
                           else value for column, value in zip(columns, row)})
        tables[name] = {"columns": columns,
                        "rows": sorted(values, key=lambda row: packed(row))}
    return tables


def insert(database, table, values):
    columns = ",".join(f'"{key}"' for key in values)
    placeholders = ",".join("?" for _ in values)
    database.execute(f'INSERT INTO "{table}"({columns}) VALUES({placeholders})',
                     list(values.values()))


def seed(library):
    db = library.db
    metadata = packed({"unknown": {"null": None, "large_integer": 2**60 + 3,
                                   "label": "Synthetic: sí 旅行", "list": [True, 2]},
                       "camera_date": "1965:02:03 04:05:06"})
    insert(db, "photos", dict(
        id=PHOTO, name="Synthetic 'trip' 旅行.jpg", extension=".jpg", kind="photo",
        size=128, taken=-154641294.5, width=17, height=9, metadata=metadata,
        albums=packed(["Fixture album", "Imported only"]), favorite=1, hidden=0,
        archived=1, trashed=0, caption="Synthetic caption with 'quotes' and sí",
        tags="fixture,旅行", updated=STAMP, dirty=1,
        manifest=packed({"unknown": {"preserve": [None, 2**60 + 7]}}),
        edits=packed({"rotate": 90, "unknown": {"keep": True}})))
    insert(db, "sources", dict(path="/synthetic/photos/trip.jpg", size=128,
                               mtime=1700000000000000000, meta_stamp="synthetic-stamp",
                               photo_id=PHOTO))
    library.set_setting("fixture_unknown", {"nested": {"null": None},
                                            "large_integer": 2**60 + 11,
                                            "unicode": "旅行", "enabled": False})
    insert(db, "people", dict(id=PERSON, name="Synthetic person", ignored=0,
                              updated=STAMP, birth="1962-02-03"))
    vector = struct.pack("<4f", 1.0, 0.5, -1.0, 0.0)
    insert(db, "semantic_embeddings", dict(photo_id=PHOTO, model="synthetic-clip",
                                           embedding=vector, error=""))
    insert(db, "motion_scans", dict(photo_id=PHOTO, reader_version=1))
    insert(db, "face_scans", dict(photo_id=PHOTO, model="synthetic-face", error=""))
    insert(db, "faces", dict(id="synthetic-face", photo_id=PHOTO, person_id=PERSON,
                             rect=packed([0.1, 0.2, 0.3, 0.4]), embedding=vector,
                             excluded=packed(["synthetic-excluded-person"])))
    insert(db, "person_tags", dict(photo_id=PHOTO, person_id=PERSON))
    insert(db, "person_tag_edits", dict(photo_id=PHOTO))
    library._ensure_album("Fixture album")
    library._ensure_album("Imported only")
    db.execute("UPDATE albums SET description=?,cover=?,pinned=1,filter=?,auto_people=?,entity=?,record=? WHERE name=?",
               ("Synthetic preserved description", PHOTO, "tag:fixture",
                packed([PERSON]), "0123456789abcdef0123456789abcdef",
                packed({"unknown": {"keep": [None, "旅行"]}}), "Fixture album"))
    db.commit()


def CaptureLibraryFixtures(output: Path) -> None:
    output = output.resolve()
    fixture_root = (ROOT / "tests/fixtures/golden").resolve()
    if output != fixture_root and fixture_root not in output.parents:
        raise ValueError("Final synthetic fixtures belong under tests/fixtures/golden")
    scratch = ROOT / "build/tests/tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="golden-store-", dir=scratch) as temp:
        base = Path(temp)
        with patch("photos.library.time.time", return_value=STAMP):
            current = Library(base / "current")
            try:
                seed(current)
                current_dump = sql_dump(current.db)
                current_state = snapshot(current.db)
                legacy_root = base / "legacy"
                legacy_root.mkdir(mode=0o700)
                with sqlite3.connect(legacy_root / "library.sqlite") as legacy:
                    current.db.backup(legacy)
                    for table, columns in MISSING.items():
                        for column in columns:
                            legacy.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
                    legacy.execute("DELETE FROM settings WHERE key='album_catalog_migrated'")
                    legacy.execute("DELETE FROM albums WHERE name='Imported only'")
                    legacy.commit()
                    legacy_dump = sql_dump(legacy)
                    legacy_state = snapshot(legacy)
            finally:
                current.db.close()

            migrated = Library(legacy_root)
            try:
                migrated_dump = sql_dump(migrated.db)
                migrated_state = snapshot(migrated.db)
                photo = dict(migrated.db.execute("SELECT * FROM photos").fetchone())
                assert photo["id"] == PHOTO and photo["archived"] == 0 and photo["edits"] == "{}"
                assert photo["metadata"] == current_state["photos"]["rows"][0]["metadata"]
                assert photo["taken"] == -154641294.5
                assert migrated.setting("album_catalog_migrated") is True
                assert migrated.db.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 2
                for table in ("sources", "faces", "face_scans", "semantic_embeddings",
                              "motion_scans", "person_tags", "person_tag_edits"):
                    assert migrated_state[table] == legacy_state[table], table
                assert migrated.setting("fixture_unknown")["large_integer"] == 2**60 + 11
            finally:
                migrated.db.close()
            repeated = Library(legacy_root)
            try:
                assert sql_dump(repeated.db) == migrated_dump, "schema migration must be idempotent"
                assert snapshot(repeated.db) == migrated_state
            finally:
                repeated.db.close()

        files = {"library-v0.1.59.sql": current_dump,
                 "library-legacy.sql": legacy_dump,
                 "library-legacy-migrated.sql": migrated_dump}
        for name, data in files.items():
            (output / name).write_text(data, encoding="utf-8")
        expected = {"synthetic_only": True, "photo_id": PHOTO, "person_id": PERSON,
                    "fixture_time": STAMP, "legacy_missing_columns": MISSING,
                    "current": current_state, "legacy": legacy_state,
                    "legacy_migrated": migrated_state,
                    "notes": ["Legacy album catalog marker and one album row are absent.",
                              "Missing fields receive defaults; existing IDs and other values remain.",
                              "Protocol, encryption and recovery fixtures remain pending."]}
        (output / "library-expectations.json").write_text(
            json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
    print("Captured four synthetic store fixtures: current, legacy, migrated and persisted expectations")


def CaptureCryptoFixtures(output):
    """Independent Python primitives, synthetic bytes only; never call helpers."""
    from nacl import bindings, pwhash
    from pqcrypto.sign import ml_dsa_44

    output = output.resolve()
    fixture_root = (ROOT / "tests/fixtures/golden").resolve()
    if output != fixture_root and fixture_root not in output.parents:
        raise ValueError("Final synthetic fixtures belong under tests/fixtures/golden")
    output.mkdir(parents=True, exist_ok=True)
    key, nonce, salt = bytes(range(32)), bytes(range(24)), bytes(range(16))
    private = bytes(i % 256 for i in range(2560))
    plain = b"Synthetic Photos compatibility only\0\xff" + bytes(range(256))
    aad = b'{"account_id":"synthetic","app_id":"photos","id":"fixture","part":"record"}'
    password = "synthetic recovery password 旅行".encode()
    recovery_plain = packed(dict(version=1, node_id="a"*64, account="",
                                 identity=dict(content_key=key.hex()))).encode()
    record_key = hmac.digest(private, b"daochi-entity-v1/records/photos", "sha256")
    digest = hashlib.sha256(plain).hexdigest()
    base = hmac.digest(private, b"daochi-entity-v1/blobs/photos", "sha256")
    blob_key = hmac.digest(base, digest.encode(), "sha256")
    chunk_key = hmac.digest(blob_key, b"chunk/0", "sha256")
    encrypt = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt
    blob_nonce = hmac.digest(key, aad+hashlib.sha256(plain).digest(), "sha256")[:24]
    recovery_key = pwhash.argon2id.kdf(32, password, salt, opslimit=3, memlimit=67108864)
    public_path, secret_path = output/"crypto-account-public.bin", output/"crypto-account-secret.bin"
    if public_path.exists() and secret_path.exists():
        public, secret = public_path.read_bytes(), secret_path.read_bytes()
    else:
        public, secret = ml_dsa_44.generate_keypair()
    files = {
        "crypto-key.bin": key, "crypto-private.bin": private,
        "crypto-plain.bin": plain, "crypto-aad.bin": aad, "crypto-nonce.bin": nonce,
        "crypto-sync.bin": encrypt(plain, aad, nonce, key),
        "crypto-blob.bin": blob_nonce+encrypt(plain, aad, blob_nonce, key),
        "crypto-record-key.bin": record_key,
        "crypto-record.bin": nonce+encrypt(plain, aad, nonce, record_key),
        "crypto-chunk.bin": bindings.crypto_aead_chacha20poly1305_ietf_encrypt(
            plain, b"daochi-entity-v1/blob/photos/0", bytes(12), chunk_key),
        "crypto-hash.txt": digest.encode(), "crypto-password.txt": password,
        "crypto-recovery-plain.bin": recovery_plain,
        "crypto-recovery.bin": b"PHOTOS-RECOVERY\x01"+salt+nonce+encrypt(
            recovery_plain, b"photos-recovery-v1", nonce, recovery_key),
        "crypto-account-public.bin": public, "crypto-account-secret.bin": secret,
    }
    # Keep an existing randomized signature if it still proves this fixture.
    proof = output/"crypto-account-signature.bin"
    signature = proof.read_bytes() if proof.exists() else b""
    if len(signature) != 2420 or not ml_dsa_44.verify(public, plain, signature):
        signature = ml_dsa_44.sign(secret, plain)
    files["crypto-account-signature.bin"] = signature
    for name, data in files.items():
        (output/name).write_bytes(data)
    (output/"crypto-expectations.json").write_text(json.dumps(dict(
        synthetic_only=True, entity_kind="photos", entity_chunk_index=0,
        entity_chunk_bytes=8388608, account_algorithm="ML-DSA-44",
        account_public_bytes=1312, account_secret_bytes=2560, account_signature_bytes=2420,
        recovery_magic="PHOTOS-RECOVERY\\x01", recovery_argon2id_version=19,
        recovery_passes=3, recovery_memory_bytes=67108864,
        notes=["All keys, passwords and content are public synthetic test data.",
               "Python primitives create these files without invoking Photos pipe helpers."]),
        sort_keys=True, indent=2)+"\n", encoding="utf-8")
    print("Captured synthetic Python crypto fixtures (binary bodies omitted)")


def VerifyCryptoFixtures(output):
    """Open in-process Ziran outputs with independent Python primitives."""
    from nacl import bindings, pwhash
    from nacl.signing import VerifyKey
    from pqcrypto.sign import ml_dsa_44

    read = lambda name: (output/('crypto-'+name)).read_bytes()
    produced = lambda name: (ROOT/'build/tests'/('crypto-'+name+'-ziran.bin')).read_bytes()
    plain, aad, key = read('plain.bin'), read('aad.bin'), read('key.bin')
    decrypt = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt
    assert decrypt(produced('sync'), aad, read('nonce.bin'), key) == plain
    assert produced('blob') == read('blob.bin')
    record = produced('record')
    assert decrypt(record[24:], aad, record[:24], read('record-key.bin')) == plain
    private = read('private.bin')
    digest = hashlib.sha256(plain).hexdigest().encode()
    base = hmac.digest(private, b'daochi-entity-v1/blobs/photos', 'sha256')
    blob_key = hmac.digest(base, digest, 'sha256')
    chunk_key = hmac.digest(blob_key, b'chunk/0', 'sha256')
    assert bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
        produced('chunk'), b'daochi-entity-v1/blob/photos/0', bytes(12), chunk_key) == plain
    assert produced('chunk') == read('chunk.bin')
    password = read('password.txt')
    recovery = produced('recovery')
    derived = pwhash.argon2id.kdf(32, password, recovery[:16], opslimit=3, memlimit=67108864)
    assert decrypt(recovery[40:], b'photos-recovery-v1', recovery[16:40], derived) == read('recovery-plain.bin')
    assert ml_dsa_44.verify(read('account-public.bin'), plain, produced('account'))
    assert VerifyKey(produced('device-public')).verify(plain, produced('device')) == plain
    print("Python opened and verified all in-process Ziran crypto outputs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--store-only", action="store_true")
    mode.add_argument("--crypto-only", action="store_true")
    mode.add_argument("--verify-crypto", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "tests/fixtures/golden")
    args = parser.parse_args()
    if args.crypto_only:
        CaptureCryptoFixtures(args.output)
    elif args.verify_crypto:
        VerifyCryptoFixtures(args.output)
    else:
        CaptureLibraryFixtures(args.output)


if __name__ == "__main__":
    main()
