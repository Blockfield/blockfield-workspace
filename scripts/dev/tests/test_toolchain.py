import base64
import collections
import hashlib
import http.server
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import toolchain  # noqa: E402 - common helper lives alongside the dev directory.


class Toolchain(unittest.TestCase):
    def test_cached_wrong_checksum_is_rejected_before_download_or_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "tool.tar.gz"
            target.write_bytes(b"corrupted cache")
            with (
                patch.object(toolchain, "CACHE", Path(tmp)),
                patch.object(toolchain.urllib.request, "urlopen") as download,
                patch.object(toolchain, "call") as execute,
            ):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    toolchain.download(
                        "tool.tar.gz", "https://example.invalid/tool", "0" * 64
                    )
                download.assert_not_called()
                execute.assert_not_called()
            self.assertEqual(target.read_bytes(), b"corrupted cache")

    def test_stale_cache_names_its_path_and_publish_is_atomic(self):
        import hashlib

        data = b"pinned tool bytes"
        checksum = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with (
                patch.object(toolchain, "CACHE", cache),
                patch.object(
                    toolchain.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(data),
                ),
            ):
                first = toolchain.download(
                    "tool.tar.gz", "https://example.invalid/tool", checksum
                )
            self.assertEqual(first.read_bytes(), data)
            with (
                patch.object(toolchain, "CACHE", cache),
                patch.object(toolchain.urllib.request, "urlopen") as download,
            ):
                second = toolchain.download(
                    "tool.tar.gz", "https://example.invalid/tool", checksum
                )
                download.assert_not_called()
            self.assertEqual(first, second)
            stale = cache / "tool.tar.gz"
            stale.write_bytes(b"stale bytes")
            with (
                patch.object(toolchain, "CACHE", cache),
                patch.object(toolchain.urllib.request, "urlopen") as download,
            ):
                with self.assertRaisesRegex(RuntimeError, f"delete {stale} and retry"):
                    toolchain.download(
                        "tool.tar.gz", "https://example.invalid/tool", checksum
                    )
                download.assert_not_called()

    def test_interrupted_publish_leaves_no_partial_cache_entry(self):
        import hashlib

        data = b"pinned tool bytes"
        checksum = hashlib.sha256(data).hexdigest()
        real = tempfile.NamedTemporaryFile

        class FailWrite:
            def __init__(self, *args, **kwargs):
                self._file = real(*args, **kwargs)
                self.name = self._file.name

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._file.__exit__(*exc)

            def write(self, data):
                raise OSError(28, "No space left on device")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(toolchain, "CACHE", Path(tmp)),
                patch.object(
                    toolchain.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(data),
                ),
                patch.object(toolchain.tempfile, "NamedTemporaryFile", FailWrite),
            ):
                with self.assertRaises(OSError):
                    toolchain.download(
                        "tool.tar.gz", "https://example.invalid/tool", checksum
                    )
            self.assertFalse((Path(tmp) / "tool.tar.gz").exists())
            self.assertEqual(list(Path(tmp).glob(".dl-*")), [])

    def test_cached_python_requires_exact_version_and_compressor(self):
        with tempfile.TemporaryDirectory() as tmp:
            platform = "-".join(toolchain.host())
            relative = Path("python") / (
                "python.exe" if os.name == "nt" else "bin/python3"
            )
            python = Path(tmp) / ("python-3.12.14-" + platform) / relative
            python.parent.mkdir(parents=True)
            python.touch()
            with (
                patch.object(toolchain, "CACHE", Path(tmp)),
                patch.object(toolchain.sys, "version_info", (3, 14, 0)),
                patch.object(
                    toolchain, "python_version", return_value="(3, 12, 14) 1.3.1 1.3.1"
                ) as probe,
                patch.object(toolchain, "download") as download,
            ):
                with self.assertRaisesRegex(RuntimeError, "compressor mismatch"):
                    toolchain.canonical_python()
                probe.assert_called_once_with(python)
                download.assert_not_called()

    def test_child_environment_preserves_native_sdk_and_isolates_python_and_java(self):
        injected = {
            key: "/invalid"
            for key in (
                "PYTHONHOME",
                "PYTHONPATH",
                "JAVA_TOOL_OPTIONS",
                "_JAVA_OPTIONS",
                "JDK_JAVA_OPTIONS",
                "CLASSPATH",
            )
        }
        with patch.dict(os.environ, {**injected, "JAVA_HOME": "/native/jdk"}):
            env = toolchain.child_env()
        self.assertTrue(set(injected).isdisjoint(env))
        self.assertEqual(env["JAVA_HOME"], "/native/jdk")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")


class ManagedJava(unittest.TestCase):
    def archive(self, system, unsafe=False):
        output = io.BytesIO()
        root = "jdk-" + toolchain.JAVA_VERSION
        extension = ".exe" if system == "windows" else ""
        entries = {
            f"{root}/bin/{name}{extension}": b"fixture"
            for name in ("java", "javac", "jdb")
        }
        if unsafe:
            entries["../outside"] = b"escape"
        if system == "windows":
            with zipfile.ZipFile(output, "w") as archive:
                for name, data in entries.items():
                    archive.writestr(name, data)
        else:
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                for name, data in entries.items():
                    member = tarfile.TarInfo(name)
                    member.mode = 0o755
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
        return output.getvalue()

    def test_first_install_without_host_java_and_cached_reuse_on_both_platforms(self):
        for system in ("linux", "windows"):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as tmp:
                own = Path(tmp).resolve() / "own stack with spaces"
                data = self.archive(system)
                url = "https://example.invalid/jdk" + (
                    ".zip" if system == "windows" else ".tar.gz"
                )
                with (
                    patch.dict(
                        os.environ,
                        {
                            "BF_DEV_HOME": str(own),
                            "JAVA_HOME": "/missing/host/jdk",
                            "PATH": "",
                        },
                    ),
                    patch.object(toolchain, "DEV_CACHE", Path(tmp) / "cache"),
                    patch.object(toolchain, "host", return_value=(system, "amd64")),
                    patch.dict(
                        toolchain.JAVA_ASSETS,
                        {system + "-amd64": [url, hashlib.sha256(data).hexdigest()]},
                    ),
                    patch.object(
                        toolchain.urllib.request,
                        "urlopen",
                        return_value=io.BytesIO(data),
                    ) as fetch,
                    patch.object(
                        toolchain.subprocess,
                        "run",
                        return_value=SimpleNamespace(
                            returncode=0,
                            stderr="OpenJDK Runtime Environment Temurin-"
                            + toolchain.JAVA_VERSION,
                        ),
                    ) as probe,
                ):
                    installed = toolchain.java_home()
                    self.assertEqual(
                        installed,
                        own
                        / "jdk"
                        / (
                            "temurin-"
                            + toolchain.JAVA_VERSION
                            + "-"
                            + system
                            + "-amd64"
                        ),
                    )
                    self.assertEqual(toolchain.java_home(), installed)
                    fetch.assert_called_once_with(url, timeout=60)
                    self.assertEqual(probe.call_count, 2)
                    executable = "java.exe" if system == "windows" else "java"
                    self.assertEqual(
                        probe.call_args.args[0],
                        [str(installed / "bin" / executable), "-version"],
                    )
                    self.assertEqual(
                        (Path(tmp) / "cache/jdk" / Path(url).name).read_bytes(), data
                    )
                    with patch.dict(
                        os.environ, {"BF_DEV_HOME": str(Path(tmp) / "second")}
                    ):
                        second = toolchain.java_home()
                    fetch.assert_called_once_with(url, timeout=60)
                    (second / "bin" / executable).write_bytes(b"stand mutation")
                    self.assertEqual(
                        (installed / "bin" / executable).read_bytes(), b"fixture"
                    )

    def test_default_workspace_home_ignores_external_java_and_cleans_jvm_environment(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            system, arch = toolchain.host()
            extension = ".exe" if system == "windows" else ""
            sdk = (
                root
                / ".dev"
                / "jdk"
                / ("temurin-" + toolchain.JAVA_VERSION + "-" + system + "-" + arch)
            )
            for name in ("java", "javac", "jdb"):
                binary = sdk / "bin" / (name + extension)
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.touch()
                binary.chmod(0o755)
            with (
                patch.dict(
                    os.environ,
                    {
                        "BF_DEV_HOME": "",
                        "JAVA_HOME": "/external/jdk",
                        "PATH": "",
                        "JAVA_TOOL_OPTIONS": "injected",
                    },
                ),
                patch.object(toolchain, "ROOT", root),
                patch.object(
                    toolchain.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=0, stderr="Temurin-" + toolchain.JAVA_VERSION
                    ),
                ),
                patch.object(toolchain.urllib.request, "urlopen") as fetch,
            ):
                for name in ("java", "javac", "jdb"):
                    self.assertEqual(
                        toolchain.native("/external/bin/" + name + extension),
                        str(sdk / "bin" / (name + extension)),
                    )
                self.assertEqual(os.environ["JAVA_HOME"], str(sdk))
                self.assertEqual(
                    Path(os.environ["PATH"].split(os.pathsep)[0]), sdk / "bin"
                )
                self.assertNotIn("JAVA_TOOL_OPTIONS", os.environ)
                fetch.assert_not_called()

    def test_bad_checksum_and_archive_escape_never_publish_or_execute_jdk(self):
        for system, unsafe in (("linux", False), ("linux", True), ("windows", True)):
            with (
                self.subTest(system=system, unsafe=unsafe),
                tempfile.TemporaryDirectory() as tmp,
            ):
                own = Path(tmp) / "own"
                data = self.archive(system, unsafe=unsafe)
                checksum = hashlib.sha256(data).hexdigest() if unsafe else "0" * 64
                url = "https://example.invalid/jdk" + (
                    ".zip" if system == "windows" else ".tar.gz"
                )
                with (
                    patch.dict(os.environ, {"BF_DEV_HOME": str(own)}),
                    patch.object(toolchain, "DEV_CACHE", Path(tmp) / "cache"),
                    patch.object(toolchain, "host", return_value=(system, "amd64")),
                    patch.dict(
                        toolchain.JAVA_ASSETS, {system + "-amd64": [url, checksum]}
                    ),
                    patch.object(
                        toolchain.urllib.request,
                        "urlopen",
                        return_value=io.BytesIO(data),
                    ),
                    patch.object(toolchain.subprocess, "run") as execute,
                ):
                    with self.assertRaises((RuntimeError, tarfile.FilterError)):
                        toolchain.java_home()
                    execute.assert_not_called()
                    self.assertFalse(
                        (
                            own
                            / "jdk"
                            / (
                                "temurin-"
                                + toolchain.JAVA_VERSION
                                + "-"
                                + system
                                + "-amd64"
                            )
                        ).exists()
                    )
                    self.assertFalse((own / "jdk" / "outside").exists())


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits[self.path] += 1
        if self.path == "/flaky" and self.server.hits[self.path] == 1:
            self._send(500, b"flaky first attempt")
        elif self.path in self.server.good or self.path == "/flaky":
            self._send(200, LockDownloader.GOOD)
        elif self.path == "/always500":
            self._send(500, b"boom")
        else:
            self._send(404, b"nope")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LockDownloader(unittest.TestCase):
    """Lock-driven fetch: ordered fallback, retry, checksum gate, null mirror
    skip, BF_TOOL_CACHE. Local HTTP server only, no external network."""

    GOOD = b"pinned tool bytes fixture"
    BAD = b"wrong bytes"

    @classmethod
    def setUpClass(cls):
        cls.sha = hashlib.sha256(cls.GOOD).hexdigest()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.server.hits = collections.Counter()
        cls.server.good = {"/good", "/m/" + cls.sha}
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=10)
        cls.server.server_close()

    def setUp(self):
        self.server.hits.clear()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def write_lock(self, tmp, mirror, urls, digest=None, integrity=False):
        spec = {"urls": urls, "kind": "raw"}
        if integrity:
            raw = hashlib.sha512(self.GOOD).digest()
            spec["integrity"] = "sha512-" + base64.b64encode(raw).decode()
        else:
            spec["sha256"] = digest or self.sha
        lock = {
            "version": 1,
            "mirror": mirror,
            "tools": {"probe": {"version": "0", "platforms": {"any": spec}}},
        }
        path = Path(tmp) / "tools.lock.json"
        path.write_text(json.dumps(lock), encoding="utf-8")
        return path

    def test_ordered_fallback_first_verified_source_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = self.write_lock(
                tmp, None, [self.url("/always500"), self.url("/good")]
            )
            with (
                patch.object(toolchain, "LOCK_PATH", lock),
                patch.dict(os.environ, {"BF_TOOL_CACHE": tmp}),
                patch("time.sleep"),
            ):
                out = toolchain.fetch("probe", "probe.bin")
            self.assertEqual(out.read_bytes(), self.GOOD)
            self.assertEqual(self.server.hits["/always500"], 3)
            self.assertEqual(self.server.hits["/good"], 1)

    def test_checksum_mismatch_rejects_without_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = self.write_lock(tmp, None, [self.url("/good")], digest="0" * 64)
            with (
                patch.object(toolchain, "LOCK_PATH", lock),
                patch.dict(os.environ, {"BF_TOOL_CACHE": tmp}),
            ):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    toolchain.fetch("probe", "probe.bin")
            self.assertFalse((Path(tmp) / "probe.bin").exists())
            self.assertEqual(self.server.hits["/good"], 1)

    def test_retry_recovers_flaky_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = self.write_lock(tmp, None, [self.url("/flaky")])
            with (
                patch.object(toolchain, "LOCK_PATH", lock),
                patch.dict(os.environ, {"BF_TOOL_CACHE": tmp}),
                patch("time.sleep"),
            ):
                out = toolchain.fetch("probe", "probe.bin")
            self.assertEqual(out.read_bytes(), self.GOOD)
            self.assertEqual(self.server.hits["/flaky"], 2)

    def test_null_mirror_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = self.write_lock(tmp, None, [self.url("/good")])
            self.assertIsNone(json.loads(lock.read_text(encoding="utf-8"))["mirror"])
            with (
                patch.object(toolchain, "LOCK_PATH", lock),
                patch.dict(os.environ, {"BF_TOOL_CACHE": tmp}),
            ):
                out = toolchain.fetch("probe", "probe.bin")
            self.assertEqual(out.read_bytes(), self.GOOD)
            self.assertEqual(set(self.server.hits), {"/good"})

    def test_mirror_is_last_resort_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = self.write_lock(
                tmp, self.url("/m"), [self.url("/always500"), self.url("/missing")]
            )
            with (
                patch.object(toolchain, "LOCK_PATH", lock),
                patch.dict(os.environ, {"BF_TOOL_CACHE": tmp}),
                patch("time.sleep"),
            ):
                out = toolchain.fetch("probe", "probe.bin")
            self.assertEqual(out.read_bytes(), self.GOOD)
            self.assertEqual(self.server.hits["/always500"], 3)
            self.assertEqual(self.server.hits["/missing"], 1)
            self.assertEqual(self.server.hits["/m/" + self.sha], 1)

    def test_registry_integrity_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = self.write_lock(tmp, None, [self.url("/good")], integrity=True)
            with (
                patch.object(toolchain, "LOCK_PATH", lock),
                patch.dict(os.environ, {"BF_TOOL_CACHE": tmp}),
            ):
                out = toolchain.fetch("probe", "probe.tgz")
            self.assertEqual(out.read_bytes(), self.GOOD)

    def test_bf_tool_cache_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = self.write_lock(tmp, None, [self.url("/good")])
            with patch.object(toolchain, "LOCK_PATH", lock):
                with patch.dict(
                    os.environ, {"BF_TOOL_CACHE": str(Path(tmp) / "shared")}
                ):
                    out = toolchain.fetch("probe", "shared.bin")
                    self.assertEqual(out, Path(tmp) / "shared" / "shared.bin")
                with patch.object(toolchain, "CACHE", Path(tmp) / "legacy"):
                    env = dict(os.environ)
                    env.pop("BF_TOOL_CACHE", None)
                    with patch.dict(os.environ, env, clear=True):
                        out = toolchain.fetch("probe", "legacy.bin")
                        self.assertEqual(out, Path(tmp) / "legacy" / "legacy.bin")
