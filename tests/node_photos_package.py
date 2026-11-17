"""Validate the real module artifact with the supported signer, using a test key."""
import copy
from hashlib import sha256
import importlib.util
import json
import tempfile
from pathlib import Path
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

root = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("node_release", Path(sys.argv[1]) / "scripts/node_release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
key = Ed25519PrivateKey.generate()
pin = {
    "key_id": "photos-local-test",
    "app_ids": ["inbe.photos"],
    "public_key": key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex(),
}
release, data = publisher.node_app_release(
    "photos", key, pin["key_id"], ledger_path=root / "portable/versions.json",
    bundle_directory=root / "build/node-apps",
)
publisher.validate_release(release, "inbe.photos", [pin])
assert release["host_api"] == 43 and release["module_api"] == 2
assert release["data_schema"] == 0 and release["dependencies"] == []
assert release["version"] == "1.0.6" and release["sequence"] == 7
assert data == (root / "build/node-apps/photos.zx").read_bytes()
assert data[:8] == b"ZX\0\0\x1c\0\0\0", "Photos must use the current ZX28 format"
assert release["artifacts"] == [{"variant": "module", "sha256": sha256(data).hexdigest(), "size": len(data)}]
for field, value in (("host_api", 41), ("module_api", 3), ("version", "1.0.7")):
    tampered = copy.deepcopy(release)
    tampered[field] = value
    try:
        publisher.validate_release(tampered, "inbe.photos", [pin])
    except ValueError:
        pass
    else:
        raise AssertionError(f"tampered {field} accepted")
tampered = copy.deepcopy(release)
tampered["artifacts"][0]["sha256"] = "0" * 64
try:
    publisher.validate_release(tampered, "inbe.photos", [pin])
except ValueError:
    pass
else:
    raise AssertionError("tampered payload identity accepted")
try:
    publisher.validate_release(release, "inbe.photos", [])
except ValueError:
    pass
else:
    raise AssertionError("untrusted publisher accepted")
# Exercise the supported local publication path with this disposable key.
# A changed ABI cannot overwrite an already signed version and sequence.
with tempfile.TemporaryDirectory(prefix="photos-package-", dir=root / "build/tests") as folder:
    directory = Path(folder)
    encode = lambda value: (json.dumps(value, separators=(",", ":")) + "\n").encode()
    publisher.write_node_app(directory, release, encode(release), data, [pin])
    assert (directory / "latest-v2.json").read_bytes() == encode(release)
    assert (directory / (sha256(data).hexdigest() + ".zx")).read_bytes() == data
    changed = copy.deepcopy(release)
    changed["host_api"] = 41
    changed["signature"] = key.sign(publisher.signing_message(changed)).hex()
    publisher.validate_release(changed, "inbe.photos", [pin])
    try:
        publisher.write_node_app(directory, changed, encode(changed), data, [pin])
    except ValueError as error:
        assert "advance its version and sequence" in str(error)
    else:
        raise AssertionError("an occupied version and sequence were overwritten")
    assert (directory / "latest-v2.json").read_bytes() == encode(release)
print("Photos local package: host 43/module 2, payload identity, signature and tamper rejection passed")
