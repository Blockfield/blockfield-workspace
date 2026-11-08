"""Workspace-only tool pins: Temurin JDK, boat CLI, Debian Xvfb/VirtualGL.

Not part of the vendored core (scripts/toolchain.py): the core only reads
scripts/tools.lock.json, and every other repo keeps its own repo-specific
module next to its copy of the core. This module is never vendored elsewhere.
"""

import errno
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from dev_cache import clone_tree
from terminal import progress

JAVA_VERSION = "21.0.12+8"
JAVA_ASSETS = {
    "linux-amd64": [
        "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12%2B8/OpenJDK21U-jdk_x64_linux_hotspot_21.0.12_8.tar.gz",
        "e4446ff06a276155697597cc0f1b15da004ff083f4964a35271ecee567177370",
    ],
    "linux-arm64": [
        "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12%2B8/OpenJDK21U-jdk_aarch64_linux_hotspot_21.0.12_8.tar.gz",
        "eba38e871b02d407897bfe017ea35352dfc1420ef6d2112425b0c67325ca509d",
    ],
    "windows-amd64": [
        "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12%2B8/OpenJDK21U-jdk_x64_windows_hotspot_21.0.12_8.zip",
        "9ba963ee2371874a74185d18bc7bb2ab9407df7683300855ed7606e0662321d0",
    ],
    "windows-arm64": [
        "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12%2B8/OpenJDK21U-jdk_aarch64_windows_hotspot_21.0.12_8.zip",
        "e7ddf1cf45885cae2cfb8a018db60b0041ef69e96843b93a92b447d9a4cba676",
    ],
}
LOCAL_ASSETS = {
    "boat-linux-amd64": [
        "https://github.com/ariana-dot-dev/agent-server/releases/download/boat-cli-v1.0.85/boat-linux-x64",
        "5d7933a238dcea0446bbbee0e2506692961d16537b9960003e4ece9ecb1ec498",
    ],
    "boat-linux-arm64": [
        "https://github.com/ariana-dot-dev/agent-server/releases/download/boat-cli-v1.0.85/boat-linux-arm64",
        "a7d8a3a577deeea5c77f314aaf2b07657c4cfefb9f48e560119354566c72bea8",
    ],
    "boat-windows-amd64": [
        "https://github.com/ariana-dot-dev/agent-server/releases/download/boat-cli-v1.0.85/boat-windows-x64.exe",
        "02ebb405804f1bf353f5e3b651d3b3c51d3411858d33641e5b3847435d357fdc",
    ],
    "Xvfb-linux-amd64": [
        "https://deb.debian.org/debian/pool/main/x/xorg-server/xvfb_21.1.16-1.3+deb13u3_amd64.deb",
        "365da2b6c93339f34337c434a9489bb6411a240fa47b9f49693d3091745f28c2",
    ],
    "virtualgl-linux-amd64": [
        "https://github.com/VirtualGL/virtualgl/releases/download/3.1.5/virtualgl_3.1.5_amd64.deb",
        "df3f7788ce41b182a47c0d298e5cd6d2d63579522cb41825970b7726e825485e",
    ],
}


def check_java(directory, system):
    import toolchain

    extension = ".exe" if system == "windows" else ""
    binary = directory / "bin"
    if not all(
        (binary / (name + extension)).is_file() for name in ("java", "javac", "jdb")
    ):
        raise RuntimeError(
            "A complete managed JDK 21 is required, including javac and jdb"
        )
    probe = subprocess.run(
        [str(binary / ("java" + extension)), "-version"],
        env=toolchain.child_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode or not re.search(
        re.escape(JAVA_VERSION) + r"(?![\d.+])", probe.stderr
    ):
        raise RuntimeError(
            f"Managed Java version mismatch: expected Temurin {JAVA_VERSION}"
        )


def java_home():
    """Workspace-managed Temurin 21 (same layout and guards as before the split)."""
    import toolchain

    system, arch = toolchain.host()
    key = system + "-" + arch
    home = (
        Path(os.environ.get("BF_DEV_HOME") or toolchain.ROOT / ".dev").resolve() / "jdk"
    )
    directory = home / ("temurin-" + JAVA_VERSION + "-" + key)
    cached = toolchain.DEV_CACHE / "jdk" / directory.name
    if not directory.is_dir() and cached.is_dir():
        home.mkdir(parents=True, exist_ok=True)
        # Clone via a temporary name so an interrupted copy is never mistaken
        # for an installed JDK on the next run.
        with tempfile.TemporaryDirectory(dir=home) as temporary:
            pending = Path(temporary) / "tree"
            clone_tree(cached, pending)
            try:
                pending.rename(directory)
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
    if not directory.is_dir():
        url, checksum = JAVA_ASSETS[key]
        print(f"Installing Temurin {JAVA_VERSION} in {directory}", file=sys.stderr)
        archive = toolchain.download(
            Path(url).name, url, checksum, cache=toolchain.DEV_CACHE / "jdk"
        )
        home.mkdir(parents=True, exist_ok=True)
        with (
            progress("Unpacking and verifying Java runtime"),
            tempfile.TemporaryDirectory(dir=home) as temporary,
        ):
            staging = Path(temporary)
            if system == "windows":
                with zipfile.ZipFile(archive) as source:
                    _check_zip_members(source)
                    source.extractall(staging)
            else:
                with tarfile.open(archive) as source:
                    source.extractall(staging, filter="data")
            extracted = staging / ("jdk-" + JAVA_VERSION)
            check_java(extracted, system)
            if not cached.exists():
                cached.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=cached.parent) as cache_tmp:
                    pending = Path(cache_tmp) / "tree"
                    clone_tree(extracted, pending)
                    try:
                        pending.rename(cached)
                    except OSError as error:
                        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                            raise
            with tempfile.TemporaryDirectory(dir=home) as temporary:
                pending = Path(temporary) / "tree"
                clone_tree(cached, pending)
                try:
                    pending.rename(directory)
                except OSError as error:
                    if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                        raise
    else:
        check_java(directory, system)
    return directory


def _check_zip_members(source):
    import stat

    for member in source.infolist():
        name = member.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or PureWindowsPath(name).drive
            or stat.S_ISLNK(member.external_attr >> 16)
        ):
            raise RuntimeError(f"Unsafe JDK archive member: {name}")


def deb_payload(archive):
    """The data.tar.xz of a Debian package, opened."""
    package = io.BytesIO(archive.read_bytes())
    if package.read(8) != b"!<arch>\n":
        raise RuntimeError("Invalid Debian tool archive")
    while header := package.read(60):
        if len(header) != 60 or header[58:] != b"`\n":
            raise RuntimeError("Invalid Debian tool member")
        size = int(header[48:58])
        payload = package.read(size)
        if size % 2:
            package.read(1)
        if header[:16].strip().rstrip(b"/") == b"data.tar.xz":
            return tarfile.open(fileobj=io.BytesIO(payload))
    raise RuntimeError("Missing Debian tool payload")


def deb_libraries(name, *paths):
    """Files of a pinned Debian package (by package path), extracted into the cache."""
    import toolchain

    system, arch = toolchain.host()
    url, checksum = LOCAL_ASSETS[name + "-" + system + "-" + arch]
    archive = toolchain.download(name + "-" + Path(url).name, url, checksum)
    files = []
    with deb_payload(archive) as source:
        for path in paths:
            data = source.extractfile("./" + path).read()
            target = toolchain.cache_dir() / name / Path(path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
            files.append(target)
    return files


def local_tool(name):
    """Raw workspace-only binaries (boat CLI, Xvfb) the lock does not pin."""
    import toolchain

    system, arch = toolchain.host()
    if system == "windows" and name in ("boat",):
        arch = "amd64"  # Windows 11 ARM64 runs the amd64 binary by emulation.
    try:
        url, checksum = LOCAL_ASSETS[name + "-" + system + "-" + arch]
    except KeyError:
        raise RuntimeError(f"Unknown tool {name!r}: not pinned for {system}-{arch}")
    archive = toolchain.download(name + "-" + Path(url).name, url, checksum)
    filename = name + (".exe" if system == "windows" else "")
    if url.endswith(".deb"):
        with deb_payload(archive) as source:
            data = source.extractfile("./usr/bin/" + filename).read()
    else:
        data = archive.read_bytes()
    binary = toolchain.cache_dir() / "bin" / filename
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.is_file() or binary.read_bytes() != data:
        binary.write_bytes(data)
    binary.chmod(0o755)
    return binary
