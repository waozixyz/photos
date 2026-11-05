"""Temporary independent golden oracle; only existing synthetic key fixtures."""
import hashlib
import json
from pathlib import Path
import sys

from nacl import bindings, pwhash

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/golden"
PASSWORD = b"private recovery fixture password"
PIN = "a" * 64
MAGIC = b"PHOTOS-RECOVERY\x01"


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def seal(name, value):
    plain = packed(value)
    salt, nonce = bytes(range(16)), bytes(range(24))
    key = pwhash.argon2id.kdf(32, PASSWORD, salt, opslimit=3, memlimit=67108864)
    cipher = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        plain, b"photos-recovery-v1", nonce, key)
    (GOLDEN / f"recovery-{name}.photos-recovery").write_bytes(MAGIC + salt + nonce + cipher)


def generate():
    public = (GOLDEN / "crypto-account-public.bin").read_bytes()
    private = (GOLDEN / "crypto-account-secret.bin").read_bytes()
    if len(public) != 1312 or len(private) != 2560:
        raise ValueError("Synthetic key fixture lengths differ")
    content = bytes(range(32)).hex()
    for name, identity in [
        ("legacy", dict(public=public.hex(), private=private.hex(), content_key=content)),
        ("content", dict(content_key=content)),
    ]:
        account = hashlib.sha256(public).hexdigest() if name == "legacy" else ""
        value = dict(version=1, node_id=PIN, account=account, identity=identity)
        seal(name, value)
        (GOLDEN / f"recovery-{name}-identity.json").write_bytes(packed(identity))
        if name == "legacy":
            seal("wrong-account", dict(value, account="b" * 64))
            bad = bytes([private[0] ^ 1]) + private[1:]
            seal("wrong-private", dict(value, identity=dict(identity, private=bad.hex())))
    print("Synthetic whole-file and invalid-account/key recovery fixtures captured")


def verify(path):
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC) or not 73 <= len(data) <= 65608:
        raise ValueError("Native recovery framing differs")
    raw = data[len(MAGIC):]
    key = pwhash.argon2id.kdf(32, PASSWORD, raw[:16], opslimit=3, memlimit=67108864)
    plain = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
        raw[40:], b"photos-recovery-v1", raw[16:40], key)
    expected = json.loads((GOLDEN / "recovery-legacy-identity.json").read_bytes())
    value = json.loads(plain)
    if value != dict(version=1, node_id=PIN,
                     account=hashlib.sha256(bytes.fromhex(expected["public"])).hexdigest(),
                     identity=expected):
        raise ValueError("Native recovery identity differs")
    print("Native full recovery file opened by independent Python baseline")


if __name__ == "__main__":
    generate() if len(sys.argv) == 1 else verify(sys.argv[1])
