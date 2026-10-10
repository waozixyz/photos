#!/usr/bin/env python3
"""Synthetic account recovery for the private Android emulator; no owner data."""
import hashlib
from pathlib import Path
import secrets
import sys

from nacl.signing import SigningKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from photos import account_sign, recovery
from photos.library import packed

output = ROOT / "build/android/fixtures"
output.mkdir(parents=True, exist_ok=True)
public, private = account_sign.generate_keypair()
# A synthetic Daochi node identity: the emulator upload check starts its own
# disposable node from this seed, so the recovered pin must match it exactly.
node_seed = secrets.token_bytes(32)
(output.parent / "node-seed.bin").write_bytes(node_seed)
node_id = hashlib.sha256(SigningKey(node_seed).verify_key.encode()).hexdigest()
value = dict(version=1, node_id=node_id, account=hashlib.sha256(public).hexdigest(),
             identity=dict(public=public.hex(), private=private.hex(), content_key=secrets.token_hex(32)))
(output / "recovery.bin").write_bytes(recovery.MAGIC + recovery.crypt("Synthetic Android recovery password", packed(value).encode()))
(output / "expected.bin").write_bytes(bytes.fromhex(value["node_id"] + value["account"]))
