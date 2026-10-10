#!/usr/bin/env python3
"""Build pinned libsodium for the app ABIs without changing its source package."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "armeabi-v7a": ("arm-linux-androideabi", "armv7a-linux-androideabi"),
    "arm64-v8a": ("aarch64-linux-android", "aarch64-linux-android"),
    "x86_64": ("x86_64-linux-android", "x86_64-linux-android"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--abis", default="armeabi-v7a,arm64-v8a,x86_64")
    args = parser.parse_args()
    source = args.source.resolve()
    if not (source / "configure").is_file():
        raise RuntimeError("Pinned libsodium package has no configure script")
    sdk = Path(os.environ.get("ANDROID_HOME", str(Path.home() / "Android/Sdk")))
    toolchain = sdk / "ndk/28.2.13676358/toolchains/llvm/prebuilt/linux-x86_64/bin"
    if not toolchain.is_dir():
        raise RuntimeError("Install the NDK version declared by the Android project")
    output = ROOT / "build/android/crypto"
    output.mkdir(parents=True, exist_ok=True)
    # Never replace outputs in use by a second invocation.
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for abi in args.abis.split(","):
            if abi not in TARGETS:
                raise RuntimeError("Unsupported Photos ABI: " + abi)
            host, compiler = TARGETS[abi]
            build = output / abi
            build.mkdir(exist_ok=True)
            install = build / "install"
            commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            identity = dict(source=str(source), commit=commit, configure=hashlib.sha256((source / "configure").read_bytes()).hexdigest(),
                            toolchain=str(toolchain), api=28, abi=abi)
            marker = build / "source.json"
            archive = install / "lib/libsodium.a"
            if marker.is_file() and json.loads(marker.read_text()) == identity and archive.is_file():
                print("Pinned Android encryption ready: " + abi, flush=True)
                continue
            env = dict(os.environ, CC=str(toolchain / (compiler + "28-clang")),
                       AR=str(toolchain / "llvm-ar"), RANLIB=str(toolchain / "llvm-ranlib"),
                       STRIP=str(toolchain / "llvm-strip"), CFLAGS="-O2 -fPIC")
            if (build / "Makefile").is_file():
                subprocess.run(["make", "clean"], cwd=build, env=env, check=True)
            subprocess.run([str(source / "configure"), "--host=" + host, "--prefix=" + str(install),
                            "--disable-shared", "--enable-static", "--disable-pie", "--disable-dependency-tracking"],
                           cwd=build, env=env, check=True)
            subprocess.run(["make", "-j2"], cwd=build, env=env, check=True)
            subprocess.run(["make", "install"], cwd=build, env=env, check=True)
            if not archive.is_file():
                raise RuntimeError("Android encryption archive was not produced")
            marker.write_text(json.dumps(identity, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
