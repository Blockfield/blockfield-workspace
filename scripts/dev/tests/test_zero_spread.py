"""Stand-only zero-spread TaCZ test gun: pack validity and stand install."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "devstack.py"
spec = importlib.util.spec_from_file_location("devstack", SCRIPT)
devstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devstack)

PACK = devstack.ZERO_SPREAD_PACK
GUN_ID = "tacz:ak47_zero_spread"
# Shipped default-pack values (owner rig .minecraft/tacz/tacz_default_gun);
# the test gun must match these exactly so measurements stay comparable.
AK_BALLISTICS = {
    "ammo": "tacz:762x39",
    "ammo_amount": 30,
    "rpm": 600,
    "damage": 9,
    "speed": 250,
    "gravity": 0.15,
    "bolt": "closed_bolt",
}


def pack_json(relative):
    return json.loads((PACK / relative).read_text(encoding="utf-8"))


class PackLayout(unittest.TestCase):
    def test_pack_has_no_assets_only_referenced_ak_display(self):
        self.assertTrue((PACK / "gunpack.meta.json").is_file())
        index = pack_json("data/tacz/index/guns/ak47_zero_spread.json")
        self.assertEqual(index["data"], "tacz:ak47_zero_spread_data")
        self.assertEqual(index["display"], "tacz:ak47_display")
        self.assertFalse((PACK / "assets").exists())
        non_json = [
            path
            for path in PACK.rglob("*")
            if path.is_file() and path.suffix != ".json"
        ]
        self.assertEqual(non_json, [])

    def test_index_and_data_ids_agree(self):
        index = pack_json("data/tacz/index/guns/ak47_zero_spread.json")
        data = pack_json("data/tacz/data/guns/ak47_zero_spread_data.json")
        namespace, _, name = index["data"].partition(":")
        self.assertEqual(
            PACK / "data" / namespace / "data" / "guns" / f"{name}.json",
            PACK / "data/tacz/data/guns/ak47_zero_spread_data.json",
        )
        self.assertEqual(data["script"], "tacz:xmag_reload_logic")


class ZeroSpread(unittest.TestCase):
    def test_inaccuracy_is_zero_in_every_state(self):
        data = pack_json("data/tacz/data/guns/ak47_zero_spread_data.json")
        self.assertEqual(
            data["inaccuracy"],
            {"stand": 0, "move": 0, "sneak": 0, "lie": 0, "aim": 0},
        )

    def test_recoil_is_zero(self):
        recoil = pack_json("data/tacz/data/guns/ak47_zero_spread_data.json")["recoil"]
        for curve in ("pitch", "yaw"):
            self.assertTrue(recoil[curve])
            for point in recoil[curve]:
                self.assertEqual(point["value"], [0, 0])

    def test_ballistics_match_standard_ak(self):
        data = pack_json("data/tacz/data/guns/ak47_zero_spread_data.json")
        self.assertEqual(data["ammo"], AK_BALLISTICS["ammo"])
        self.assertEqual(data["ammo_amount"], AK_BALLISTICS["ammo_amount"])
        self.assertEqual(data["rpm"], AK_BALLISTICS["rpm"])
        self.assertEqual(data["bullet"]["damage"], AK_BALLISTICS["damage"])
        self.assertEqual(data["bullet"]["speed"], AK_BALLISTICS["speed"])
        self.assertEqual(data["bullet"]["gravity"], AK_BALLISTICS["gravity"])
        self.assertEqual(data["bolt"], AK_BALLISTICS["bolt"])
        self.assertIn("auto", data["fire_mode"])


class StandInstall(unittest.TestCase):
    def test_install_copies_pack_under_server_tacz_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "server"
            with patch.object(devstack, "SERVER", server):
                devstack.install_zero_spread_pack()
                installed = server / "tacz" / PACK.name
                self.assertTrue(
                    (
                        installed / "data/tacz/data/guns/ak47_zero_spread_data.json"
                    ).is_file()
                )
                self.assertTrue(
                    (installed / "data/tacz/index/guns/ak47_zero_spread.json").is_file()
                )
                self.assertFalse((server / "mods").exists())
                before = {
                    path: path.read_bytes()
                    for path in sorted(installed.rglob("*"))
                    if path.is_file()
                }
                devstack.install_zero_spread_pack()
                after = {
                    path: path.read_bytes()
                    for path in sorted(installed.rglob("*"))
                    if path.is_file()
                }
                self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
