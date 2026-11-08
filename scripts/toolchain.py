"""Pinned project-local quality tools and reproducible build Python.

Core downloader: the single pin set lives in scripts/tools.lock.json and this
module only reads it. Every download is retried with backoff, written
atomically, and verified against the locked hash. `BF_TOOL_CACHE` points all
tools at one shared cache directory (hook-test fixtures and fresh worktrees
use it); `toolchain.py --tools a,b` prefetches named tools for CI.

Workspace-only code (Temurin JDK, boat CLI, Debian Xvfb/VirtualGL) lives in
scripts/toolchain_local.py and is re-exported here, so existing imports keep
working. Other repos vendor this file plus the lock, with their own local
module next to it.
"""

import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

__all__ = [
    "CACHE",
    "DEV_CACHE",
    "LOCK_PATH",
    "cache_dir",
    "call",
    "canonical_python",
    "check_hash",
    "child_env",
    "download",
    "fetch",
    "host",
    "jdk_env",
    "load_lock",
    "lock_platform",
    "lock_tool",
    "native",
    "publish_link",
    "read_url",
    "replace",
    "retried",
    "ruff_version",
    "tool",
    # Re-exported from scripts/toolchain_local.py (workspace-only pins).
    "JAVA_ASSETS",
    "JAVA_VERSION",
    "check_java",
    "deb_libraries",
    "deb_payload",
    "java_home",
]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from terminal import progress  # noqa: E402
from dev_cache import CACHE as DEV_CACHE, clone_tree  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "quality"
LOCK_PATH = ROOT / "scripts" / "tools.lock.json"


def load_lock():
    """The single pin set every repo vendors: tool, version, per-platform
    sources in try order, and the hash each source must verify against."""
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except OSError as error:
        raise RuntimeError(f"Cannot read tool pins at {LOCK_PATH}: {error}")
    if lock.get("version") != 1 or "tools" not in lock:
        raise RuntimeError(f"Unsupported tool pins at {LOCK_PATH}: expected version 1")
    return lock


def cache_dir():
    """Shared tool cache: BF_TOOL_CACHE when set (hook-test fixtures and
    fresh worktrees point it at the parent checkout's cache), else this
    checkout's .cache/quality."""
    override = os.environ.get("BF_TOOL_CACHE")
    return Path(override) if override else CACHE


def lock_tool(name, lock=None):
    try:
        return (lock or load_lock())["tools"][name]
    except KeyError:
        raise RuntimeError(
            f"Unknown tool {name!r}: not pinned in scripts/tools.lock.json"
        )


def lock_platform(name, platform=None, lock=None):
    """Locked entry, resolved platform key (aliases, then platform-independent
    "any"), and its source spec for this host."""
    lock = lock or load_lock()
    entry = lock_tool(name, lock)
    key = platform or "-".join(host())
    platforms = entry["platforms"]
    if key not in platforms:
        key = entry.get("aliases", {}).get(key, "any")
    try:
        return entry, key, platforms[key]
    except KeyError:
        raise RuntimeError(f"No {key} asset pinned for tool {name!r}")


def check_hash(data, spec, name):
    """Verify downloaded bytes against the locked hash (sha256, or the
    registry integrity value for npm tarballs)."""
    if "sha256" in spec:
        if hashlib.sha256(data).hexdigest() != spec["sha256"]:
            raise RuntimeError(f"Tool checksum mismatch: {name}")
    elif "integrity" in spec:
        algorithm, _, encoded = spec["integrity"].partition("-")
        try:
            expected = base64.b64decode(encoded)
            digest = hashlib.new(algorithm, data).digest()
        except (ValueError, TypeError) as error:
            raise RuntimeError(f"Tool checksum mismatch: {name} ({error})")
        if digest != expected:
            raise RuntimeError(f"Tool checksum mismatch: {name}")
    else:
        raise RuntimeError(f"No hash pinned for tool {name!r}")


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
            str(cache_dir() / "bin"),
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


def jdk_env():
    """Env with the pinned Temurin 21 first: every Gradle/Java entrypoint uses
    this instead of inheriting whatever java the host PATH happens to carry."""
    environment = child_env()
    sdk = str(java_home())
    environment["JAVA_HOME"] = sdk
    entries = [str(Path(sdk) / "bin"), *environment.get("PATH", "").split(os.pathsep)]
    environment["PATH"] = os.pathsep.join(
        dict.fromkeys(part for part in entries if part)
    )
    return environment


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


def replace(target, data, mode=None):
    """Publish bytes so concurrent checkouts never see a partial file (and a
    running binary is never rewritten in place): sibling temp file, then rename
    into place. Mode is applied when given (tool binaries need 0o755)."""
    target = Path(target)
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
        if mode is not None:
            os.chmod(temporary, mode)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


CANONICAL_PYTHON_PROBE = "(3, 12, 14) 1.3.2 1.3.2"


def download(name, url, checksum, *, cache=None):
    """Single pinned file: cached copy verified, else downloaded with retries
    and published atomically. Kept for explicit callers; lock-driven code
    uses fetch() for ordered source fallback."""
    root = Path(cache) if cache is not None else cache_dir()
    target = root / name
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
    replace(target, data)
    return target


def fetch(tool_name, filename=None, *, cache=None, platform=None):
    """Locked tool file: each source tried in lock order, every source
    retried and hash-verified."""
    lock = load_lock()
    entry, key, spec = lock_platform(tool_name, platform, lock)
    urls = list(spec["urls"])
    name = filename or Path(urls[0]).name
    root = Path(cache) if cache is not None else cache_dir()
    target = root / name
    if target.is_file():
        try:
            check_hash(target.read_bytes(), spec, name)
        except RuntimeError as error:
            raise RuntimeError(f"{error} (delete {target} and retry)")
        return target
    failures = []
    for url in urls:
        try:
            with progress(f"Downloading {name}"):
                data = retried(lambda url=url: read_url(url))
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(f"{url}: {error}")
            continue
        try:
            check_hash(data, spec, name)
        except RuntimeError as error:
            failures.append(f"{url}: {error}")
            continue
        replace(target, data)
        return target
    raise RuntimeError(f"Tool {tool_name!r} download failed: " + "; ".join(failures))


def ruff_version(python):
    try:
        probe = subprocess.run(
            [str(python), "-I", "-m", "ruff", "--version"],
            env=child_env(),
            capture_output=True,
            text=True,
        )
    except OSError:  # interpreter missing or a dangling symlink
        return ""
    return "" if probe.returncode else probe.stdout.strip()


def _install_ruff_venv(directory, python, expected):
    try:
        call(canonical_python(), "-I", "-m", "venv", directory)
        # The wheel is hash-verified by fetch(); pip installs the local file.
        wheel = fetch("ruff")
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
                str(wheel),
            )
        )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    if ruff_version(python) != expected:
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError(f"ruff install did not produce {expected}")


def _build_ruff_venv(directory, python, expected):
    """One builder per versioned dir wins a mkdir claim and builds in place: a
    venv is not relocatable (renaming breaks installed shebangs with "bad
    interpreter"), so there is no staging rename. Losers wait for a valid
    install; nothing installed is ever cleared or reinstalled under a runner."""
    try:
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir()
    except FileExistsError:
        pass
    else:
        _install_ruff_venv(directory, python, expected)
        return
    for _ in range(60):  # a cold venv+pip build takes ~1-2 min; bound the wait
        if python.is_file() and ruff_version(python) == expected:
            return
        time.sleep(5)
    # Nobody published: reclaim a stalled or damaged dir, else say how to fix.
    shutil.rmtree(directory, ignore_errors=True)
    try:
        directory.mkdir()
    except FileExistsError:
        raise RuntimeError(
            f"Tool ruff install is contested: delete {directory} and retry"
        )
    _install_ruff_venv(directory, python, expected)


def tool_ruff():
    # Keyed by the locked version, like tool_npm: a lock bump must not reuse
    # (or reinstall under) another pin's venv.
    version = lock_tool("ruff")["version"]
    expected = "ruff " + version
    directory = cache_dir() / ("venv-ruff-" + version)
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file() or ruff_version(python) != expected:
        _build_ruff_venv(directory, python, expected)
    return directory / ("Scripts/ruff.exe" if os.name == "nt" else "bin/ruff")


def tool_npm(name):
    # Keyed by the locked version, like canonical_python: a lock bump must not reuse an old install.
    directory = cache_dir() / ("npm-" + name + "-" + lock_tool(name)["version"])
    binary = (
        directory
        / "node_modules"
        / ".bin"
        / (name + ".cmd" if os.name == "nt" else name)
    )
    if not binary.is_file():
        # The tarball carries the registry integrity hash; npm installs the
        # verified local file, so no registry round-trip can swap the bytes.
        tarball = fetch(name)
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
            str(tarball),
        )
    return binary


def publish_link(binary):
    """Point bin/<name> at the pinned binary with an atomic link swap, so
    bare-name PATH use (CI exports, VM provisioning) keeps resolving while the
    pin-keyed file itself is never rewritten under a runner. Best effort: the
    absolute pinned path is the contract, the link is compatibility."""
    binary = Path(binary)
    link = binary.parent.parent / binary.name
    try:
        if link.is_symlink() and link.readlink() == binary:
            return
    except OSError:
        pass
    try:
        descriptor, name = tempfile.mkstemp(dir=link.parent, prefix=".link-")
        os.close(descriptor)
        candidate = Path(name)
    except OSError as error:
        print(f"toolchain: no PATH link for {binary} ({error})", file=sys.stderr)
        return
    try:
        candidate.unlink()
        candidate.symlink_to(binary)
        os.replace(candidate, link)
    except OSError as error:
        candidate.unlink(missing_ok=True)
        print(f"toolchain: no PATH link for {binary} ({error})", file=sys.stderr)


def tool(name):
    if name == "ruff":
        return tool_ruff()
    if name in ("prettier", "pnpm"):
        return tool_npm(name)
    lock = load_lock()
    if name not in lock["tools"]:
        from toolchain_local import local_tool

        return local_tool(name)
    system, arch = host()
    _, _, spec = lock_platform(name, lock=lock)
    archive = fetch(name, name + "-" + Path(spec["urls"][0]).name)
    filename = name + (".exe" if system == "windows" else "")
    if spec["kind"] == "zip":
        with zipfile.ZipFile(archive) as source:
            member = next(n for n in source.namelist() if Path(n).name == filename)
            data = source.read(member)
    elif spec["kind"] == "tar.gz":
        with tarfile.open(archive) as source:
            member = next(
                m for m in source.getmembers() if Path(m.name).name == filename
            )
            data = source.extractfile(member).read()
    else:
        data = archive.read_bytes()
    # Keyed by the pin, so checkouts on other pins never swap the binary under
    # a running check (same pattern as the mod's old toolchain).
    key = (
        spec["sha256"][:16]
        if "sha256" in spec
        else hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
    )
    binary = cache_dir() / "bin" / key / filename
    if not binary.is_file() or binary.read_bytes() != data:
        replace(binary, data, mode=0o755)
    binary.chmod(0o755)
    publish_link(binary)
    return binary


def canonical_python():
    if sys.version_info < (3, 12):
        raise RuntimeError("Install native Python 3.12 or later to run project tools")
    if sys.version_info[:3] == (3, 12, 14) and (
        zlib.ZLIB_VERSION,
        zlib.ZLIB_RUNTIME_VERSION,
    ) == ("1.3.2", "1.3.2"):
        return Path(sys.executable)
    key = "-".join(host())
    version = lock_tool("python")["version"].split("+")[0]
    directory = cache_dir() / ("python-" + version + "-" + key)
    relative = Path("python") / ("python.exe" if os.name == "nt" else "bin/python3")
    python = directory / relative
    if not python.is_file():
        archive = fetch("python", "python-" + version + "-" + key + ".tar.gz")
        with (
            progress("Unpacking and verifying Python runtime"),
            tempfile.TemporaryDirectory(dir=cache_dir()) as temporary,
        ):
            staging = Path(temporary) / "install"
            staging.mkdir()
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
            probed = python_version(staging / relative)
            if probed != CANONICAL_PYTHON_PROBE:
                raise RuntimeError(f"Canonical Python compressor mismatch: {probed}")
            try:
                staging.rename(directory)
            except OSError:
                # Another process installed it first: use the winner when it
                # is a valid install (same checks as the cache hit), else raise.
                if (
                    not python.is_file()
                    or python_version(python) != CANONICAL_PYTHON_PROBE
                ):
                    raise
    probed = python_version(python)
    if probed != CANONICAL_PYTHON_PROBE:
        raise RuntimeError(f"Canonical Python compressor mismatch: {probed}")
    return python


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


def prefetch(names):
    """Download named tools into the shared cache for CI: one explicit list,
    no lazy downloads inside later steps."""
    for name in names:
        tool(name)


def main(argv):
    wanted = None
    for index, arg in enumerate(argv):
        if arg == "--tools" and index + 1 < len(argv):
            wanted = argv[index + 1]
        elif arg.startswith("--tools="):
            wanted = arg.partition("=")[2]
    # Only a leading flag selects prefetch; `toolchain.py script.py --ci` must run the script.
    if argv and (argv[0] in ("--ci", "--tools") or argv[0].startswith("--tools=")):
        names = [name for name in (wanted or "").split(",") if name]
        if "--ci" in argv and not names:
            names = ["just", "shellcheck"]
        prefetch(names)
        if "--ci" in argv:
            python = canonical_python()
            if "GITHUB_PATH" in os.environ:
                with Path(os.environ["GITHUB_PATH"]).open(
                    "a", encoding="utf-8", newline="\n"
                ) as output:
                    output.write(
                        str(cache_dir() / "bin") + "\n" + str(python.parent) + "\n"
                    )
    else:
        call(canonical_python(), "-I", "-X", "utf8", *argv)


if __name__ == "__main__":
    main(sys.argv[1:])


try:
    from toolchain_local import (  # noqa: F401
        JAVA_ASSETS,
        JAVA_VERSION,
        check_java,
        deb_libraries,
        deb_payload,
        java_home,
    )
except ImportError:
    # Standalone core without the workspace module (other repos vendor their
    # own repo-specific module next to their copy of the core).
    JAVA_VERSION = None
    JAVA_ASSETS = {}

    def check_java(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")

    def deb_payload(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")

    def deb_libraries(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")

    def java_home(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")
