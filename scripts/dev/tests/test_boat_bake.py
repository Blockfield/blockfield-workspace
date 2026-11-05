"""Golden bake scripts: public origin, launcher deps, pack-host gate."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boat_bake  # noqa: E402


class CheckoutOrigin(unittest.TestCase):
    def test_repoints_stale_private_origin(self):
        # Golden VMs predate the public migration: without the set-url the
        # bake checks out the archived private main (client pin 7ad39817,
        # unfetchable from the public client repo -> FALLBACK-MAIN).
        script = boat_bake.checkout_script("/r")
        self.assertIn(
            "git remote set-url origin "
            "https://github.com/Blockfield/blockfield-workspace.git",
            script,
        )
        self.assertNotIn("workspace-private", script)


class LauncherDeps(unittest.TestCase):
    def test_covers_brief_addendum(self):
        # boat-deps-launcher.txt: mingw-w64 + rustup target, Tauri/WebKitGTK
        # libs, Xvfb + fonts. The trailing verifies prove each tool present.
        script = boat_bake.launcher_deps_script()
        for token in (
            "mingw-w64",
            "x86_64-pc-windows-gnu",
            "libwebkit2gtk-4.1-dev",
            "fonts-dejavu-core",
            "patchelf",
            "cargo clippy --version",
            "x86_64-w64-mingw32-gcc",
            "pkg-config --modversion webkit2gtk-4.1",
        ):
            self.assertIn(token, script)
        self.assertIn("Xvfb", script)


class PackHostGate(unittest.TestCase):
    def test_http_error_counts_as_up(self):
        self.assertTrue(boat_bake.host_reachable("PACK-HOST-UP\n"))

    def test_down_skips_warm(self):
        self.assertFalse(boat_bake.host_reachable("PACK-HOST-DOWN URLError no route\n"))
        self.assertFalse(boat_bake.host_reachable(""))

    def test_probe_is_bounded(self):
        # Client PR #3 pattern: 20 s timeout, UP/DOWN verdict markers.
        script = boat_bake.probe_script()
        self.assertIn("timeout=20", script)
        self.assertIn("PACK-HOST-UP", script)
        self.assertIn("PACK-HOST-DOWN", script)

    def test_probe_body_compiles(self):
        body = boat_bake.probe_script().split("\n", 1)[1].rsplit("\n", 1)[0]
        compile(body, "probe", "exec")


if __name__ == "__main__":
    unittest.main()
