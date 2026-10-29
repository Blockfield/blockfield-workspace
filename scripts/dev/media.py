"""Project-local, checksum-verified render and video tools."""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from toolchain import CACHE, download, host, native

FFMPEG_ASSETS = {
    "linux-amd64": (
        "https://files.pythonhosted.org/packages/a0/2d/43c8522a2038e9d0e7dbdf3a61195ecc31ca576fb1527a528c877e87d973/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl",
        "c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282",
    ),
    "linux-arm64": (
        "https://files.pythonhosted.org/packages/33/e7/1925bfbc563c39c1d2e82501d8372734a5c725e53ac3b31b4c2d081e895b/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_aarch64.whl",
        "1d47bebd83d2c5fc770720d211855f208af8a596c82d17730aa51e815cdee6dc",
    ),
    "windows-amd64": (
        "https://files.pythonhosted.org/packages/2c/c6/fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl",
        "02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a",
    ),
}
MESA_ASSET = (
    "https://github.com/pal1000/mesa-dist-win/releases/download/26.2.2/mesa3d-26.2.2-release-msvc.7z",
    "0f21394d44036019bb5c07a3788dd278fd7b7b3469077a4686f0d639e8e874a1",
)
MESA_DLL_SHA256 = {
    "opengl32.dll": "c09183fb3021b6abd56625f7ff10acf72b873d4bf2b5ab92b24cd55122dec94d",
    "libgallium_wgl.dll": "3e91e42114c3c3b9a0054176cb9916288e507c98256bc82b2a34dea31e860012",
}


def ffmpeg():
    system, arch = host()
    if system == "windows":
        arch = "amd64"
    key = system + "-" + arch
    url, checksum = FFMPEG_ASSETS[key]
    archive = download(Path(url).name, url, checksum)
    directory = CACHE / "media" / key
    binary = directory / ("ffmpeg.exe" if system == "windows" else "ffmpeg")
    with zipfile.ZipFile(archive) as wheel:
        members = [
            name
            for name in wheel.namelist()
            if name.startswith("imageio_ffmpeg/binaries/ffmpeg-")
            and not name.endswith("/")
        ]
        if len(members) != 1:
            raise RuntimeError("Pinned FFmpeg wheel must contain one encoder")
        data = wheel.read(members[0])
    directory.mkdir(parents=True, exist_ok=True)
    if not binary.is_file() or binary.read_bytes() != data:
        binary.write_bytes(data)
        binary.chmod(0o755)
    if system == "windows":
        stub = directory / "avicap32.dll"
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        if (windows / "System32" / "avicap32.dll").is_file():
            stub.unlink(missing_ok=True)
        elif not stub.is_file() or stub.read_bytes() != AVICAP32_STUB:
            stub.write_bytes(AVICAP32_STUB)
    return binary


def _avicap32_stub():
    """Every Windows FFmpeg build links AVICAP32.dll statically for its webcam input (vfwcap).

    Server Core (Blacksmith's Windows runners) has no AVICAP32, so ffmpeg.exe fails to start
    with 0xC0000135. FFmpeg imports only capCreateCaptureWindowA and capGetDriverDescriptionA and
    calls them only for vfwcap input, which we never use. This stub, found first in the
    application directory, exports both returning 0 ("no capture device").
    """
    import struct

    names = [b"capCreateCaptureWindowA", b"capGetDriverDescriptionA"]
    base = 0x1000  # .text RVA, file offset 0x200
    code = b"\xb8\x01\x00\x00\x00\xc3\x90\x90" + b"\x31\xc0\xc3".ljust(8, b"\x90")
    entry, stub = base, base + 8  # DllMain: return TRUE; exports: return 0
    exports = base + len(code)
    functions = exports + 40
    name_table = functions + 4 * len(names)
    ordinals = name_table + 4 * len(names)
    strings = ordinals + 2 * len(names)
    blob, rvas = b"", []
    for name in [b"avicap32.dll", *names]:
        rvas.append(strings + len(blob))
        blob += name + b"\0"
    section = (
        code
        + struct.pack(
            "<IIHHIIIIIII",
            0,
            0,
            0,
            0,
            rvas[0],
            1,
            2,
            2,
            functions,
            name_table,
            ordinals,
        )
        + struct.pack("<II", stub, stub)
        + struct.pack("<II", *rvas[1:])
        + struct.pack("<HH", 0, 1)
        + blob
    )
    export_size = len(section) - len(code)
    optional = (
        struct.pack(
            "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
            0x20B,
            14,
            0,
            0x200,
            0,
            0,
            entry,
            base,
            0x180000000,
            0x1000,
            0x200,
            6,
            0,
            0,
            0,
            6,
            0,
            0,
            0x2000,
            0x200,
            0,
            2,
            0x160,
            0x100000,
            0x1000,
            0x100000,
            0x1000,
            0,
            16,
        )
        + struct.pack("<II", exports, export_size)
        + bytes(15 * 8)
    )
    header = (
        b"MZ".ljust(0x3C, b"\0")
        + struct.pack("<I", 0x40)
        + b"PE\0\0"
        + struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, len(optional), 0x2022)
        + optional
        + struct.pack(
            "<8sIIIIIIHHI",
            b".text",
            len(section),
            base,
            0x200,
            0x200,
            0,
            0,
            0,
            0,
            0x60000020,
        )
    )
    return header.ljust(0x200, b"\0") + section.ljust(0x200, b"\0")


AVICAP32_STUB = _avicap32_stub()


def encoder_info(binary):
    result = subprocess.run(
        [str(binary), "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if not any(
        len(fields := line.split()) >= 2 and fields[1] == "libx264"
        for line in result.stdout.splitlines()
    ):
        raise RuntimeError(
            "Pinned FFmpeg does not provide the required libx264 encoder"
        )
    version = subprocess.run(
        [str(binary), "-version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.splitlines()[0]
    return {
        "version": version,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }


def encode_capture(manifest_path, binary=None):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frames = manifest["frames"]
    seconds = manifest.get("requested_seconds")
    if (
        type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0.1 <= seconds <= 20
    ):
        raise ValueError(
            "Capture duration must be finite and between 0.1 and 20 seconds"
        )
    if (
        manifest.get("complete") is not True
        or not 1 <= len(frames) <= 200
        or not all(frame.get("saved") is True for frame in frames)
    ):
        raise ValueError("Only a complete capture with saved frames can be encoded")
    lines = []
    previous = -1.0
    for index, frame in enumerate(frames):
        at = frame.get("at_seconds")
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or not previous < at < seconds
            or at < 0
        ):
            raise ValueError(
                "Frame timestamps must be finite, increasing and inside the capture"
            )
        previous = at
        name = frame["file"]
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or PureWindowsPath(name).drive
            or len(path.parts) != 1
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.png", name)
        ):
            raise ValueError("Unsafe capture frame path")
        actual = (manifest_path.parent / name).resolve()
        if actual.parent != manifest_path.parent or not actual.is_file():
            raise ValueError(
                "Capture frame is missing or outside its evidence directory"
            )
        end = (
            frames[index + 1]["at_seconds"]
            if index + 1 < len(frames)
            else manifest["requested_seconds"]
        )
        duration = float(end) - at
        lines.extend([f"file '{name}'", f"duration {duration:.6f}"])
    lines.append(f"file '{frames[-1]['file']}'")
    binary = Path(binary) if binary else ffmpeg()
    info = encoder_info(binary)
    playlist = manifest_path.parent / "frames.txt"
    output = manifest_path.parent / "capture.mp4"
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        subprocess.run(
            [
                str(binary),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(playlist),
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-t",
                str(seconds - frames[0]["at_seconds"]),
                str(output),
            ],
            cwd=manifest_path.parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        playlist.unlink(missing_ok=True)
    manifest["video"] = {
        "file": output.name,
        "codec": "libx264",
        "start_offset_seconds": frames[0]["at_seconds"],
        "end_offset_seconds": seconds,
        **info,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return output


def windows_java(jdk_bin):
    if host() != ("windows", "amd64"):
        raise RuntimeError(
            "The verified Windows software renderer requires Windows x64"
        )
    jdk_bin = Path(jdk_bin).resolve()
    if not (jdk_bin / "java.exe").is_file():
        raise RuntimeError("Windows renderer requires an existing project-local JDK")
    directory = jdk_bin.parent.parent / (jdk_bin.parent.name + "-headless-mesa-26.2.2")
    marker = directory / "mesa.json"
    if marker.is_file():
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        if (
            receipt.get("archive_sha256") == MESA_ASSET[1]
            and receipt.get("dlls") == MESA_DLL_SHA256
            and (directory / "bin" / "java.exe").is_file()
            and all(
                (directory / "bin" / name).is_file()
                and hashlib.sha256((directory / "bin" / name).read_bytes()).hexdigest()
                == sha
                for name, sha in MESA_DLL_SHA256.items()
            )
        ):
            return directory / "bin" / "java.exe"
        raise RuntimeError("Cached Windows renderer checksum mismatch")
    archive = download(Path(MESA_ASSET[0]).name, *MESA_ASSET)
    listing = subprocess.run(
        [native("tar"), "-tf", str(archive)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    for name in listing:
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or PureWindowsPath(name).drive:
            raise RuntimeError("Unsafe Mesa archive member")
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=directory.parent, prefix=".mesa-"
    ) as temporary:
        staging = Path(temporary)
        subprocess.run(
            [
                native("tar"),
                "-xf",
                str(archive),
                "-C",
                str(staging),
                *["x64/" + name for name in MESA_DLL_SHA256],
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        dlls = [staging / "x64" / name for name in MESA_DLL_SHA256]
        if any(
            not dll.is_file()
            or hashlib.sha256(dll.read_bytes()).hexdigest() != MESA_DLL_SHA256[dll.name]
            for dll in dlls
        ):
            raise RuntimeError("Pinned Mesa DLL checksum mismatch")
        target = staging / "jdk"

        def link_or_copy(source, destination):
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
            return destination

        shutil.copytree(jdk_bin.parent, target, copy_function=link_or_copy)
        checks = {}
        for dll in dlls:
            destination = target / "bin" / dll.name
            if destination.exists():
                destination.unlink()
            shutil.copy2(dll, destination)
            checks[dll.name] = hashlib.sha256(dll.read_bytes()).hexdigest()
        (target / "mesa.json").write_text(
            json.dumps({"archive_sha256": MESA_ASSET[1], "dlls": checks}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        target.rename(directory)
    return directory / "bin" / "java.exe"
