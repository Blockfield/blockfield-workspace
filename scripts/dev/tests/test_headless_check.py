"""Headless probe ownership and portable archive validation without applications."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

DEV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEV))
import headless_check as check  # noqa: E402


class ProbeFiles(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bf probe fixtures ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / "owned stand"
        self.home.mkdir()
        self.owner = "probe-unit-session"
        self.records = {}
        self.busy = set()
        for name, value in (
            ("ROOT", self.root),
            ("HOME", self.home),
            ("PID_FILE", self.home / "server.pid"),
            ("CLIENT_PID_FILE", self.home / "client.pid"),
            ("CLIENT_CMD_FILE", self.home / "client-command.json"),
            ("process_owner", Mock(return_value=self.owner)),
            ("process_record", Mock(side_effect=lambda path: self.records.get(path))),
            ("port_open", Mock(side_effect=lambda port: port in self.busy)),
        ):
            setting = patch.object(check.devstack, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        environment = patch.dict(os.environ, {"BF_DEV_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def rejected_claim(self):
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            check.claim_stack()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.home / "headless-probe-owner.json").exists())


class StackOwnership(ProbeFiles):
    @unittest.skipIf(os.name == "nt", "creating symlinks requires Windows privileges")
    def test_reused_probe_rejects_linked_game_destinations_before_any_writes(self):
        check.claim_stack()
        foreign = self.root / "foreign client"
        foreign.mkdir()
        kept = foreign / "keep.txt"
        kept.write_bytes(b"foreign client data")
        for relative in (
            "play",
            "play/mods",
            "play/options.txt",
            "play/mods/bftestbot.jar",
            "play/mods/" + check.FABRIC_API[0],
        ):
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            directory = relative in ("play", "play/mods")
            path.symlink_to(
                foreign if directory else kept, target_is_directory=directory
            )
            before = self.snapshot()
            with (
                self.subTest(relative=relative),
                patch.object(check, "ffmpeg") as encoder,
                patch.object(check, "download") as download,
                patch.object(check.devstack, "start_process") as start,
                patch.object(check.devstack, "stop_process") as stop,
                self.assertRaisesRegex(RuntimeError, "symlink"),
            ):
                check.run(SimpleNamespace())
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(kept.read_bytes(), b"foreign client data")
            for operation in (encoder, download, start, stop):
                operation.assert_not_called()
            path.unlink()

    def test_probe_requires_explicit_matching_nondefault_home_before_inspection(self):
        for selected in ("", str(self.root / "different")):
            with (
                self.subTest(selected=selected),
                patch.dict(os.environ, {"BF_DEV_HOME": selected}),
            ):
                self.rejected_claim()
        with (
            patch.object(check.devstack, "HOME", self.root / ".dev"),
            patch.dict(os.environ, {"BF_DEV_HOME": str(self.root / ".dev")}),
            self.assertRaises(RuntimeError),
        ):
            check.claim_stack()
        check.devstack.process_record.assert_not_called()
        check.devstack.port_open.assert_not_called()
        self.assertFalse((self.root / ".dev").exists())

    def test_live_or_foreign_records_are_refused_before_any_stack_mutation(self):
        for path in (
            check.devstack.PID_FILE,
            check.devstack.CLIENT_PID_FILE,
            self.home / "display.pid",
        ):
            for record in (
                {"owner": self.owner, "live": True},
                {"owner": "another-session", "live": False},
                {"live": False},
            ):
                with self.subTest(path=path, record=record):
                    self.records = {path: record}
                    self.rejected_claim()
        check.devstack.port_open.assert_not_called()

    def test_busy_ports_prevent_claim_and_run_before_encoder_download_or_cleanup(self):
        for port in (
            check.devstack.PORT,
            check.devstack.RCON_PORT,
            *check.devstack.BOT_PORTS.values(),
        ):
            with self.subTest(port=port):
                self.busy = {port}
                self.rejected_claim()
        with (
            patch.object(check, "ffmpeg") as encoder,
            patch.object(check, "download") as download,
            patch.object(check.devstack, "start_process") as start,
            patch.object(check.devstack, "stop_process") as stop,
            self.assertRaises(RuntimeError),
        ):
            check.run(SimpleNamespace())
        for operation in (encoder, download, start, stop):
            operation.assert_not_called()

    def test_unmarked_existing_game_directories_or_job_are_never_adopted(self):
        for relative in ("play", "server", "client-command.json", "current-job"):
            path = self.home / relative
            with self.subTest(relative=relative):
                if relative in ("play", "server"):
                    path.mkdir()
                else:
                    path.write_text("user-owned", encoding="utf-8")
                self.rejected_claim()
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_owned_dead_records_allow_claim_and_receipt_preserves_session(self):
        self.records = {
            check.devstack.CLIENT_PID_FILE: {"owner": self.owner, "live": False}
        }
        self.assertEqual(check.claim_stack(), (self.home, self.owner))
        marker = self.home / "headless-probe-owner.json"
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8")),
            {"owner": self.owner, "minecraft": "1.21.1", "fabric": "0.19.3"},
        )
        (self.home / "play").mkdir()
        before = marker.read_bytes()
        self.assertEqual(check.claim_stack(), (self.home, self.owner))
        self.assertEqual(marker.read_bytes(), before)
        marker.write_text(json.dumps({"owner": "other"}), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            check.claim_stack()
        self.assertEqual(self.snapshot(), before)


class BackendProof(ProbeFiles):
    def setUp(self):
        super().setUp()
        self.client = {
            "pid": 123,
            "native": "linux",
            "start_ticks": 17,
            "owner": self.owner,
            "live": True,
            "backend": {"station": "Blockfield-unit", "desktop": "Blockfield-desk"},
        }
        self.display = {
            "pid": 456,
            "native": "linux",
            "start_ticks": 23,
            "owner": self.owner,
            "live": True,
        }
        self.records = {
            check.devstack.CLIENT_PID_FILE: self.client,
            self.home / "display.pid": self.display,
        }
        (self.home / "display.number").write_text("87\n", encoding="utf-8")
        (self.home / "display.auth").write_bytes(b"isolated fixture authority")

    def backend(self, platform="linux", **values):
        return {
            "pid": 123,
            "platform": platform,
            "silent": True,
            "renderThread": "Render thread",
            "version": "4.5 (Core Profile) Mesa fixture",
            "glsl": "4.50",
            "renderer": "llvmpipe (fixture)",
            "display": ":87",
            "wayland": "",
            "authority": str(self.home / "display.auth"),
            "verified": True,
            "visible": False,
            "owner": self.owner,
            "station": "Blockfield-unit",
            "desktop": "Blockfield-desk",
            **values,
        }

    def verify(self, backend=None, platform="linux"):
        with patch.object(check.sys, "platform", platform):
            return check.verify_backend({"backend": backend or self.backend(platform)})

    def test_linux_backend_returns_only_verified_registered_native_identities(self):
        proof = self.verify()
        self.assertEqual(proof["client"]["pid"], 123)
        self.assertEqual(proof["display"]["pid"], 456)
        self.assertNotIn("live", proof["client"])
        self.assertNotIn("backend", proof["client"])
        self.assertEqual(
            self.verify(self.backend(version="3.2 Mesa"))["backend"]["version"],
            "3.2 Mesa",
        )

    def test_missing_foreign_or_legacy_native_client_records_cannot_issue_proof(self):
        for changes in (
            {"live": False},
            {"owner": "another-session"},
            {"native": None},
            {"native": "windows"},
            {"pid": True},
            {"pid": 0},
        ):
            with self.subTest(changes=changes):
                self.records[check.devstack.CLIENT_PID_FILE] = {
                    **self.client,
                    **changes,
                }
                with self.assertRaises(RuntimeError):
                    self.verify()
        self.records.clear()
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_wrong_native_pid_silent_flag_or_insufficient_render_proof_is_rejected(
        self,
    ):
        for changes in (
            {"pid": 124},
            {"platform": "win32"},
            {"silent": False},
            {"silent": 1},
            {"renderThread": "main"},
            {"version": "3.1 Mesa"},
            {"version": "unknown"},
            {"glsl": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.verify(self.backend(**changes))

    def test_linux_backend_refuses_wrong_display_authority_or_wayland(self):
        for changes in (
            {"display": ":88"},
            {"authority": str(self.root / "user.auth")},
            {"wayland": "wayland-0"},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.verify(self.backend(**changes))
        for changes in (
            {"owner": "other"},
            {"live": False},
            {"native": None},
            {"pid": True},
        ):
            with self.subTest(display=changes):
                self.records[self.home / "display.pid"] = {**self.display, **changes}
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_windows_requires_exact_assigned_noninteractive_software_backend(self):
        self.client["native"] = "windows"
        self.client["birth"] = 37
        proof = self.verify(platform="win32")
        self.assertEqual(proof["client"]["native"], "windows")
        self.assertNotIn("display", proof)
        for changes in (
            {"station": "WinSta0"},
            {"desktop": "Default"},
            {"visible": True},
            {"visible": 0},
            {"verified": False},
            {"verified": 1},
            {"owner": "other"},
            {"renderer": "hardware OpenGL"},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.verify(self.backend("win32", **changes), "win32")
        self.client["backend"] = {}
        with self.assertRaises(RuntimeError):
            self.verify(self.backend("win32", station=None, desktop=None), "win32")

    def test_menu_readiness_uses_semantic_screen_kind_and_resets_deadline(self):
        state = {
            "screen": "class_442",
            "screenKind": "TitleScreen",
            "connected": False,
            "loadingOverlay": False,
        }
        self.busy = {check.devstack.BOT_PORTS[1]}
        before = check.game.DEADLINE.get()
        with patch.object(check, "tool_value", return_value=state) as tool:
            self.assertEqual(check.wait_menu(), state)
        tool.assert_called_once_with("bf_state", {"client": 1})
        self.assertEqual(check.game.DEADLINE.get(), before)

    def test_menu_waits_for_loading_overlay_to_be_explicitly_gone(self):
        state = {"screen": "class_442", "screenKind": "TitleScreen", "connected": False}
        samples = [
            state,
            {**state, "loadingOverlay": True},
            {**state, "loadingOverlay": 0},
            {**state, "loadingOverlay": False},
        ]
        self.busy = {check.devstack.BOT_PORTS[1]}
        before = check.game.DEADLINE.get()
        with (
            patch.object(check, "tool_value", side_effect=samples) as tool,
            patch.object(check.time, "sleep") as sleep,
        ):
            self.assertEqual(check.wait_menu(), samples[-1])
        self.assertEqual(tool.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(check.game.DEADLINE.get(), before)


class PartialEvidence(ProbeFiles):
    def test_failed_escape_or_encoder_keeps_saved_paths_in_persisted_probe_report(self):
        job = self.root / "game job"
        saved = job / "evidence" / "menu.png"
        manifest = job / "evidence" / "capture" / "capture.json"
        manifest.parent.mkdir(parents=True)
        saved.write_bytes(b"saved screenshot fixture")
        manifest.write_text('{"complete": true}\n', encoding="utf-8")
        cached_mod = self.root / "cached fabric.jar"
        cached_mod.write_bytes(b"pinned fixture dependency")
        capture = {"manifest": str(manifest), "complete": True, "frames": 1}
        before = {"screenKind": "OptionsScreen", "loadingOverlay": False}
        original_screenshot = check.game.TOOLS["bf_screenshot"]

        def select_job(_args):
            (self.home / "current-job").write_text(str(job), encoding="utf-8")

        def start(*_args, **_kwargs):
            self.records[check.devstack.CLIENT_PID_FILE] = {
                "owner": self.owner,
                "pid": 123,
                "live": True,
                "native": "linux",
            }
            return 123, None

        def stop(path):
            self.records.pop(path, None)

        for failure in ("escape", "encoder"):
            launch = Mock(return_value=["java.exe", "fixture"])
            after = {
                "screenKind": "OptionsScreen" if failure == "escape" else "TitleScreen",
                "loadingOverlay": False,
            }
            with (
                self.subTest(failure=failure),
                patch.object(
                    check, "ffmpeg", return_value=self.root / "ffmpeg fixture"
                ),
                patch.object(check, "encoder_info", return_value={}),
                patch.object(check, "download", return_value=cached_mod),
                patch.multiple(
                    check.devstack,
                    java_exe=Mock(),
                    install_testbot=Mock(),
                    patch_client_settings=Mock(),
                    patch_headless_options=Mock(),
                    client_launch_command=launch,
                    game_job=Mock(side_effect=select_job),
                    client_environment=Mock(return_value={}),
                    start_process=Mock(side_effect=start),
                    wait_client_ready=Mock(),
                    stop_process=Mock(side_effect=stop),
                ),
                patch.object(check, "wait_menu", return_value={}),
                patch.object(
                    check, "verify_backend", return_value={"client": {"pid": 123}}
                ),
                patch.object(check.game, "job", return_value=(job, {})),
                patch.object(check.game, "bot"),
                patch.dict(
                    check.game.TOOLS,
                    {
                        "bf_screenshot": (
                            Mock(
                                return_value={
                                    "content": [
                                        {"type": "text", "text": f"saved {saved}"}
                                    ]
                                }
                            ),
                            *original_screenshot[1:],
                        )
                    },
                ),
                patch.object(check, "tool_value", side_effect=[before, capture, after]),
                patch.object(
                    check, "encode_capture", side_effect=RuntimeError("encoder failed")
                ) as encode,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(
                    RuntimeError,
                    "synthetic Escape" if failure == "escape" else "encoder failed",
                ),
            ):
                check.run(
                    SimpleNamespace(xmx="2G")
                    if failure == "escape"
                    else SimpleNamespace()
                )
            self.assertEqual(
                launch.call_args.args[0].xmx, "2G" if failure == "escape" else "4G"
            )
            proof = json.loads(
                (self.home / "headless-proof.json").read_text(encoding="utf-8")
            )
            evidence = proof["runs"][0]["evidence"]
            self.assertFalse(proof["complete"])
            self.assertEqual(evidence["screenshot"], str(saved))
            self.assertEqual(evidence["capture"]["manifest"], str(manifest))
            self.assertEqual(
                evidence["input"],
                {
                    "before": "OptionsScreen",
                    "after": after["screenKind"],
                    "key": "Escape",
                },
            )
            self.assertNotIn("video", evidence)
            self.assertTrue(Path(evidence["capture"]["manifest"]).is_file())
            self.assertEqual(encode.call_count, 0 if failure == "escape" else 1)


class ReleasedArchive(ProbeFiles):
    def setUp(self):
        super().setUp()
        self.game_dir = self.home / "play"
        (self.game_dir / "mods").mkdir(parents=True)
        (self.game_dir / "mods" / "old.jar").write_bytes(b"old own mod")
        (self.game_dir / "config").mkdir()
        (self.game_dir / "config" / "user.cfg").write_bytes(b"preserve config")
        self.archive = self.root / "released client.zip"

    def archive_with(self, *entries):
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("mods/new.jar", b"released mod")
            for name, content in entries:
                if isinstance(name, str):
                    entry = zipfile.ZipInfo(name)
                    entry.filename = name
                    entry.orig_filename = name
                else:
                    entry = name
                archive.writestr(entry, content)
        return self.archive

    def rejected(self):
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            check.extract_released_pack(self.archive, self.game_dir)
        self.assertEqual(self.snapshot(), before)

    def test_valid_pack_replaces_owned_mods_and_keeps_unrelated_game_files(self):
        archive = self.archive_with(
            ("config/", b""),
            ("config/released.cfg", b"released config"),
            ("resourcepacks/base.zip", b"released resource"),
            ("mods/second.jar", b"second mod"),
        )
        self.assertEqual(
            check.extract_released_pack(archive, self.game_dir), {"mods": 2, "files": 4}
        )
        self.assertFalse((self.game_dir / "mods" / "old.jar").exists())
        self.assertEqual(
            (self.game_dir / "mods" / "new.jar").read_bytes(), b"released mod"
        )
        self.assertEqual(
            (self.game_dir / "config" / "user.cfg").read_bytes(), b"preserve config"
        )

    def test_all_entry_paths_are_validated_before_existing_mods_are_removed(self):
        for name in (
            "../outside.txt",
            "/outside.txt",
            "config/../../outside.txt",
            "C:/outside.txt",
            "C:outside.txt",
            "\\\\server\\share\\file",
            "config\\outside.txt",
            "config/./file.txt",
            "config//file.txt",
            "config/CON",
            "config/trailing.",
            "config/trailing ",
            "config/bad?.txt",
        ):
            with self.subTest(name=name):
                self.archive_with((name, b"untrusted"))
                self.assertEqual(
                    self.archive.read_bytes().count(name.encode("ascii")), 2
                )
                self.rejected()

    def test_truncated_nul_names_and_casefold_duplicate_paths_are_rejected(self):
        self.archive_with(("config/safe_name.txt", b"untrusted"))
        data = self.archive.read_bytes()
        self.assertEqual(data.count(b"config/safe_name.txt"), 2)
        self.archive.write_bytes(
            data.replace(b"config/safe_name.txt", b"config/safe\x00name.txt")
        )
        self.rejected()
        self.archive_with(("MODS/New.jar", b"duplicate portable path"))
        self.rejected()

    def test_symlink_and_special_file_entries_are_rejected_before_mutation(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK):
            entry = zipfile.ZipInfo("config/special")
            entry.create_system = 3
            entry.external_attr = (mode | 0o600) << 16
            with self.subTest(mode=mode):
                self.archive_with((entry, b"../outside"))
                self.rejected()

    @unittest.skipIf(os.name == "nt", "creating symlinks requires Windows privileges")
    def test_existing_symlink_file_or_directory_cannot_receive_archive_writes(self):
        outside = self.root / "foreign"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"foreign owned")
        for relative, target in (
            ("config/link", outside),
            ("config/link", outside / "keep.txt"),
        ):
            link = self.game_dir / relative
            link.symlink_to(target, target_is_directory=target.is_dir())
            with self.subTest(target=target):
                self.archive_with(
                    (relative + ("/new.txt" if target.is_dir() else ""), b"untrusted")
                )
                self.rejected()
                self.assertEqual((outside / "keep.txt").read_bytes(), b"foreign owned")
                self.assertFalse((outside / "new.txt").exists())
            link.unlink()


if __name__ == "__main__":
    unittest.main()
