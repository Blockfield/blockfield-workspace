"""Portable encoder/renderer contracts without network, games or GUI processes."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "dev"))
import media  # noqa: E402


class MediaFiles(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bf media fixtures ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.cache = self.root / "cache"
        cache = patch.object(media, "CACHE", self.cache)
        cache.start()
        self.addCleanup(cache.stop)

    def wheel(self, members):
        path = self.root / "encoder.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return path


class EncoderCache(MediaFiles):
    def test_encoder_uses_the_platform_pin_and_repairs_extracted_cache_corruption(self):
        payload = b"pinned native encoder fixture"
        archive = self.wheel({"imageio_ffmpeg/binaries/ffmpeg-fixture": payload})
        for platform in (
            ("linux", "amd64"),
            ("linux", "arm64"),
            ("windows", "amd64"),
            ("windows", "arm64"),
        ):
            with (
                self.subTest(platform=platform),
                patch.object(media, "host", return_value=platform),
                patch.object(media, "download", return_value=archive) as download,
            ):
                binary = media.ffmpeg()
                key = "-".join(
                    (platform[0], "amd64" if platform[0] == "windows" else platform[1])
                )
                self.assertEqual(binary.parent, self.cache / "media" / key)
                self.assertEqual(
                    binary.name, "ffmpeg.exe" if platform[0] == "windows" else "ffmpeg"
                )
                self.assertEqual(binary.read_bytes(), payload)
                pin = media.FFMPEG_ASSETS[key]
                download.assert_called_with(Path(pin[0]).name, *pin)
                binary.write_bytes(b"corrupted cache")
                self.assertEqual(media.ffmpeg().read_bytes(), payload)

    def test_windows_without_avicap32_gets_the_stub_next_to_the_encoder(self):
        import struct

        archive = self.wheel({"imageio_ffmpeg/binaries/ffmpeg-fixture": b"exe"})
        windows = self.root / "Windows"
        with (
            patch.object(media, "host", return_value=("windows", "amd64")),
            patch.object(media, "download", return_value=archive),
            patch.dict(os.environ, {"SystemRoot": str(windows)}),
        ):
            stub = media.ffmpeg().parent / "avicap32.dll"
            self.assertEqual(stub.read_bytes(), media.AVICAP32_STUB)
            (windows / "System32").mkdir(parents=True)
            (windows / "System32" / "avicap32.dll").write_bytes(b"real")
            media.ffmpeg()
            self.assertFalse(stub.exists())
        dll = media.AVICAP32_STUB
        pe = struct.unpack_from("<I", dll, 0x3C)[0]
        self.assertEqual(dll[pe : pe + 4], b"PE\0\0")
        self.assertEqual(struct.unpack_from("<H", dll, pe + 4)[0], 0x8664)
        rva = struct.unpack_from("<I", dll, pe + 24 + 112)[0]

        def at(value):
            return value - 0x1000 + 0x200

        _, _, count, named, functions, names, _ = struct.unpack_from(
            "<IIIIIII", dll, at(rva) + 12
        )
        exported = [
            dll[start : dll.index(b"\0", start)]
            for start in (
                at(struct.unpack_from("<I", dll, at(names) + 4 * i)[0])
                for i in range(named)
            )
        ]
        self.assertEqual(
            exported, [b"capCreateCaptureWindowA", b"capGetDriverDescriptionA"]
        )
        # Both return 0 ("no capture device").
        code = at(struct.unpack_from("<I", dll, at(functions))[0])
        self.assertEqual(dll[code : code + 3], b"\x31\xc0\xc3")

    def test_malformed_pinned_wheels_fail_before_an_encoder_is_written(self):
        for members in (
            {"other.txt": b"not an encoder"},
            {
                "imageio_ffmpeg/binaries/ffmpeg-a": b"a",
                "imageio_ffmpeg/binaries/ffmpeg-b": b"b",
            },
        ):
            with (
                self.subTest(members=members),
                patch.object(media, "host", return_value=("linux", "amd64")),
                patch.object(media, "download", return_value=self.wheel(members)),
                self.assertRaisesRegex(RuntimeError, "one encoder"),
            ):
                media.ffmpeg()
        self.assertFalse((self.cache / "media").exists())

    def test_codec_preflight_requires_exact_libx264_and_reports_the_binary_digest(self):
        binary = self.root / "ffmpeg fixture"
        binary.write_bytes(b"fixture encoder")
        for encoders in (" V..... libx264rgb RGB H.264\n", " V..... mpeg4 MPEG-4\n"):
            with (
                self.subTest(encoders=encoders),
                patch.object(
                    media.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout=encoders, stderr=""
                    ),
                ) as run,
                self.assertRaisesRegex(RuntimeError, "libx264"),
            ):
                media.encoder_info(binary)
            self.assertEqual(run.call_count, 1)
        with patch.object(
            media.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess(
                    [], 0, stdout=" V....D libx264 H.264\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout="ffmpeg fixture version\nconfiguration: fixture\n",
                    stderr="",
                ),
            ],
        ) as run:
            details = media.encoder_info(binary)
        self.assertEqual(details["version"], "ffmpeg fixture version")
        self.assertEqual(
            details["sha256"], hashlib.sha256(binary.read_bytes()).hexdigest()
        )
        self.assertEqual(
            [call.kwargs["timeout"] for call in run.call_args_list], [15, 15]
        )


class CaptureEncoding(MediaFiles):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "job evidence"
        self.folder.mkdir()
        self.binary = self.root / "ffmpeg.exe"
        self.binary.write_bytes(b"fixture encoder")
        self.manifest_path = self.folder / "capture.json"
        self.manifest = {
            "version": 1,
            "complete": True,
            "requested_seconds": 1,
            "recording_seconds": 1,
            "frames": [
                {"file": f"frame-{index:04d}.png", "at_seconds": at, "saved": True}
                for index, at in enumerate((0, 0.25, 0.9))
            ],
        }
        for frame in self.manifest["frames"]:
            (self.folder / frame["file"]).write_bytes(b"fixture PNG")
        self.write_manifest()

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def rejected(self):
        self.write_manifest()
        with (
            patch.object(media, "ffmpeg") as resolve,
            patch.object(media, "encoder_info") as info,
            patch.object(media.subprocess, "run") as run,
            self.assertRaises((ValueError, RuntimeError)),
        ):
            media.encode_capture(self.manifest_path)
        resolve.assert_not_called()
        info.assert_not_called()
        run.assert_not_called()
        self.assertFalse((self.folder / "frames.txt").exists())
        self.assertFalse((self.folder / "capture.mp4").exists())

    def test_timestamped_frames_drive_video_timing_and_receipt_without_global_encoder(
        self,
    ):
        inspected = {}

        def encode(command, **kwargs):
            playlist = Path(command[command.index("-i") + 1])
            inspected["playlist"] = playlist.read_text(encoding="utf-8")
            inspected["command"] = command
            inspected["kwargs"] = kwargs
            Path(command[-1]).write_bytes(b"fixture MP4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        provenance = {"version": "ffmpeg fixture", "sha256": "f" * 64}
        with (
            patch.object(media, "ffmpeg", return_value=self.binary) as resolve,
            patch.object(media, "encoder_info", return_value=provenance) as info,
            patch.object(media.subprocess, "run", side_effect=encode),
        ):
            output = media.encode_capture(self.manifest_path)
        resolve.assert_called_once_with()
        info.assert_called_once_with(self.binary)
        command = inspected["command"]
        self.assertEqual(command[0], str(self.binary))
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-safe") + 1], "1")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertIn("pad=ceil(iw/2)*2:ceil(ih/2)*2", command)
        self.assertEqual(inspected["kwargs"]["cwd"], self.folder)
        self.assertEqual(inspected["kwargs"]["timeout"], 90)
        self.assertEqual(output.read_bytes(), b"fixture MP4")
        self.assertEqual(
            inspected["playlist"].splitlines(),
            [
                "file 'frame-0000.png'",
                "duration 0.250000",
                "file 'frame-0001.png'",
                "duration 0.650000",
                "file 'frame-0002.png'",
                "duration 0.100000",
                "file 'frame-0002.png'",
            ],
        )
        receipt = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            receipt["video"],
            {
                "file": "capture.mp4",
                "codec": "libx264",
                "start_offset_seconds": 0,
                "end_offset_seconds": 1,
                **provenance,
            },
        )
        self.assertEqual(receipt["frames"], self.manifest["frames"])
        self.assertFalse((self.folder / "frames.txt").exists())

    def test_video_zero_maps_to_first_frame_request_in_the_capture_timeline(self):
        for frame, at in zip(self.manifest["frames"], (0.4, 0.65, 0.9), strict=True):
            frame["at_seconds"] = at
        self.write_manifest()
        commands = []

        def encode(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"fixture MP4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (
            patch.object(media, "encoder_info", return_value={}),
            patch.object(media.subprocess, "run", side_effect=encode),
        ):
            media.encode_capture(self.manifest_path, self.binary)
        receipt = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["video"]["start_offset_seconds"], 0.4)
        self.assertEqual(receipt["video"]["end_offset_seconds"], 1)
        self.assertAlmostEqual(float(commands[0][commands[0].index("-t") + 1]), 0.6)
        self.assertEqual(receipt["frames"], self.manifest["frames"])

    def test_invalid_completion_and_timestamps_fail_before_encoder_setup(self):
        original = json.dumps(self.manifest)
        for change in (
            {"complete": False},
            {"complete": "true"},
            {"frames": []},
            {"requested_seconds": 0},
            {"requested_seconds": 21},
            {"requested_seconds": float("nan")},
            {"requested_seconds": float("inf")},
            {"requested_seconds": True},
        ):
            with self.subTest(change=change):
                self.manifest = {**json.loads(original), **change}
                self.rejected()
        for field, invalid in (
            ("saved", False),
            ("saved", "true"),
            ("at_seconds", -0.1),
            ("at_seconds", 0),
            ("at_seconds", 1),
            ("at_seconds", float("nan")),
            ("at_seconds", float("inf")),
            ("at_seconds", True),
            ("at_seconds", "0.25"),
        ):
            with self.subTest(field=field, invalid=invalid):
                self.manifest = json.loads(original)
                self.manifest["frames"][1][field] = invalid
                self.rejected()
        self.manifest = json.loads(original)
        self.manifest["frames"] = [
            {"file": "frame-0000.png", "saved": True, "at_seconds": index / 201}
            for index in range(201)
        ]
        self.rejected()

    def test_unsafe_or_missing_frame_names_fail_before_encoder_setup(self):
        for name in (
            "../outside.png",
            "/tmp/outside.png",
            "C:\\outside.png",
            "frame\\outside.png",
            "bad'file.png",
            "bad\nfile.png",
            ".hidden.png",
            "кадр.png",
            "frame.jpg",
        ):
            with self.subTest(name=name):
                if name in (".hidden.png", "кадр.png", "bad'file.png"):
                    (self.folder / name).write_bytes(b"fixture PNG")
                self.manifest["frames"][0]["file"] = name
                self.rejected()
        self.manifest["frames"][0]["file"] = "missing.png"
        self.rejected()

    @unittest.skipIf(
        os.name == "nt", "Windows symlinks may require developer privileges"
    )
    def test_frame_symlinks_cannot_escape_the_evidence_directory(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"foreign image")
        (self.folder / "symlink.png").symlink_to(outside)
        self.manifest["frames"][0]["file"] = "symlink.png"
        self.rejected()

    def test_failed_encoding_cleans_the_playlist_without_publishing_video_provenance(
        self,
    ):
        with (
            patch.object(
                media,
                "encoder_info",
                return_value={"version": "fixture", "sha256": "f" * 64},
            ),
            patch.object(
                media.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired([str(self.binary)], 90),
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            media.encode_capture(self.manifest_path, self.binary)
        self.assertFalse((self.folder / "frames.txt").exists())
        self.assertNotIn(
            "video", json.loads(self.manifest_path.read_text(encoding="utf-8"))
        )

    def test_unavailable_codec_fails_before_any_output_or_encoding_process(self):
        original = self.manifest_path.read_bytes()
        with (
            patch.object(
                media, "encoder_info", side_effect=RuntimeError("libx264 unavailable")
            ),
            patch.object(media.subprocess, "run") as run,
            self.assertRaisesRegex(RuntimeError, "libx264"),
        ):
            media.encode_capture(self.manifest_path, self.binary)
        run.assert_not_called()
        self.assertEqual(self.manifest_path.read_bytes(), original)
        self.assertFalse((self.folder / "frames.txt").exists())
        self.assertFalse((self.folder / "capture.mp4").exists())


class WindowsRenderer(MediaFiles):
    DLLS = {
        "opengl32.dll": b"fixture Mesa WGL",
        "libgallium_wgl.dll": b"fixture Mesa llvmpipe",
    }

    def setUp(self):
        super().setUp()
        self.jdk = self.root / "jdk" / "temurin-fixture"
        self.bin = self.jdk / "bin"
        self.bin.mkdir(parents=True)
        (self.bin / "java.exe").write_bytes(b"fixture managed Java")
        (self.bin / "opengl32.dll").write_bytes(b"original JDK library")
        self.directory = self.jdk.parent / (self.jdk.name + "-headless-mesa-26.2.2")
        self.hashes = {
            name: hashlib.sha256(data).hexdigest() for name, data in self.DLLS.items()
        }
        for target, value in (
            ("host", Mock(return_value=("windows", "amd64"))),
            ("MESA_DLL_SHA256", self.hashes),
        ):
            setting = patch.object(media, target, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.archive = self.root / "mesa-fixture.7z"
        self.archive.write_bytes(b"fixture verified archive")

    def renderer_run(self, command, **_kwargs):
        if "-tf" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="x64/\nx64/opengl32.dll\nx64/libgallium_wgl.dll\nx64/ignored.dll\n",
                stderr="",
            )
        staging = Path(command[command.index("-C") + 1])
        (staging / "x64").mkdir()
        for name, data in {**self.DLLS, "ignored.dll": b"unselected DLL"}.items():
            (staging / "x64" / name).write_bytes(data)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def cached(self, receipt=None, java=True):
        (self.directory / "bin").mkdir(parents=True)
        for name, data in self.DLLS.items():
            (self.directory / "bin" / name).write_bytes(data)
        if java:
            (self.directory / "bin" / "java.exe").write_bytes(b"fixture managed Java")
        (self.directory / "mesa.json").write_text(
            json.dumps(
                receipt
                if receipt is not None
                else {"archive_sha256": media.MESA_ASSET[1], "dlls": self.hashes}
            ),
            encoding="utf-8",
        )

    def test_renderer_is_verified_in_a_separate_jdk_cache_and_reused_without_download(
        self,
    ):
        with (
            patch.object(media, "download", return_value=self.archive) as download,
            patch.object(media, "native", return_value="fixture-tar"),
            patch.object(media.subprocess, "run", side_effect=self.renderer_run) as run,
        ):
            java = media.windows_java(self.bin)
            self.assertEqual(media.windows_java(self.bin), java)
        self.assertEqual(java, self.directory / "bin" / "java.exe")
        self.assertEqual(java.read_bytes(), b"fixture managed Java")
        self.assertEqual(
            (self.bin / "opengl32.dll").read_bytes(), b"original JDK library"
        )
        for name, data in self.DLLS.items():
            self.assertEqual((java.parent / name).read_bytes(), data)
        self.assertFalse((java.parent / "ignored.dll").exists())
        download.assert_called_once_with(
            Path(media.MESA_ASSET[0]).name, *media.MESA_ASSET
        )
        self.assertEqual(run.call_count, 2)
        extract = next(
            call.args[0] for call in run.call_args_list if "-xf" in call.args[0]
        )
        self.assertIn("x64/opengl32.dll", extract)
        self.assertIn("x64/libgallium_wgl.dll", extract)
        self.assertEqual(
            json.loads((self.directory / "mesa.json").read_text())["dlls"], self.hashes
        )
        (java.parent / "opengl32.dll").write_bytes(b"modified isolated cache")
        self.assertEqual(
            (self.bin / "opengl32.dll").read_bytes(), b"original JDK library"
        )

    def test_cached_renderer_hashes_cannot_be_replaced_by_a_forged_receipt(self):
        self.cached()
        modified = b"unverified renderer"
        (self.directory / "bin" / "opengl32.dll").write_bytes(modified)
        receipt = {
            "archive_sha256": media.MESA_ASSET[1],
            "dlls": {
                **self.hashes,
                "opengl32.dll": hashlib.sha256(modified).hexdigest(),
            },
        }
        (self.directory / "mesa.json").write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch.object(media, "download") as download,
            self.assertRaisesRegex(RuntimeError, "checksum"),
        ):
            media.windows_java(self.bin)
        download.assert_not_called()

    def test_jdk_clone_falls_back_to_copy_when_windows_links_are_unavailable(self):
        with (
            patch.object(media, "download", return_value=self.archive),
            patch.object(media, "native", return_value="fixture-tar"),
            patch.object(media.subprocess, "run", side_effect=self.renderer_run),
            patch.object(media.os, "link", side_effect=OSError("link not supported")),
        ):
            java = media.windows_java(self.bin)
        self.assertEqual(java.read_bytes(), b"fixture managed Java")
        java.write_bytes(b"modified fallback clone")
        self.assertEqual((self.bin / "java.exe").read_bytes(), b"fixture managed Java")
        self.assertEqual(
            (self.bin / "opengl32.dll").read_bytes(), b"original JDK library"
        )

    def test_cache_requires_java_and_exact_pinned_dll_set(self):
        self.cached(java=False)
        with (
            patch.object(media, "download") as download,
            self.assertRaises(RuntimeError),
        ):
            media.windows_java(self.bin)
        download.assert_not_called()
        (self.directory / "bin" / "java.exe").write_bytes(b"fixture Java")
        for hashes in (
            {},
            {"opengl32.dll": self.hashes["opengl32.dll"]},
            {**self.hashes, "../foreign.dll": "f" * 64},
        ):
            (self.directory / "mesa.json").write_text(
                json.dumps({"archive_sha256": media.MESA_ASSET[1], "dlls": hashes}),
                encoding="utf-8",
            )
            with (
                self.subTest(hashes=hashes),
                patch.object(media, "download") as download,
                self.assertRaises(RuntimeError),
            ):
                media.windows_java(self.bin)
            download.assert_not_called()

    def test_mesa_archive_rejects_windows_and_posix_traversal_before_extraction(self):
        for name in (
            "../outside.dll",
            "/outside.dll",
            "x64\\..\\outside.dll",
            "C:\\outside.dll",
            "\\\\host\\outside.dll",
        ):
            with (
                self.subTest(name=name),
                patch.object(media, "download", return_value=self.archive),
                patch.object(media, "native", return_value="fixture-tar"),
                patch.object(
                    media.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout=name + "\n", stderr=""
                    ),
                ) as run,
                self.assertRaisesRegex(RuntimeError, "Unsafe"),
            ):
                media.windows_java(self.bin)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(self.directory.exists())

    def test_extracted_dll_checksums_are_verified_before_a_cache_is_published(self):
        def corrupted(command, **kwargs):
            result = self.renderer_run(command, **kwargs)
            if "-xf" in command:
                staging = Path(command[command.index("-C") + 1])
                (staging / "x64" / "opengl32.dll").write_bytes(b"unverified WGL")
            return result

        with (
            patch.object(media, "download", return_value=self.archive),
            patch.object(media, "native", return_value="fixture-tar"),
            patch.object(media.subprocess, "run", side_effect=corrupted),
            self.assertRaisesRegex(RuntimeError, "checksum"),
        ):
            media.windows_java(self.bin)
        self.assertFalse(self.directory.exists())
        self.assertEqual(
            (self.bin / "opengl32.dll").read_bytes(), b"original JDK library"
        )

    def test_unverified_platforms_fail_without_download_or_native_commands(self):
        for platform in (("linux", "amd64"), ("windows", "arm64")):
            with (
                self.subTest(platform=platform),
                patch.object(media, "host", return_value=platform),
                patch.object(media, "download") as download,
                patch.object(media.subprocess, "run") as run,
                self.assertRaises(RuntimeError),
            ):
                media.windows_java(self.bin)
            download.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
