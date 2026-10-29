"""Pinned project-local quality tools and reproducible build Python."""

import errno
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from terminal import progress  # noqa: E402
from dev_cache import CACHE as DEV_CACHE, clone_tree  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "quality"
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
PYTHON_ASSETS = {
    "linux-arm64": [
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.12.14%2B20260924-aarch64-unknown-linux-gnu-install_only.tar.gz",
        "c0574c9c9fec561eb192ad510e4e79aaad039929b3531b3bb873e2f630fb14a9",
    ],
    "linux-amd64": [
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.12.14%2B20260924-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "5eae8cf79dd47fc2496a4fccc892936be831ce7a84d984b2299dfb1cdb592682",
    ],
    "windows-arm64": [
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.12.14%2B20260924-aarch64-pc-windows-msvc-install_only.tar.gz",
        "394b3bf95c45e5b02e02fe9b201e51df11dbff612db5b26406c6db484581317d",
    ],
    "windows-amd64": [
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.12.14%2B20260924-x86_64-pc-windows-msvc-install_only.tar.gz",
        "c5303174bc29f5205decf6721ac549d4eb41c448f9b8c46cbc562d00348865bb",
    ],
}
BINARY_ASSETS = {
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
    "rclone-linux-amd64": [
        "https://github.com/rclone/rclone/releases/download/v1.74.3/rclone-v1.74.3-linux-amd64.zip",
        "dbee7ccd7a5d617e4ed4cd4555c16669b511abfe8d31164f61be35ac9e999bd2",
    ],
    "rclone-linux-arm64": [
        "https://github.com/rclone/rclone/releases/download/v1.74.3/rclone-v1.74.3-linux-arm64.zip",
        "8f8d47446e061f80c3256659fe8e21f56d72d96aaefe1275d088ea5eb6b42aa7",
    ],
    "rclone-windows-amd64": [
        "https://github.com/rclone/rclone/releases/download/v1.74.3/rclone-v1.74.3-windows-amd64.zip",
        "ecb0ed9006e0d1a693757007716a11dab6c2cde6dac3f2fd87da962eaa73d11d",
    ],
    "rclone-windows-arm64": [
        "https://github.com/rclone/rclone/releases/download/v1.74.3/rclone-v1.74.3-windows-arm64.zip",
        "5b6dcef9272f81caa44da9f9c216c385309b78cf53ebe1bd83a45bced50fc991",
    ],
    "actionlint-linux-amd64": [
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ],
    "actionlint-linux-arm64": [
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_arm64.tar.gz",
        "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
    ],
    "actionlint-windows-amd64": [
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_windows_amd64.zip",
        "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9",
    ],
    "actionlint-windows-arm64": [
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_windows_arm64.zip",
        "cadcf7ea4efe3a68728893813643cebe1185e5b1d4be5b96245f65c9a4d5ea41",
    ],
    "shfmt-linux-amd64": [
        "https://github.com/mvdan/sh/releases/download/v3.14.1/shfmt_v3.14.1_linux_amd64",
        "76e77641faa025814b77f153b29796b8e6fa2fca03e0c76a691608b86c7ea7bf",
    ],
    "shfmt-linux-arm64": [
        "https://github.com/mvdan/sh/releases/download/v3.14.1/shfmt_v3.14.1_linux_arm64",
        "5f2db09dae91fca848f7adbdd014632e921a383863a2ad7e0450ad3aba0c6489",
    ],
    "shfmt-windows-amd64": [
        "https://github.com/mvdan/sh/releases/download/v3.14.1/shfmt_v3.14.1_windows_amd64.exe",
        "13629ce28442ca80b6b5a819f7574ab39e1c28c6e26734ca816c9714e04851df",
    ],
    "shellcheck-linux-arm64": [
        "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.aarch64.tar.gz",
        "68a8133197a50beb8803f8d42f9908d1af1c5540d4bb05fdfca8c1fa47decefc",
    ],
    "shellcheck-linux-amd64": [
        "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.gz",
        "b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6",
    ],
    "shellcheck-windows-amd64": [
        "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.zip",
        "8a4e35ab0b331c85d73567b12f2a444df187f483e5079ceffa6bda1faa2e740e",
    ],
    "just-windows-arm64": [
        "https://github.com/casey/just/releases/download/1.57.0/just-1.57.0-aarch64-pc-windows-msvc.zip",
        "14ff33bbac8d2f07deda30cd3bafe7be083213cdb1c4dae7a55567d375cc17f1",
    ],
    "just-linux-arm64": [
        "https://github.com/casey/just/releases/download/1.57.0/just-1.57.0-aarch64-unknown-linux-musl.tar.gz",
        "f225044a81adea6e0b3a8b9370aaf374e6af76c8735ae263ac993df55fd137ec",
    ],
    "just-windows-amd64": [
        "https://github.com/casey/just/releases/download/1.57.0/just-1.57.0-x86_64-pc-windows-msvc.zip",
        "4c7391d17cb1d17b758b52004ee6411372b8a13ff37c3c9b9031625cb6026e09",
    ],
    "just-linux-amd64": [
        "https://github.com/casey/just/releases/download/1.57.0/just-1.57.0-x86_64-unknown-linux-musl.tar.gz",
        "45b548094283cb9739af8f13273b8cddeee869f5b4ef2bb631b1f311cb566155",
    ],
}


def child_env():
    env = os.environ.copy()
    for key in (
        "APPIMAGE",
        "__MISE_SHIM",
        "__MISE_DIFF",
        "PYTHONHOME",
        "PYTHONPATH",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "CLASSPATH",
    ):
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    sdk = Path(env.get("JAVA_HOME", "")) / "bin"
    env["PATH"] = os.pathsep.join(
        [
            *([str(sdk)] if env.get("JAVA_HOME") and sdk.is_dir() else []),
            str(CACHE / "bin"),
            *(
                entry
                for entry in env.get("PATH", "").split(os.pathsep)
                if not (
                    Path(entry).name.lower() == "shims"
                    and Path(entry).parent.name.lower() == "mise"
                )
            ),
        ]
    )
    return env


def native(name):
    java_tool = Path(name).name.lower()
    if java_tool in (
        "java",
        "java.exe",
        "javac",
        "javac.exe",
        "jdb",
        "jdb.exe",
        "gradlew",
        "gradlew.bat",
    ):
        sdk = java_home()
        environment = child_env()
        environment["JAVA_HOME"] = str(sdk)
        environment["PATH"] = os.pathsep.join(
            dict.fromkeys([str(sdk / "bin"), *environment["PATH"].split(os.pathsep)])
        )
        os.environ.clear()
        os.environ.update(environment)
        if java_tool not in ("gradlew", "gradlew.bat"):
            extension = ".exe" if host()[0] == "windows" else ""
            name = sdk / "bin" / (java_tool.removesuffix(".exe") + extension)
    executable = shutil.which(str(name), path=child_env()["PATH"])
    if executable is None:
        raise RuntimeError(
            f"Missing native {name}; install the repository's documented SDK prerequisites"
        )
    return executable


def call(*args, capture=False):
    executable = native(args[0])
    return subprocess.run(
        [executable, *map(str, args[1:])],
        check=True,
        env=child_env(),
        text=True,
        stdout=subprocess.PIPE if capture else sys.stderr,
        shell=os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"),
    ).stdout


def host():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if system not in ("linux", "windows") or arch is None:
        raise RuntimeError(f"Unsupported platform: {system}/{machine}")
    return system, arch


def retried(fetch, attempts=3):
    """Runners hit transient GitHub/R2 failures (HTTP 5xx, read timeouts); checksums still verify the bytes."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch()
        except (OSError, subprocess.SubprocessError) as error:
            permanent = (
                isinstance(error, urllib.error.HTTPError)
                and error.code < 500
                and error.code != 429
            )
            if permanent or attempt == attempts:
                raise
            print(
                f"download failed ({error}), retry {attempt}/{attempts - 1}",
                file=sys.stderr,
            )
            time.sleep(10 * attempt)


def read_url(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def download(name, url, checksum, *, cache=None):
    target = (CACHE if cache is None else cache) / name
    if target.is_file():
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != checksum:
            raise RuntimeError(
                f"Tool checksum mismatch: {name} (delete {target} and retry)"
            )
        return target
    with progress(f"Downloading {name}"):
        data = retried(lambda: read_url(url))
    if hashlib.sha256(data).hexdigest() != checksum:
        raise RuntimeError(f"Tool checksum mismatch: {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".dl-", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def check_java(directory, system):
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
        env=child_env(),
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
    system, arch = host()
    key = system + "-" + arch
    home = Path(os.environ.get("BF_DEV_HOME") or ROOT / ".dev").resolve() / "jdk"
    directory = home / ("temurin-" + JAVA_VERSION + "-" + key)
    cached = DEV_CACHE / "jdk" / directory.name
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
        archive = download(Path(url).name, url, checksum, cache=DEV_CACHE / "jdk")
        home.mkdir(parents=True, exist_ok=True)
        with (
            progress("Unpacking and verifying Java runtime"),
            tempfile.TemporaryDirectory(dir=home) as temporary,
        ):
            staging = Path(temporary)
            if system == "windows":
                with zipfile.ZipFile(archive) as source:
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


def python_version(python):
    return call(
        python,
        "-I",
        "-X",
        "utf8",
        "-c",
        "import sys,zlib; print(sys.version_info[:3],zlib.ZLIB_VERSION,zlib.ZLIB_RUNTIME_VERSION)",
        capture=True,
    ).strip()


def canonical_python():
    if sys.version_info < (3, 12):
        raise RuntimeError("Install native Python 3.12 or later to run project tools")
    if sys.version_info[:3] == (3, 12, 14) and (
        zlib.ZLIB_VERSION,
        zlib.ZLIB_RUNTIME_VERSION,
    ) == ("1.3.2", "1.3.2"):
        return Path(sys.executable)
    key = "-".join(host())
    directory = CACHE / ("python-3.12.14-" + key)
    relative = Path("python") / ("python.exe" if os.name == "nt" else "bin/python3")
    python = directory / relative
    if not python.is_file():
        archive = download("python-3.12.14-" + key + ".tar.gz", *PYTHON_ASSETS[key])
        with (
            progress("Unpacking and verifying Python runtime"),
            tempfile.TemporaryDirectory(dir=CACHE) as temporary,
        ):
            staging = Path(temporary) / "install"
            staging.mkdir()
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
            version = python_version(staging / relative)
            if version != "(3, 12, 14) 1.3.2 1.3.2":
                raise RuntimeError(f"Canonical Python compressor mismatch: {version}")
            staging.rename(directory)
    version = python_version(python)
    if version != "(3, 12, 14) 1.3.2 1.3.2":
        raise RuntimeError(f"Canonical Python compressor mismatch: {version}")
    return python


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
    system, arch = host()
    url, checksum = BINARY_ASSETS[name + "-" + system + "-" + arch]
    archive = download(name + "-" + Path(url).name, url, checksum)
    files = []
    with deb_payload(archive) as source:
        for path in paths:
            data = source.extractfile("./" + path).read()
            target = CACHE / name / Path(path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
            files.append(target)
    return files


def tool(name):
    if name == "ruff":
        directory = CACHE / "venv"
        python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            call(canonical_python(), "-I", "-m", "venv", "--clear", directory)
        probe = subprocess.run(
            [str(python), "-I", "-m", "ruff", "--version"],
            env=child_env(),
            capture_output=True,
            text=True,
        )
        if probe.returncode or probe.stdout.strip() != "ruff 0.15.7":
            # pip does not retry a read timeout in the middle of a download.
            retried(
                lambda: call(
                    python,
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--disable-pip-version-check",
                    "ruff==0.15.7",
                )
            )
        return directory / ("Scripts/ruff.exe" if os.name == "nt" else "bin/ruff")
    if name in ("prettier", "pnpm"):
        version = {"prettier": "3.8.3", "pnpm": "11.18.0"}[name]
        directory = CACHE / "npm"
        binary = (
            directory
            / "node_modules"
            / ".bin"
            / (name + ".cmd" if os.name == "nt" else name)
        )
        if not binary.is_file():
            call(
                "npm",
                "install",
                "--prefix",
                directory,
                "--no-audit",
                "--no-fund",
                "--ignore-scripts",
                "--package-lock=false",
                "--save-exact",
                name + "@" + version,
            )
        return binary
    system, arch = host()
    # These tools publish amd64 Windows binaries, supported by Windows 11 ARM64 emulation.
    if system == "windows" and name in ("shellcheck", "shfmt", "boat"):
        arch = "amd64"
    url, checksum = BINARY_ASSETS[name + "-" + system + "-" + arch]
    archive = download(name + "-" + Path(url).name, url, checksum)
    filename = name + (".exe" if system == "windows" else "")
    if url.endswith(".deb"):
        with deb_payload(archive) as source:
            data = source.extractfile("./usr/bin/" + filename).read()
    elif url.endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            member = next(n for n in source.namelist() if Path(n).name == filename)
            data = source.read(member)
    elif url.endswith(".tar.gz"):
        with tarfile.open(archive) as source:
            member = next(
                m for m in source.getmembers() if Path(m.name).name == filename
            )
            data = source.extractfile(member).read()
    else:
        data = archive.read_bytes()
    binary = CACHE / "bin" / filename
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.is_file() or binary.read_bytes() != data:
        binary.write_bytes(data)
    binary.chmod(0o755)
    return binary


if __name__ == "__main__":
    if sys.argv[1:] == ["--ci"]:
        tool("just")
        tool("shellcheck")
        python = canonical_python()
        with Path(os.environ["GITHUB_PATH"]).open(
            "a", encoding="utf-8", newline="\n"
        ) as output:
            output.write(str(CACHE / "bin") + "\n" + str(python.parent) + "\n")
    else:
        call(canonical_python(), "-I", "-X", "utf8", *sys.argv[1:])
