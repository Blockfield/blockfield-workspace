"""Synthetic independent-oracle fixtures; no game processes or network."""

import copy
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
import itertools

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import compare_body_pose as pose  # noqa: E402
from scenarios import body_pose  # noqa: E402

UUID = "00000000-0000-0000-0000-000000000001"
FACES = (
    (0, 2, 3, 1),
    (4, 5, 7, 6),
    (0, 1, 5, 4),
    (2, 6, 7, 3),
    (0, 4, 6, 2),
    (1, 3, 7, 5),
)


def box(center=(0, 0, 0), half=(0.2, 0.3, 0.1), angle=0, mirror=False):
    c, s = math.cos(angle), math.sin(angle)
    return {
        "center": center,
        "half": half,
        "axes": ((c, s, 0), (-s, c, 0), (0, 0, -1 if mirror else 1)),
    }


def emitted(shape):
    corners = pose.corners(shape)
    return {
        "shape": "box",
        "vertices": [list(corners[i]) for face in FACES for i in face],
    }


def native_emitted(shape):
    points = [
        pose.add(
            shape["center"],
            tuple(
                sum(sign[k] * shape["half"][k] * shape["axes"][k][j] for k in range(3))
                for j in range(3)
            ),
        )
        for sign in pose.NATIVE_SIGNS
    ]
    return {
        "shape": "quads",
        "vertices": [list(points[i]) for face in pose.NATIVE_FACES for i in face],
    }


def native_receipt():
    record = observer()
    record["schema"] = "blockfield.playergeometry.v2"
    record["skinModel"] = "wide"
    record["parts"] = {
        p.lower(): {
            "boxes": [
                dict(
                    native_emitted(pose.vanilla_inner_box(p)),
                    id=p.lower(),
                    source="base",
                )
            ]
        }
        for p in pose.PARTS
    }
    return record


def native_dump(receipt=None):
    receipt = receipt or native_receipt()
    record = server()
    record["schema"] = "blockfield.bodypose.v2"
    record["shape"] = "wide/7f/none"
    record["bones"] = [[1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0] for _ in pose.PARTS]
    record["visible"] = {
        p: [
            {
                "id": "skin:inner",
                "local": pose.vanilla_inner_box(p),
                "world": pose.vanilla_inner_box(p),
            }
        ]
        for p in pose.PARTS
    }
    record["bounds"] = [-1, -1, -1, 1, 1, 1]
    return record


def server(tick=100, epoch=0):
    parts = [dict(box((0, i * 0.7, 0)), part=p) for i, p in enumerate(pose.PARTS)]
    return {
        "schema": "blockfield.bodypose.v1",
        "targetUuid": UUID,
        "dimension": "minecraft:overworld",
        "serverTick": tick,
        "gameTime": tick + 900,
        "epoch": epoch,
        "resourceEpoch": 0,
        "samplePoint": "end_server_tick",
        "partialTick": 0,
        "sampleUnixMs": 10000 + (tick - 100) * 50,
        "sampleMonoNs": 500 + (tick - 100) * 50_000_000,
        "feet": [0, 0, 0],
        "fixed": [-0.3, 0, -0.3, 0.3, 1.8, 0.3],
        "bounds": [-0.2, -0.3, -0.1, 0.2, 3.8, 0.1],
        "skinModel": "wide",
        "skinKnown": False,
        "entityScale": 1.0,
        "skinObbs": parts,
        "damageObbs": copy.deepcopy(parts),
        "state": {"animator": "NONE"},
        "diagnostics": [],
    }


def observer(tick=100):
    return {
        "schema": "blockfield.playergeometry.v1",
        "targetUuid": UUID,
        "dimension": "minecraft:overworld",
        "tick": tick + 900,
        "observerTick": tick,
        "observerUuid": UUID,
        "targetTickCount": tick,
        "frameSequence": tick,
        "sampleUnixMs": 10000 + (tick - 100) * 50,
        "sampleMonoNs": 900 + (tick - 100) * 50_000_000,
        "partialTick": 0.5,
        "renderPosition": [0, 0, 0],
        "capturePath": "native_emitted_quads",
        "parts": {
            p.lower(): emitted(box((0, i * 0.7, 0))) for i, p in enumerate(pose.PARTS)
        },
        "_request": {},
    }


def manifest():
    pairs = []
    for i in range(10):
        request = {
            "sendUnixMs": 9000 + i * 50,
            "recvUnixMs": 9000 + i * 50,
            "sendMonoNs": i * 50_000_000,
            "recvMonoNs": i * 50_000_000,
            "sampleUnixMs": 9000 + i * 50,
            "sampleMonoNs": i * 50_000_000,
        }
        pairs.append(
            {
                "server": dict(request, gameTime=980 + i),
                "observer": dict(request, tick=980 + i),
            }
        )
    return {
        "targetUuid": UUID,
        "dimension": "minecraft:overworld",
        "baselineTicks": 40,
        "clockDomain": "shared_local_unix",
        "calibrationPairs": pairs,
    }


class Geometry(unittest.TestCase):
    def test_identical_rotated_mirrored_boxes_and_camera_translation(self):
        for angle in (0, 0.72, math.pi / 2):
            for mirror in (False, True):
                shape = box((100, 64, -70), angle=angle, mirror=mirror)
                oracle = pose.oracle_part(emitted(shape))
                result = pose.metrics(shape, oracle)
                self.assertAlmostEqual(result["center"], 0)
                self.assertAlmostEqual(result["corners"]["max"], 0)
                self.assertLess(max(result["axisDegrees"]), 1e-5)
                self.assertFalse(result["axisAmbiguous"])

    def test_known_offset_and_translation_normalization(self):
        shape = box()
        observed = pose.oracle_part(emitted(box((0.01, 0.02, 0.02))))
        result = pose.metrics(shape, observed)
        self.assertAlmostEqual(result["center"], 0.03)
        self.assertAlmostEqual(result["corners"]["rms"], 0.03)
        self.assertAlmostEqual(result["corners"]["p95"], 0.03)
        normalized = pose.metrics(shape, observed, observer_anchor=(0.01, 0.02, 0.02))
        self.assertAlmostEqual(normalized["corners"]["max"], 0)

    def test_rotation_and_extent_error(self):
        angle = math.radians(10)
        observed = pose.oracle_part(emitted(box(angle=angle)))
        result = pose.metrics(box(), observed)
        self.assertAlmostEqual(max(result["axisDegrees"]), 10)
        self.assertAlmostEqual(
            result["corners"]["rms"], 2 * math.hypot(0.2, 0.3) * math.sin(angle / 2)
        )
        scaled = pose.metrics(
            box(), pose.oracle_part(emitted(box(half=(0.22, 0.33, 0.11))))
        )
        self.assertAlmostEqual(sum(abs(x) for x in scaled["extentDifference"]), 0.12)

    def test_cube_axes_ambiguous_and_assignment_is_bijective(self):
        shape = box(half=(0.2, 0.2, 0.2))
        self.assertTrue(
            pose.metrics(shape, pose.oracle_part(emitted(shape)))["axisAmbiguous"]
        )
        errors = pose.assignment([(0, 0, 0), (0.1, 0, 0)], [(0, 0, 0), (1, 0, 0)])
        self.assertEqual(errors, [0, 0.9])

    def test_nonbox_never_fitted_and_false_box_rejected(self):
        bent = emitted(box())
        bent["vertices"][0][0] += 0.05
        with self.assertRaises(ValueError):
            pose.oracle_part(bent)
        bent["shape"] = "quads"
        oracle = pose.oracle_part(bent)
        self.assertIsNone(oracle["box"])
        self.assertIsNone(pose.inside((0, 0, 0), oracle))
        self.assertEqual(pose.metrics(box(), oracle)["failure"], "non_box_oracle")
        with self.assertRaises(ValueError):
            pose.oracle_part(
                {"shape": "box", "vertices": emitted(box())["vertices"][:20]}
            )

    def test_invalid_finite_axes_and_schema(self):
        for changed in (
            {"schema": "blockfield.bodypose.v2"},
            {"targetUuid": "nope"},
            {"epoch": -1},
            {"dimension": "../world"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                pose.validate_server(dict(server(), **changed))
        bad = server()
        bad["skinObbs"][0]["half"] = [math.nan, 1, 1]
        with self.assertRaises(ValueError):
            pose.validate_server(bad)
        with self.assertRaises(ValueError):
            pose.validate_box(dict(box(), axes=((1, 0, 0), (1, 0, 0), (0, 0, 1))))


class ScaledBoxes(unittest.TestCase):
    SCALE = 0.9375

    def scaled(self, scale=SCALE):
        shape = box()
        return {
            "center": shape["center"],
            "half": shape["half"],
            "axes": [tuple(x * scale for x in axis) for axis in shape["axes"]],
        }

    def test_uniform_model_scale_folds_into_half(self):
        raw = self.scaled()
        normalized = pose.validate_box(raw)
        for axis in normalized["axes"]:
            self.assertAlmostEqual(pose.norm(axis), 1.0)
        for got, want in zip(normalized["half"], box()["half"]):
            self.assertAlmostEqual(got, want * self.SCALE)
        self.assertEqual(
            [[round(x, 9) for x in c] for c in pose.corners(normalized)],
            [[round(x, 9) for x in c] for c in pose.corners(raw)],
        )

    def test_nonuniform_scale_and_skew_still_rejected(self):
        raw = self.scaled()
        stretched = dict(
            raw,
            axes=(
                raw["axes"][0],
                tuple(x * 1.1 for x in raw["axes"][1]),
                raw["axes"][2],
            ),
        )
        with self.assertRaises(ValueError):
            pose.validate_box(stretched)
        with self.assertRaises(ValueError):
            pose.validate_box(dict(box(), axes=((1, 0, 0), (1, 0, 0), (0, 0, 1))))
        with self.assertRaises(ValueError):
            pose.validate_box(dict(box(), axes=((0, 0, 0), (0, 1, 0), (0, 0, 1))))

    def test_real_candidate_dump_excerpt_loads(self):
        # Verbatim HEAD skin:inner world box from the acc-damage candidate dump:
        # the 0.9375 model scale lives inside axes and must not fail loading.
        world = {
            "center": [0.49978027342876885, 152.62660996615887, 0.5601482393685728],
            "axes": [
                [0.9374999403953552, 0.0, -0.0],
                [0.0, -0.9374553561210632, -0.009147644974291325],
                [0.0, 0.009147644974291325, -0.9374553561210632],
            ],
            "half": [0.25, 0.25, 0.25],
        }
        loaded = pose.validate_box(world)
        for axis in loaded["axes"]:
            self.assertAlmostEqual(pose.norm(axis), 1.0)
        for got in loaded["half"]:
            self.assertAlmostEqual(got, 0.234375, places=6)

    def test_gap_harness_accepts_scaled_candidate_dump(self):
        world = {
            "center": [0.49978027342876885, 152.62660996615887, 0.5601482393685728],
            "axes": [
                [0.9374999403953552, 0.0, -0.0],
                [0.0, -0.9374553561210632, -0.009147644974291325],
                [0.0, 0.009147644974291325, -0.9374553561210632],
            ],
            "half": [0.25, 0.25, 0.25],
        }
        record = {
            "schema": "blockfield.bodypose.v2",
            "gameTime": 53628891,
            "visible": {"HEAD": [{"id": "skin:inner", "world": world}]},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidate.ndjson"
            path.write_text(json.dumps(record) + "\n")
            boxes = pose.load_dump_boxes(path)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["part"], "HEAD")
        self.assertAlmostEqual(pose.norm(boxes[0]["axes"][0]), 1.0)


class Rays(unittest.TestCase):
    def test_independent_inside_zero_parallel_and_mirror(self):
        for angle in (0, 0.6):
            shape = box(angle=angle, mirror=True)
            geometry = pose.oracle_part(emitted(shape))
            for start, end in (
                ((0, 0, 0), (0, 0, 0)),
                ((0, 0, 0), (5, 0, 0)),
                ((-2, 0, 0), (2, 0, 0)),
                ((-2, 3, 0), (2, 3, 0)),
            ):
                actual, expected = (
                    pose.quad_ray(start, end, geometry),
                    pose.slab(start, end, shape),
                )
                if actual is None or expected is None:
                    self.assertEqual(actual, expected)
                else:
                    self.assertAlmostEqual(actual, expected)
            self.assertIsNone(pose.quad_ray((2, 0, 0), (2, 0, 0), geometry))

    def test_seam_tie_order_and_graze(self):
        shapes = [box() for _ in pose.PARTS]
        geometry = dict(zip(pose.PARTS, [pose.oracle_part(emitted(s)) for s in shapes]))
        label, _, seam, graze = pose.label(
            (-2, 0.3, 0.1), (2, 0.3, 0.1), geometry, True
        )
        self.assertEqual(label, "HEAD")
        self.assertTrue(seam)
        self.assertTrue(graze)

    def test_label_grid_zero_and_known_flip_with_miss(self):
        shapes = [box((0, i * 0.7, 0)) for i in range(6)]
        geometry = dict(zip(pose.PARTS, [pose.oracle_part(emitted(s)) for s in shapes]))
        result = pose.ray_report(shapes, shapes, geometry, 0.1, [])
        self.assertEqual(result["skin"]["disagreements"], 0)
        self.assertGreater(result["skin"]["allGrid"], result["skin"]["unionHit"])
        self.assertGreater(result["skin"]["confusion"]["MISS"]["MISS"], 0)
        swap = copy.deepcopy(shapes)
        swap[0], swap[1] = swap[1], swap[0]
        result = pose.ray_report(swap, shapes, geometry, 0.1, [])
        self.assertGreater(result["skin"]["headTorso"], 0)
        self.assertEqual(result["damage"]["disagreements"], 0)
        self.assertTrue(result["skin"]["mismatches"][0]["start"])

    def test_grid_covers_six_cardinal_views_and_distinct_eyes(self):
        shapes = [box((0, i * 0.7, 0)) for i in range(6)]
        geometry = dict(zip(pose.PARTS, [pose.oracle_part(emitted(s)) for s in shapes]))
        specs = {
            id(spec): spec for spec, _, _ in pose.grid(shapes, geometry, 0.5)
        }.values()
        orthographic = [s["direction"] for s in specs if s["eye"] is None][:6]
        self.assertEqual(
            orthographic,
            [(-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1)],
        )
        eyes = [tuple(s["eye"]) for s in specs if s["eye"] is not None]
        self.assertEqual(len(eyes), len(set(eyes)), eyes)


class Alignment(unittest.TestCase):
    def compare(self, servers=None, observers=None, meta=None):
        return pose.compare(
            [pose.validate_server(s) for s in servers or [server()]],
            [pose.validate_observer(s) for s in observers or [observer()]],
            meta or manifest(),
            rays=False,
        )

    def test_tick_mapping_dedup_and_clock_independent_of_geometry(self):
        result = self.compare(observers=[observer(), observer()])
        self.assertEqual(result["pairedFrames"], 1)
        self.assertEqual(result["duplicateReceipts"], 1)
        self.assertEqual(result["frames"][0]["sceneTimeDiagnosticTicks"], 0.5)
        self.assertEqual(result["frames"][0]["gameTime"], 1000)
        self.assertEqual(result["frames"][0]["serverTick"], 100)

    def test_cached_unequal_ages_do_not_shift_pairing_or_scene_clock(self):
        meta = manifest()
        for pair in meta["calibrationPairs"]:
            for name, age in (("server", 100), ("observer", 1500)):
                pair[name]["sampleUnixMs"] -= age
                pair[name]["sampleMonoNs"] -= age * 1_000_000
                pair[name]["gameTime" if name == "server" else "tick"] -= age // 50
        receipt = observer()
        receipt["_request"] = {
            "sendUnixMs": 11480,
            "recvUnixMs": 11500,
            "sendMonoNs": 0,
            "recvMonoNs": 20_000_000,
        }
        result = self.compare([server(t) for t in range(100, 136)], [receipt], meta)
        row = result["frames"][0]
        self.assertEqual(row["serverTick"], 100)
        self.assertEqual(row["sampleAgeAtReceiveMs"], 1500)
        self.assertEqual(row["requestDurationMs"], 20)
        self.assertEqual(row["sceneTimeDiagnosticTicks"], 0.5)
        self.assertEqual(result["calibration"]["observer"], 0)
        self.assertEqual(
            result["calibration"]["sampleAgeAtReceiveMs"]["observer"]["max"], 1500
        )

    def test_stale_post_baseline_does_not_change_clock_offset(self):
        meta = manifest()
        meta["postCalibrationPairs"] = copy.deepcopy(meta["calibrationPairs"])
        for pair in meta["postCalibrationPairs"]:
            for name in ("server", "observer"):
                pair[name]["sendUnixMs"] += 3000
                pair[name]["recvUnixMs"] += 3000
                pair[name]["sendMonoNs"] += 3_000_000_000
                pair[name]["recvMonoNs"] += 3_000_000_000
        result = self.compare(meta=meta)
        self.assertEqual(result["frames"][0]["serverTick"], 100)
        self.assertEqual(
            result["calibration"]["sampleAgeAtReceiveMs"]["server"]["max"], 3000
        )
        self.assertEqual(result["calibration"]["uncertaintyMs"], 1)

    def test_uncalibrated_remote_and_missing_metadata_are_inconclusive(self):
        for kind in ("remote", "missing", "undeclared"):
            meta = manifest()
            if kind == "remote":
                meta["remote"] = True
            elif kind == "undeclared":
                del meta["clockDomain"]
            else:
                del meta["calibrationPairs"][0]["observer"]["sampleMonoNs"]
            with self.subTest(kind=kind):
                result = self.compare(meta=meta)
                self.assertEqual(result["alignment"], "inconclusive")
                self.assertEqual(result["frames"][0]["alignment"], "inconclusive")
                self.assertTrue(result["calibration"]["reasons"])

    def test_wall_monotonic_drift_and_request_clock_jump(self):
        meta = manifest()
        meta["postCalibrationPairs"] = copy.deepcopy(meta["calibrationPairs"])
        for pair in meta["postCalibrationPairs"]:
            pair["observer"]["sampleUnixMs"] += 300
        result = self.compare(meta=meta)
        self.assertEqual(result["alignment"], "inconclusive")
        self.assertGreaterEqual(result["calibration"]["uncertaintyMs"], 300)
        meta = manifest()
        meta["calibrationPairs"][0]["server"]["recvUnixMs"] += 100
        with self.assertRaisesRegex(ValueError, "wall clock changed"):
            self.compare(meta=meta)

    def test_window_nominal_not_lowest_error_and_fractional_diagnostic(self):
        a, b = server(), server(101)
        for part in a["skinObbs"]:
            part["center"] = pose.add(part["center"], (0.04, 0, 0))
        receipt = observer()
        receipt["sampleUnixMs"] += 20
        result = self.compare([a, b], [receipt])
        self.assertEqual(result["frames"][0]["serverTick"], 100)
        self.assertAlmostEqual(
            result["frames"][0]["parts"]["HEAD"]["world"]["center"], 0.04
        )
        self.assertTrue(result["frames"][0]["interpolatedDiagnosticOnly"]["available"])
        self.assertEqual(result["calibration"]["windowTicks"], 2)

    def test_epoch_discontinuity_and_alignment_inconclusive(self):
        a, b, receipt = server(), server(101, 1), observer()
        receipt["sampleUnixMs"] += 20
        result = self.compare([a, b], [receipt])
        self.assertFalse(result["frames"][0]["interpolatedDiagnosticOnly"]["available"])
        meta = manifest()
        meta["clockDriftMs"] = 300
        self.assertEqual(self.compare(meta=meta)["alignment"], "inconclusive")

    def test_gaps_freshness_unrelated_and_nonbox_failure(self):
        receipt = observer(110)
        self.assertEqual(
            self.compare(observers=[receipt])["failures"][0]["reason"],
            "server_capture_gap",
        )
        receipt = observer()
        receipt["_request"] = {"recvUnixMs": 13000}
        self.assertEqual(
            self.compare(observers=[receipt])["failures"][0]["reason"],
            "stale_or_future_receipt",
        )
        receipt = observer()
        receipt["dimension"] = "minecraft:the_nether"
        self.assertEqual(self.compare(observers=[receipt])["pairedFrames"], 0)
        receipt = observer()
        receipt["parts"]["head"]["shape"] = "quads"
        self.assertEqual(self.compare(observers=[receipt])["nonBoxParts"], 1)

    def test_interpolation_proper_mirrored_basis(self):
        middle = pose.interpolate(
            box(mirror=True), box(angle=math.pi / 2, mirror=True), 0.5
        )
        self.assertAlmostEqual(middle["axes"][0][0], math.sqrt(0.5))
        self.assertAlmostEqual(middle["axes"][0][1], math.sqrt(0.5))
        pose.validate_box(middle)

    def test_load_preserves_missing_receipts_as_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipts.ndjson"
            path.write_text(
                json.dumps(observer())
                + "\n"
                + json.dumps({"receipt": {"errorCode": "geometry_missing"}})
                + "\n"
            )
            records, failures = pose.load_records(path, pose.validate_observer)
            self.assertEqual(len(records), 1)
            self.assertEqual(len(failures), 1)


class Baseline(unittest.TestCase):
    def report(self):
        meta = manifest()
        meta.update(
            cohort={"kind": "base"},
            declaredInputs={},
            settings={},
            actions=[{"at": 0, "command": "hold key.forward 10"}],
            phases=[
                {"phase": "onset", "start": 0, "end": 0.5},
                {"phase": "steady", "start": 0.5, "end": 1},
            ],
        )
        receipts = [observer(), observer(101)]
        for receipt, phase in zip(receipts, ("onset", "steady")):
            receipt["cohort"] = {"kind": "base", "phase": phase}
        return pose.compare(
            [pose.validate_server(server(t)) for t in (100, 101)],
            [pose.validate_observer(r) for r in receipts],
            meta,
            spacing=0.1,
        )

    def test_damage_only_regression_retains_label_counts(self):
        baseline = self.report()
        candidate = copy.deepcopy(baseline)
        damage = candidate["cohorts"][0]["rays"]["damage"]
        damage["disagreements"] += 1
        damage["falseMisses"] += 1
        damage["confusion"]["HEAD"]["HEAD"] -= 1
        damage["confusion"]["MISS"]["HEAD"] += 1
        damage["unionHitRate"] = damage["disagreements"] / damage["unionHit"]
        result = pose.baseline_comparison(baseline, candidate)
        self.assertTrue(result["regression"])
        self.assertFalse(result["inconclusive"])
        self.assertEqual(result["deltas"][0]["rays"]["skin"]["unionHitRateDelta"], 0)
        self.assertEqual(
            result["deltas"][0]["rays"]["damage"]["candidate"]["falseMisses"], 1
        )
        self.assertFalse(candidate["accepted"])

    def test_missing_onset_or_metrics_cannot_look_like_improvement(self):
        baseline = self.report()
        for kind in (
            "onset",
            "pose",
            "labels",
            "confusion",
            "inputs",
            "actions",
            "phases",
            "frames",
        ):
            candidate = copy.deepcopy(baseline)
            if kind == "onset":
                candidate["cohorts"].pop(0)
            elif kind == "pose":
                del candidate["cohorts"][0]["parts"]["HEAD"]["cornerMax"]["max"]
            elif kind == "labels":
                del candidate["cohorts"][0]["rays"]["damage"]
            elif kind == "confusion":
                candidate["cohorts"][0]["rays"]["damage"]["confusion"] = {}
            elif kind == "frames":
                candidate["cohorts"][0]["frames"] += 1
            else:
                key = "declaredInputs" if kind == "inputs" else kind
                del candidate["manifest"][key]
            with self.subTest(kind=kind):
                result = pose.baseline_comparison(baseline, candidate)
                self.assertTrue(result["inconclusive"])
                self.assertIsNone(result["regression"])
        del baseline["manifest"]["declaredInputs"]
        self.assertTrue(pose.baseline_comparison(baseline, baseline)["inconclusive"])

    def test_pose_max_and_contract_changes_are_compared(self):
        baseline = self.report()
        candidate = copy.deepcopy(baseline)
        candidate["cohorts"][0]["parts"]["HEAD"]["cornerMax"]["max"] += 0.1
        self.assertTrue(pose.baseline_comparison(baseline, candidate)["regression"])
        candidate = copy.deepcopy(baseline)
        candidate["manifest"]["actions"][0].update(
            elapsed=0.01, result={"receipt": "ok"}
        )
        self.assertFalse(pose.baseline_comparison(baseline, candidate)["regression"])
        candidate["manifest"]["actions"][0]["command"] = "hold key.backward 10"
        self.assertTrue(pose.baseline_comparison(baseline, candidate)["inconclusive"])


class Collector(unittest.TestCase):
    def test_ownership_refuses_foreign_server_before_actions(self):
        game = Mock()
        game.require_process.side_effect = RuntimeError("foreign session")
        with self.assertRaisesRegex(RuntimeError, "foreign"):
            body_pose.require_server(game, None)
        game.require_process.reset_mock()
        body_pose.require_server(game, "remote.example:25575")
        game.require_process.assert_not_called()

    def test_action_boundary_and_forged_packets_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "actions.json"
            for command in (
                "forge roll",
                "chat /kill @a",
                "hold key.forward 201",
                "tacz reload\nkill @a",
            ):
                path.write_text(json.dumps([{"at": 0.2, "command": command}]))
                with self.subTest(command=command), self.assertRaises(ValueError):
                    body_pose.actions_from(path, False, 10)
            path.write_text(json.dumps([{"at": 0.2, "command": "tacz reload"}]))
            self.assertEqual(len(body_pose.actions_from(path, False, 10)), 1)
            with self.assertRaises(ValueError):
                body_pose.actions_from(path, True, 10)

    def test_collector_keeps_evidence_and_clears_its_selection(self):
        from mcp import game

        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source = manifest()
            source.update(
                debugStartupFlag=True,
                fakeNativeStateVerified=True,
                fakeTargetName="Fake",
                cohort={"kind": "base"},
            )
            input_path = home / "run.json"
            input_path.write_text(json.dumps(source))
            actions = home / "actions.json"
            actions.write_text(
                json.dumps([{"at": 0, "command": "player Fake move forward"}])
            )
            args = SimpleNamespace(
                target=UUID,
                ticks=1,
                manifest=input_path,
                actions=actions,
                fake_target=True,
                remote_address=None,
                rcon_password=None,
                server_capture=None,
            )

            def bot(config, client, command):
                self.assertEqual(client, 1)
                return (
                    observer() if command == "playergeometry state" else {"uuid": UUID}
                )

            def rcon(command):
                return json.dumps(server()) if " dump " in command else "{}"

            with (
                patch.object(game, "job", return_value=(home, {})),
                patch.object(body_pose, "require_server"),
                patch.object(
                    body_pose,
                    "verify_client",
                    return_value={"dimension": "minecraft:overworld"},
                ),
                patch.object(body_pose, "stable_baseline", return_value=server()),
                patch.object(game, "bot", side_effect=bot) as client,
                patch.object(game, "rcon", side_effect=rcon) as commands,
                patch.object(body_pose.time, "sleep"),
                patch.object(
                    body_pose.time, "monotonic", side_effect=itertools.count(0, 0.01)
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(body_pose.run(args), 1)
            output = next(home.glob("body-pose-*"))
            recorded = json.loads((output / "manifest.json").read_text())
            self.assertEqual(len(recorded["calibrationPairs"]), 10)
            self.assertEqual(len(recorded["postCalibrationPairs"]), 10)
            self.assertEqual(recorded["clockDomain"], "shared_local_unix")
            self.assertEqual(
                recorded["calibrationPairs"][0]["observer"]["sampleMonoNs"],
                observer()["sampleMonoNs"],
            )
            self.assertIn("incomplete", recorded)
            self.assertTrue((output / "observer.ndjson").is_file())
            self.assertIn(
                "playergeometry clear", [call.args[2] for call in client.call_args_list]
            )
            self.assertIn(
                "player Fake stop", [call.args[0] for call in commands.call_args_list]
            )
            self.assertEqual(
                recorded["actions"][0]["command"], "player Fake move forward"
            )

    def test_cli_unknown_schema_writes_failure_json_and_table(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source, receipts, meta, output = [
                home / name
                for name in ("server.ndjson", "observer.ndjson", "run.json", "out.json")
            ]
            source.write_text(json.dumps(dict(server(), schema="unknown.v99")) + "\n")
            receipts.write_text(json.dumps(observer()) + "\n")
            meta.write_text(json.dumps(manifest()))
            with patch.object(
                sys,
                "argv",
                [
                    "compare_body_pose.py",
                    "--server",
                    str(source),
                    "--observer",
                    str(receipts),
                    "--manifest",
                    str(meta),
                    "--output",
                    str(output),
                ],
            ):
                self.assertEqual(pose.main(), 1)
            report = json.loads(output.read_text())
            self.assertEqual(report["pairedFrames"], 0)
            self.assertEqual(
                report["inputFailures"]["server"][0]["reason"], "unknown server schema"
            )
            self.assertTrue(output.with_suffix(".md").is_file())

    def test_stable_baseline_waits_on_ticks_and_has_deadline(self):
        later = server(140)
        with (
            patch.object(body_pose.time, "monotonic", side_effect=[0, 1, 2]),
            patch.object(body_pose.time, "sleep"),
        ):
            self.assertEqual(
                body_pose.stable_baseline(Mock(side_effect=[server(), later]), UUID)[
                    "serverTick"
                ],
                140,
            )
        with (
            patch.object(body_pose.time, "monotonic", side_effect=[0, 11]),
            self.assertRaises(TimeoutError),
        ):
            body_pose.stable_baseline(Mock(), UUID)

    def test_brackets_keep_missing_geometry(self):
        with (
            patch.object(
                body_pose.time, "time_ns", side_effect=[100_000_000, 120_000_000]
            ),
            patch.object(body_pose.time, "monotonic_ns", side_effect=[1, 20]),
        ):
            result = body_pose.bracket(
                Mock(side_effect=RuntimeError("culled")), "playergeometry state"
            )
        self.assertEqual(result["request"]["recvUnixMs"], 120)
        self.assertEqual(result["receipt"], {"error": "culled"})


def v2_receipt():
    parts = {}
    for i, part in enumerate(pose.PARTS):
        shape = box((0, i * 0.7, 0))
        inner = dict(emitted(shape), id="inner", source="base")
        gear = dict(
            emitted(shape), id="magazine_0", source="layer:AccessoriesRenderLayer"
        )
        parts[part.lower()] = {"boxes": [inner, gear]}
    return dict(
        observer(), schema="blockfield.playergeometry.v2", skinModel="wide", parts=parts
    )


def oracle_boxes():
    return [dict(box((0, i * 0.7, 0)), part=p) for i, p in enumerate(pose.PARTS)]


class CompoundMode(unittest.TestCase):
    def test_normalize_covers_scheme_and_leaves_server_dump_alone(self):
        cases = [
            ("inner", "base", "HEAD", "skin:inner"),
            ("hat", "base:hat", "HEAD", "skin:hat"),
            (
                "vest_front",
                "layer:AccessoriesRenderLayer",
                "TORSO",
                "uniform:body/vest_front",
            ),
            (
                "chest",
                "layer:HumanoidArmorLayer:chest",
                "TORSO",
                "armor:chest/torso/chest",
            ),
            ("canopy", "layer:ParachuteRenderer", "TORSO", "curio:back/torso/canopy"),
            (
                "lens",
                "layer:ThermalImagingGogglesRenderer",
                "HEAD",
                "curio:head/head/lens",
            ),
        ]
        for raw, source, bone, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(pose.normalize_box_id(raw, source, bone), expected)
        receipt = v2_receipt()
        receipt["parts"]["head"]["boxes"].append(
            dict(emitted(box()), id="cape", source="layer:CapeLayer")
        )
        result = pose.normalize_receipt_v2(receipt)
        self.assertEqual(result["unattributed"], [])
        self.assertIn("skin:inner", result["ids"]["HEAD"])
        self.assertIn("uniform:body/magazine_0", result["ids"]["TORSO"])
        dump = {"visible": {"HEAD": [{"id": "raw", "world": box()}]}, "damageObbs": []}
        before = copy.deepcopy(dump)
        pose.normalize_receipt_v2(v2_receipt())
        self.assertEqual(dump, before)

    def test_accessories_child_path_strips_bone_prefix(self):
        # The testbot registers the full child path (torso/vest_front); the §3.4 id
        # keeps the shared UniformGeometry root name once (uniform:body/...).
        # Bare leaf ids keep working.
        self.assertEqual(
            pose.normalize_box_id(
                "torso/vest_front", "layer:AccessoriesRenderLayer", "TORSO"
            ),
            "uniform:body/vest_front",
        )
        self.assertEqual(
            pose.normalize_box_id(
                "vest_front", "layer:AccessoriesRenderLayer", "TORSO"
            ),
            "uniform:body/vest_front",
        )
        self.assertEqual(
            pose.normalize_box_id("mask", "layer:AccessoriesRenderLayer", "HEAD"),
            "uniform:head/mask",
        )

    def test_base_bone_roots_are_skin_inner(self):
        # The testbot registers each skin bone under its bone name; that run holds
        # the inner cube, so the §3.4 id is skin:inner, never skin:<bone>.
        for raw, bone in (
            ("head", "HEAD"),
            ("torso", "TORSO"),
            ("left_arm", "LEFT_ARM"),
            ("right_arm", "RIGHT_ARM"),
            ("left_leg", "LEFT_LEG"),
            ("right_leg", "RIGHT_LEG"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(pose.normalize_box_id(raw, "base", bone), "skin:inner")
        receipt = v2_receipt()
        receipt["parts"]["head"]["boxes"].append(
            dict(emitted(box()), id="head", source="base")
        )
        self.assertEqual(pose.normalize_receipt_v2(receipt)["unattributed"], [])

    def test_vertex_bound_is_4096(self):
        self.assertEqual(pose.MAX_VERTICES, 4096)
        part = {"shape": "quads", "vertices": [[0, 0, 0]] * 400}
        self.assertIsNone(pose.oracle_part(part)["box"])
        with self.assertRaises(ValueError):
            pose.oracle_part({"shape": "quads", "vertices": [[0, 0, 0]] * 4100})

    def test_registration_uses_oracle_and_vanilla_tables_only(self):
        import inspect

        self.assertNotIn("server", inspect.signature(pose.register_bone).parameters)
        for part in pose.PARTS:
            local = pose.vanilla_inner_box(part)
            frame = {
                "rotation": ((0, 0.9375, 0), (0.9375, 0, 0), (0, 0, -0.9375)),
                "translation": (7, 151, -3),
            }
            world = pose.transform_box(local, frame)
            vertices = native_emitted(world)["vertices"]
            registration = pose.register_bone(vertices, part)
            for got, want in zip(registration["rotation"], frame["rotation"]):
                for a, b in zip(got, want):
                    self.assertAlmostEqual(a, b)
            for a, b in zip(registration["translation"], frame["translation"]):
                self.assertAlmostEqual(a, b)
            self.assertEqual(registration["label"], "native_order")
            with self.assertRaises(ValueError):
                pose.register_bone(vertices, part, vertex_order="sodium")

    def test_bone_registered_corner_error(self):
        local = pose.vanilla_inner_box("TORSO")
        frame = {
            "rotation": ((0, 0.9375, 0), (0.9375, 0, 0), (0, 0, -0.9375)),
            "translation": (7, 151, -3),
        }
        oracle = native_emitted(pose.transform_box(local, frame))
        reg = pose.register_bone(oracle["vertices"], "TORSO")
        result = pose.bone_registered_errors(
            local, pose.native_corners(oracle["vertices"]), reg
        )
        self.assertAlmostEqual(result["corner"]["max"], 0)
        shifted = dict(local, center=pose.add(local["center"], (0.05, 0, 0)))
        moved = pose.bone_registered_errors(
            shifted, pose.native_corners(oracle["vertices"]), reg
        )
        self.assertAlmostEqual(moved["center"], 0.05 * 0.9375)

    def test_edge_exclusion_and_excluded_drops(self):
        shapes = [box((0, i * 0.7, 0)) for i in range(6)]
        shifted = [box((0.005 + 0, i * 0.7, 0)) for i in range(6)]
        geometry = dict(zip(pose.PARTS, [pose.oracle_part(emitted(s)) for s in shapes]))
        raw = pose.ray_report(shifted, shifted, geometry, 0.1)
        gated = pose.ray_report(shifted, shifted, geometry, 0.1, edge_exclude=0.001)
        self.assertEqual(
            gated["skin"]["seamExcluded"] + gated["skin"]["gatedDisagreements"],
            raw["skin"]["disagreements"],
        )
        self.assertTrue(
            pose.compound_gate_pass(gated) or gated["skin"]["gatedDisagreements"] >= 0
        )
        point = tuple(pose.corners(shapes[0])[0])
        self.assertAlmostEqual(pose.union_edge_distance(point, shapes), 0)
        curtain = dict(box((0, 1.75, 0), half=(5, 5, 5)), part="OFF")
        dropped = pose.ray_report(shapes, shapes, geometry, 0.2, excluded=[curtain])
        self.assertGreater(dropped["skin"]["droppedExcluded"], 0)
        self.assertTrue(dropped["skin"]["dropped"][0]["start"])

    def test_validate_v2_receipt(self):
        record = pose.validate_receipt_v2(v2_receipt())
        self.assertEqual(len(record["geometry"]["HEAD"]), 2)
        bad = v2_receipt()
        bad["parts"]["head"]["boxes"] = []
        with self.assertRaises(ValueError):
            pose.validate_receipt_v2(bad)


class AcceptanceRegressions(unittest.TestCase):
    def test_sniper_strands_are_body_owned_even_with_excluded_words(self):
        for kit in ("sniper_red", "sniper_blue"):
            receipt = native_receipt()
            for i in range(12):
                receipt["parts"]["torso"]["boxes"].append(
                    dict(
                        native_emitted(box()),
                        source="layer:AccessoriesRenderLayer",
                        id=f"torso/strand_cape_{i}/jute",
                    )
                )
            self.assertEqual(len(pose.receipt_entries(receipt)["TORSO"]), 13, kit)
            self.assertEqual(pose.normalize_receipt_v2(receipt)["unattributed"], [])
        self.assertFalse(pose.is_excluded_box("base", "held_cape_arrow"))
        self.assertFalse(pose.is_excluded_box("layer:UnknownCapeRenderer", "cape"))
        self.assertFalse(
            pose.is_excluded_box("layer:PackedParachuteRenderer", "canopy")
        )
        for layer in pose.EXCLUDED_LAYERS:
            self.assertTrue(pose.is_excluded_box("layer:" + layer, "body"))

    def test_unowned_layer_geometry_is_a_failure(self):
        receipt = native_receipt()
        receipt["unattributed"] = [
            dict(native_emitted(box()), source="layer:UnknownCapeRenderer")
        ]
        report = pose.compound_contract(native_dump(), receipt, rays=False)
        self.assertEqual(report["unattributed"], ["layer:UnknownCapeRenderer"])
        receipt["unattributed"][0]["source"] = "layer:ItemInHandLayer"
        self.assertEqual(pose.normalize_receipt_v2(receipt)["unattributed"], [])
        self.assertEqual(len(pose.excluded_entries(receipt)), 1)

    def test_native_quads_keep_faces_and_reject_open_topology(self):
        raw = native_emitted(box((7, 151, -3), half=(0.007, 0.2, 0.15), angle=0.7))
        parsed = pose.oracle_part(raw)
        self.assertIsNotNone(parsed["box"])
        self.assertEqual(parsed["vertices"], [tuple(v) for v in raw["vertices"]])
        opened = dict(raw, vertices=raw["vertices"][:20])
        self.assertIsNone(pose.oracle_part(opened)["box"])
        with self.assertRaises(ValueError):
            pose.register_bone(opened["vertices"], "HEAD")

    def test_excluded_geometry_behind_body_uses_metres(self):
        shapes = [box((4 if i == 0 else 100 + i, 0, 0)) for i in range(6)]
        geometry = {p: pose.oracle_part(emitted(b)) for p, b in zip(pose.PARTS, shapes)}
        behind = box((5, 0, 0))
        with patch.object(
            pose, "grid", return_value=iter([({"index": 0}, (0, 0, 0), (10, 0, 0))])
        ):
            report = pose.ray_report(shapes, shapes, geometry, 0.025, excluded=[behind])
        self.assertEqual(report["skin"]["droppedExcluded"], 0)

    def test_malformed_ndjson_preserves_other_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.ndjson"
            path.write_text(
                json.dumps(server()) + "\n{broken\n" + json.dumps(server(101)) + "\n"
            )
            valid, failures = pose.load_records(path, pose.validate_server)
            self.assertEqual(len(valid), 2)
            self.assertEqual(failures[0]["line"], 2)

    def test_distance_ties_do_not_depend_on_segment_length(self):
        early = dict(box((0, 0, 0), half=(0.1, 0.1, 0.1)), part="TORSO")
        late = dict(box((0.000001, 0, 0), half=(0.1, 0.1, 0.1)), part="HEAD")
        for length in (1, 1000):
            self.assertEqual(
                pose.first_entry((-length, 0, 0), (length, 0, 0), [late, early])[0],
                "TORSO",
            )
        tied = dict(late, center=(pose.EPS / 2, 0, 0))
        self.assertEqual(
            pose.first_entry((-100, 0, 0), (100, 0, 0), [early, tied])[0], "HEAD"
        )

    def test_exact_segment_distance_and_crouched_off_labels(self):
        cube = box(half=(1, 1, 1))
        for start, end, distance in (
            ((-3, 2, 0), (3, 2, 0), 1),
            ((-3, 0, 0), (3, 0, 0), 0),
            ((-3, 2, 2), (3, 2, 2), math.sqrt(2)),
            ((2, 2, 2), (2, 2, 2), math.sqrt(3)),
        ):
            self.assertAlmostEqual(pose.segment_clearance(start, end, cube), distance)
        fixed = [-0.3, 0, -0.3, 0.3, 1.5, 0.3]
        self.assertEqual(
            pose.legacy_off_label((-0.2, 0.49, 0), fixed, 1.27), "LEFT_LEG"
        )
        self.assertEqual(pose.legacy_off_label((0.2, 0.49, 0), fixed, 1.27), "LEFT_LEG")
        self.assertEqual(pose.legacy_off_label((0.2, 0.5, 0), fixed, 1.27), "TORSO")
        for y in (1.02, 1.52):
            self.assertEqual(pose.legacy_off_label((0, y, 0), fixed, 1.27), "TORSO")
        self.assertEqual(pose.legacy_off_label((0, 1.27, 0), fixed, 1.27), "HEAD")

    def test_controls_require_verified_eye_and_reject_occlusion(self):
        boxes = {p: [] for p in pose.PARTS}
        boxes["HEAD"] = [
            {"id": "uniform:head/crown", "box": box((0, 0, -1), half=(1, 1, 0.2))}
        ]
        boxes["LEFT_LEG"] = [{"id": "uniform:left_leg/flap", "box": box((0, 0, 1))}]
        self.assertEqual(pose.control_rays_from_receipt(boxes), [])
        controls = pose.control_rays_from_receipt(boxes, fireable_eyes=[(0, 0, -3)])
        self.assertTrue(controls)
        self.assertTrue(all(r["part"] == "HEAD" for r in controls))
        self.assertTrue(all(r["firstEntryVerified"] for r in controls))
        blocked = [dict(box((0, 0, -2), half=(2, 2, 0.1)), part="HEAD")]
        self.assertEqual(
            pose.control_rays_from_receipt(
                boxes, fireable_eyes=[(0, 0, -3)], excluded=blocked
            ),
            [],
        )

    def test_compound_cli_and_world_residual_do_not_gate_local_geometry(self):
        receipt, dump = native_receipt(), native_dump()
        for part in pose.PARTS:
            world = dump["visible"][part][0]["world"]
            dump["visible"][part][0]["world"] = dict(
                world, center=pose.add(world["center"], (0.1, 0, 0))
            )
        dump["bones"][0][9] = 0.1
        dump["state"]["bodyYaw"] = 45
        receipt["nativePose"] = {"bodyYaw": 0}
        report = pose.compound_contract(dump, receipt, spacing=0.1)
        self.assertTrue(report["accepted"])
        self.assertAlmostEqual(report["parts"]["HEAD"]["worldCorners"]["max"], 0.1)
        self.assertEqual(report["bodyYawResidualDegrees"], 45)
        with tempfile.TemporaryDirectory() as folder:
            paths = [
                Path(folder) / n
                for n in (
                    "dump.json",
                    "receipt.json",
                    "manifest.json",
                    "comparison.json",
                )
            ]
            for path, data in zip(paths, (dump, receipt, manifest())):
                path.write_text(json.dumps(data, indent=2))
            argv = ["compare_body_pose.py", "--spacing", ".1"]
            for flag, path in zip(("server", "observer", "manifest", "output"), paths):
                argv.extend(["--" + flag, str(path)])
            with patch.object(sys, "argv", argv):
                self.assertEqual(pose.main(), 0)
            self.assertTrue(json.loads(paths[-1].read_text())["accepted"])


EVIDENCE = Path(os.environ.get("BF_GEOMETRY_EVIDENCE", "/nonexistent"))


@unittest.skipUnless(
    EVIDENCE.is_dir(), "stored offline evidence: set BF_GEOMETRY_EVIDENCE"
)
class StoredGeometryEvidence(unittest.TestCase):
    def read_pair(self, area, cohort):
        folder = EVIDENCE / area / cohort
        return tuple(
            json.loads((folder / name).read_text())
            for name in ("dump.json", "receipt.json")
        )

    def test_sniper_id_sets_and_rebound_corners(self):
        for cohort in (
            "tmp-snipers-red",
            "tmp-assault-red-empty",
            "tmp-assault-red",
            "sniper_blue-crouch-y0-rifle",
        ):
            dump, receipt = self.read_pair("c-matrix", cohort)
            pose.validate_server(dump)
            pose.validate_observer(receipt)
            report = pose.compound_contract(dump, receipt, rays=False)
            self.assertLessEqual(report["cornerMaxM"] * 1000, 0.03, cohort)
            self.assertTrue(
                all(
                    p["idSetEqual"] and p["complete"] for p in report["parts"].values()
                ),
                cohort,
            )

    def test_three_complete_25mm_grids(self):
        expected = {
            "tmp-assault-red-empty": (175300, 73933, 153),
            "tmp-assault-red": (222364, 77309, 0),
            "sniper_blue-crouch-y0-rifle": (245866, 83369, 3),
        }
        for cohort, (total, hits, mismatches) in expected.items():
            dump, receipt = self.read_pair("c-matrix", cohort)
            grid = pose.compound_contract(dump, receipt)["grid"]
            self.assertEqual(
                (grid["allGrid"], grid["unionHit"], grid["disagreements"]),
                (total, hits, mismatches),
                cohort,
            )
            self.assertEqual(grid["gatedDisagreements"], 0, cohort)
            self.assertEqual(grid["seamExcluded"], mismatches, cohort)

    def test_infantry_standing_separate_baseline_census(self):
        expected = {
            "on": (225610, 73773, (65, 192, 0, 0, 0, 0, 632)),
            "off": (242322, 133223, (624, 1798, 5, 16, 1406, 3699, 39809)),
        }
        maxima = {
            "on": (0.064300, 0.077690, 0.023947, 0.019578, None, None),
            "off": (0.173440, 0.405927, 0.055459, 0.095856, 0.171104, 0.382710),
        }
        for name, (total, hits, counts) in expected.items():
            area = "a-gate-on" if name == "on" else "b-gate-off"
            dump, receipt = self.read_pair(area, "infantry_red-standing")
            oracle, entries = pose.load_oracle_boxes(
                EVIDENCE / area / "infantry_red-standing/receipt.json"
            )
            base = (
                [dict(pose.validate_box(b), part=b["part"]) for b in dump["damageObbs"]]
                if name == "on"
                else [dict(pose.inflated_off_box(dump["fixed"]), part="OFF")]
            )
            result = pose.select_gap_rays(
                oracle,
                base,
                pose.oracle_bones(entries),
                baseline=name,
                fixed=dump["fixed"],
                eye_height=1.62,
            )
            self.assertEqual((result["scanned"], result["baselineHit"]), (total, hits))
            for category, count in zip((*pose.GAP_CATEGORIES, "other"), counts):
                self.assertEqual(
                    result["census"][category]["count"], count, (name, category)
                )
            for category, maximum in zip(pose.GAP_CATEGORIES, maxima[name]):
                row = result["census"][category]
                if maximum is None:
                    self.assertIsNone(row["largestClearanceM"])
                else:
                    self.assertAlmostEqual(
                        row["largestClearanceM"], maximum, delta=0.000002
                    )
                if not row["count"]:
                    self.assertEqual(row["status"], "N/A")

    def test_logged_control_first_entries_and_frozen_clearances(self):
        analysis = EVIDENCE.parent / "analysis"
        for row in json.loads((analysis / "controls.json").read_text()):
            _, receipt = self.read_pair("c-candidate", row["cohort"])
            entries = pose.receipt_entries(receipt)
            oracle = pose.prepare_entries([e for p in pose.PARTS for e in entries[p]])
            eye = row["eye"]
            end = pose.add(eye, pose.mul(row["loggedDirection"], 30))
            part, _, _ = pose.first_entry(eye, end, oracle, True)
            self.assertEqual(part.lower(), row["hp"])
        for name in ("on", "off"):
            frozen = json.loads((analysis / f"frozen-{name}.json").read_text())
            for cohort, setup in frozen["setups"].items():
                area = "a-gate-on" if name == "on" else "b-gate-off"
                oracle, _ = pose.load_oracle_boxes(
                    EVIDENCE / area / cohort / "receipt.json"
                )
                for ray in setup["rays"]:
                    self.assertGreaterEqual(
                        pose.compound_clearance(ray["eye"], ray["aim"], oracle), 0.0301
                    )


class SeamGrazeExclusion(unittest.TestCase):
    def predicted(self, *rows):
        return [dict(shape, part=part, id=box_id) for part, box_id, shape in rows]

    def entries(self, *rows):
        out = {p: [] for p in pose.PARTS}
        for part, box_id, shape in rows:
            parsed = pose.oracle_part(emitted(shape))
            out[part].append(
                {"id": box_id, "part": part, "parsed": parsed, "box": parsed["box"]}
            )
        return out

    def one_ray(self, start, end):
        return iter([({"index": 0}, tuple(start), tuple(end))])

    def test_inter_part_tie_within_1mm_is_dropped_and_listed(self):
        # Coincident entry faces on different parts: server enters TORSO at
        # x=0, oracle enters LEFT_ARM at x=0; each entry is 1 um from the
        # other part's face, far from every edge.
        torso = box((0.1, 0, 0), half=(0.1, 0.3, 0.1))
        arm_here = box((0.1, 0, 0), half=(0.1, 0.3, 0.1))
        arm_next = box((0.100001, 0, 0), half=(0.1, 0.3, 0.1))
        torso_next = box((0.100001, 0, 0), half=(0.1, 0.3, 0.1))
        server = self.predicted(
            ("TORSO", "skin:jacket", torso),
            ("LEFT_ARM", "skin:sleeve", arm_next),
        )
        oracle = self.entries(
            ("TORSO", "skin:jacket", torso_next),
            ("LEFT_ARM", "skin:sleeve", arm_here),
        )
        with patch.object(
            pose, "grid", return_value=self.one_ray((-1, 0, 0), (1, 0, 0))
        ):
            report = pose.compound_grid(server, oracle)
        self.assertEqual(report["disagreements"], 1)
        self.assertEqual(report["gatedDisagreements"], 0)
        self.assertEqual(report["droppedSeam"], 1)
        self.assertEqual(len(report["droppedSeamRays"]), 1)
        ray = report["droppedSeamRays"][0]
        self.assertEqual(ray["reason"], "inter_part_seam")
        self.assertEqual((ray["server"], ray["observer"]), ("TORSO", "LEFT_ARM"))

    def test_real_disagreement_2mm_from_other_part_faces_fails(self):
        # Same flip, but the losing faces sit 2 mm behind: both entries are
        # farther than 1 mm from any other-part face, so the ray must fail.
        torso = box((0.1, 0, 0), half=(0.1, 0.3, 0.1))
        arm_here = box((0.1, 0, 0), half=(0.1, 0.3, 0.1))
        arm_next = box((0.102, 0, 0), half=(0.1, 0.3, 0.1))
        torso_next = box((0.102, 0, 0), half=(0.1, 0.3, 0.1))
        server = self.predicted(
            ("TORSO", "skin:jacket", torso),
            ("LEFT_ARM", "skin:sleeve", arm_next),
        )
        oracle = self.entries(
            ("TORSO", "skin:jacket", torso_next),
            ("LEFT_ARM", "skin:sleeve", arm_here),
        )
        with patch.object(
            pose, "grid", return_value=self.one_ray((-1, 0, 0), (1, 0, 0))
        ):
            report = pose.compound_grid(server, oracle)
        self.assertEqual(report["disagreements"], 1)
        self.assertEqual(report["droppedSeam"], 0)
        self.assertEqual(report["droppedGraze"], 0)
        self.assertEqual(report["gatedDisagreements"], 1)
        self.assertEqual(len(report["mismatches"]), 1)
        mismatch = report["mismatches"][0]
        self.assertEqual(
            (mismatch["serverId"], mismatch["observerId"]),
            ("skin:jacket", "skin:sleeve"),
        )

    def test_sub_mm_chord_graze_is_dropped(self):
        # 0.8 mm chord through a HEAD box on the server, oracle misses.
        thin = box((0, 0, 0), half=(0.2, 0.3, 0.0004))
        far = box((5, 0, 0))
        server = self.predicted(
            ("HEAD", "skin:inner", thin), ("LEFT_ARM", "skin:inner", far)
        )
        oracle = self.entries(
            ("HEAD", "skin:inner", far), ("LEFT_ARM", "skin:inner", far)
        )
        with patch.object(
            pose, "grid", return_value=self.one_ray((0, 0, -1), (0, 0, 1))
        ):
            report = pose.compound_grid(server, oracle)
        self.assertEqual(report["disagreements"], 1)
        self.assertEqual(report["gatedDisagreements"], 0)
        self.assertEqual(report["droppedSeam"], 0)
        self.assertEqual(report["droppedGraze"], 1)
        self.assertEqual(report["droppedGrazeRays"][0]["reason"], "graze")

    def test_entry_chord_cos_thresholds(self):
        target = box((0, 0, 0), half=(1, 1, 1))
        start, end = (-0.5, 0.999, 0), (0.5, 1.001, 0)
        entry = pose.slab(start, end, target) * pose.norm(pose.sub(end, start))
        chord, cos = pose.entry_chord_cos(start, end, target, entry)
        self.assertGreater(chord, 0.001)
        self.assertLess(cos, 0.05)
        chord, cos = pose.entry_chord_cos(
            (0, 0, -1), (0, 0, 1), box((0, 0, 0), half=(0.2, 0.3, 0.0004)), 1 - 0.0004
        )
        self.assertLess(chord, 0.001)

    def test_intermediary_layer_names_are_excluded(self):
        pairs = (
            ("CapeLayer", "class_972"),
            ("ElytraLayer", "class_979"),
            ("ArrowLayer", "class_973"),
            ("BeeStingerLayer", "class_4506"),
            ("CustomHeadLayer", "class_976"),
            ("ParrotOnShoulderLayer", "class_983"),
            ("Deadmau5EarsLayer", "class_978"),
            ("ItemInHandLayer", "class_989"),
            ("PlayerItemInHandLayer", "class_5697"),
            ("StuckInBodyLayer", "class_4507"),
        )
        for named, cls in pairs:
            with self.subTest(layer=named):
                self.assertTrue(pose.is_excluded_box("layer:" + named, "body"))
                self.assertTrue(pose.is_excluded_box("layer:" + cls, "body"))
        self.assertFalse(pose.is_excluded_box("layer:class_99999", "body"))
        self.assertFalse(pose.is_excluded_box("layer:UnknownCapeRenderer", "cape"))

    def test_intermediary_cape_is_excluded_and_listed(self):
        receipt = native_receipt()
        receipt["parts"]["head"]["boxes"].append(
            dict(native_emitted(box()), id="cape", source="layer:class_972")
        )
        report = pose.compound_contract(native_dump(), receipt, rays=False)
        self.assertEqual(report["unattributed"], [])
        self.assertIn({"id": "cape", "part": "HEAD"}, report["excluded"])

    def test_unknown_intermediary_layer_stays_unattributed(self):
        receipt = native_receipt()
        receipt["parts"]["head"]["boxes"].append(
            dict(native_emitted(box()), id="thing", source="layer:class_99999")
        )
        report = pose.compound_contract(native_dump(), receipt, rays=False)
        self.assertTrue(report["unattributed"])
        self.assertEqual(report["excluded"], [])


class GapHarness(unittest.TestCase):
    def selection(self, spacing=0.1):
        oracles, entries = [], pose.receipt_entries(native_receipt())
        for part in pose.PARTS:
            oracles.extend(dict(e["box"], part=part) for e in entries[part])
        on = [
            dict(
                pose.vanilla_inner_box(p),
                part=p,
                half=tuple(h + 0.15 for h in pose.vanilla_inner_box(p)["half"]),
            )
            for p in pose.PARTS
        ]
        off = dict(pose.inflated_off_box([-0.4, 0, -0.4, 0.4, 1.6, 0.4]), part="OFF")
        bones = pose.oracle_bones(entries)
        return (
            oracles,
            on,
            off,
            pose.select_gap_rays(oracles, on, bones, spacing=spacing),
        )

    def test_gap_rays_clear_oracle_and_enter_the_selected_baseline(self):
        oracles, on, _, selection = self.selection()
        self.assertGreater(len(selection["rays"]), 0)
        for ray in selection["rays"]:
            for oracle in oracles:
                self.assertGreaterEqual(
                    pose.segment_clearance(ray["start"], ray["end"], oracle),
                    pose.GAP_CLEARANCE - 1e-9,
                )
            self.assertTrue(
                any(pose.slab(ray["start"], ray["end"], b) is not None for b in on)
            )
            self.assertIn(ray["category"], pose.GAP_CATEGORIES)
            self.assertEqual(ray["baseline"], "on")
            self.assertFalse(ray["verifiedHP"])
        for row in selection["census"].values():
            if row["count"] == 0 and "status" in row:
                self.assertEqual(row["status"], "N/A")

    def test_candidate_boxes_never_select(self):
        import inspect

        self.assertNotIn(
            "candidate", inspect.signature(pose.select_gap_rays).parameters
        )
        self.assertNotIn("limit", inspect.signature(pose.select_gap_rays).parameters)
        oracles, on, _, first = self.selection()
        bones = pose.oracle_bones(pose.receipt_entries(native_receipt()))
        second = pose.select_gap_rays(oracles, on, bones, spacing=0.1)
        self.assertEqual(first["rays"], second["rays"])

    def test_candidate_clearance_is_reported(self):
        _, _, _, selection = self.selection()
        far = dict(box((50, 50, 50)), part="HEAD")
        pose.report_candidate_clearance(selection["rays"], [far])
        for ray in selection["rays"]:
            self.assertGreater(ray["candidate_clearance"], 1)
            self.assertFalse(ray["candidate_hit"])

    def test_control_rays_come_from_receipt_gear_faces(self):
        boxes = {
            part: [
                {
                    "id": f"uniform:{part.lower()}/magazine_0",
                    "box": box((0, i * 0.7, 0)),
                },
                {"id": "skin:inner", "box": box((0, i * 0.7, 0))},
            ]
            for i, part in enumerate(pose.PARTS)
        }
        rays = pose.control_rays_from_receipt(
            boxes, fireable_eyes=[(2, i * 0.7, 0) for i in range(6)]
        )
        self.assertGreater(len(rays), 0)
        by_part = {part: box((0, i * 0.7, 0)) for i, part in enumerate(pose.PARTS)}
        for ray in rays:
            with self.subTest(ray=ray["id"]):
                self.assertEqual(ray["category"], "control")
                hit = pose.slab(
                    tuple(ray["start"]), tuple(ray["end"]), by_part[ray["part"]]
                )
                self.assertIsNotNone(hit)

    def test_freeze_roundtrip_and_attribution(self):
        _, _, _, selection = self.selection()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "frozen-rays.json"
            pose.freeze_ray_set(path, selection["rays"], {"kits": ["infantry_red"]})
            loaded = pose.load_ray_set(path)
            self.assertEqual(len(loaded["rays"]), len(selection["rays"]))
            with self.assertRaises(ValueError):
                path.write_text('{"version": 99, "rays": []}')
                pose.load_ray_set(path)
        good = [
            {
                "ray_id": r["id"],
                "hp_loss": 3,
                "part": r["expectedPart"],
                "expected_part": r["expectedPart"],
            }
            for r in selection["rays"]
        ]
        self.assertEqual(
            pose.verify_before_attribution({"rays": selection["rays"]}, good)[
                "failures"
            ],
            [],
        )
        bad = copy.deepcopy(good)
        bad[0]["hp_loss"] = 0
        self.assertEqual(
            len(
                pose.verify_before_attribution({"rays": selection["rays"]}, bad)[
                    "failures"
                ]
            ),
            1,
        )

    def test_inflated_off_box_math(self):
        off = pose.inflated_off_box([-0.3, 0, -0.3, 0.3, 1.8, 0.3])
        self.assertAlmostEqual(off["half"][0], 0.47)
        self.assertAlmostEqual(off["half"][1], 1.0)
        self.assertAlmostEqual(off["center"][1], 1.0)

    def test_gap_cli_freezes_ray_set(self):
        off_fixed = [-0.3, 0, -0.3, 0.3, 1.8, 0.3]
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            oracle_path = home / "oracle.ndjson"
            oracle_path.write_text(json.dumps(native_receipt()) + "\n")
            on_path = home / "on.ndjson"
            record = server()
            on_path.write_text(json.dumps(record) + "\n")
            off_path = home / "off.ndjson"
            off_record = dict(server(), fixed=off_fixed)
            off_path.write_text(json.dumps(off_record) + "\n")
            job = home / "job"
            with patch.object(
                sys,
                "argv",
                [
                    "compare_body_pose.py",
                    "--gap",
                    "--oracle",
                    str(oracle_path),
                    "--oracle-off",
                    str(oracle_path),
                    "--baseline-on",
                    str(on_path),
                    "--baseline-off",
                    str(off_path),
                    "--job-dir",
                    str(job),
                    "--spacing",
                    "0.1",
                ],
            ):
                self.assertEqual(pose.main(), 0)
            frozen = json.loads((job / "frozen-on.json").read_text())
            self.assertEqual(frozen["version"], 1)
            self.assertIn("rays", frozen)


class F3BPairing(unittest.TestCase):
    TICK = 53628904

    def write_pair(
        self,
        folder,
        draw_tick=TICK,
        server_tick=TICK,
        game_time=TICK + 50000,
        shift=0.0,
        legacy=False,
        legacy_capture=False,
        receipt_server_tick=None,
    ):
        shape = box((0.5, 152.6, 0.56))
        capture = {
            "schema": "blockfield.bodypose.v2",
            "gameTime": game_time,
            "visible": {"HEAD": [{"id": "skin:inner", "world": dict(shape)}]},
        }
        if not legacy_capture:
            capture["serverTick"] = server_tick
        (folder / "capture.ndjson").write_text(json.dumps(capture) + "\n")
        packed = [x + shift for corner in pose.corners(shape) for x in corner]
        debug = {
            "tick": draw_tick,
            "shapeKey": "wide/7f/none",
            "boxes": {"head": [packed]},
        }
        if not legacy:
            debug["serverTick"] = (
                server_tick if receipt_server_tick is None else receipt_server_tick
            )
        receipt = {
            "schema": "blockfield.playergeometry.v2",
            "debugBoxes": debug,
        }
        (folder / "receipt.ndjson").write_text(json.dumps(receipt) + "\n")

    def run_f3b(self, folder):
        out = folder / "f3b.json"
        with patch.object(
            sys,
            "argv",
            [
                "compare_body_pose.py",
                "--f3b",
                "--receipt",
                str(folder / "receipt.ndjson"),
                "--capture",
                str(folder / "capture.ndjson"),
                "--output",
                str(out),
            ],
        ):
            self.assertEqual(pose.main(), 0)
        return json.loads(out.read_text())

    def test_same_tick_pairs_within_tolerance(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.write_pair(home)
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 1)
        self.assertEqual(report["unpaired"], [])
        self.assertEqual(report["countMismatches"], [])
        self.assertTrue(report["accepted"])
        self.assertLessEqual(report["maxCornerDifferenceM"], 0.0005)
        self.assertEqual(report["pairing"], {"serverTick": 1, "tickLegacy": 0})
        self.assertEqual(report["captureKey"], "serverTick")

    def test_server_tick_pairs_when_draw_tick_differs(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.write_pair(home, draw_tick=self.TICK + 7)
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 1)
        self.assertEqual(report["unpaired"], [])
        self.assertTrue(report["accepted"])
        self.assertEqual(report["pairing"], {"serverTick": 1, "tickLegacy": 0})

    def test_server_tick_mismatch_is_unpaired(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.write_pair(home, receipt_server_tick=self.TICK + 7)
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 0)
        self.assertEqual(len(report["unpaired"]), 1)
        self.assertEqual(report["unpaired"][0]["pairedBy"], "serverTick")
        self.assertFalse(report["accepted"])

    def test_legacy_receipt_pairs_by_draw_tick(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.write_pair(home, legacy=True)
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 1)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["pairing"], {"serverTick": 0, "tickLegacy": 1})

    def test_legacy_capture_pairs_by_game_time(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.write_pair(
                home, draw_tick=self.TICK + 50000, legacy=True, legacy_capture=True
            )
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 1)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["pairing"], {"serverTick": 0, "tickLegacy": 1})
        self.assertEqual(report["captureKey"], "gameTime")

    def test_shifted_draw_reports_max_corner_difference(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.write_pair(home, shift=0.02)
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 1)
        self.assertFalse(report["accepted"])
        self.assertGreater(report["maxCornerDifferenceM"], 0.01)

    def test_pretty_printed_receipt_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            shape = box((0.5, 152.6, 0.56))
            (home / "capture.ndjson").write_text(
                json.dumps(
                    {
                        "schema": "blockfield.bodypose.v2",
                        "gameTime": self.TICK,
                        "visible": {
                            "HEAD": [{"id": "skin:inner", "world": dict(shape)}]
                        },
                    }
                )
                + "\n"
            )
            packed = [x for corner in pose.corners(shape) for x in corner]
            (home / "receipt.ndjson").write_text(
                json.dumps(
                    {
                        "schema": "blockfield.playergeometry.v2",
                        "debugBoxes": {
                            "tick": self.TICK,
                            "shapeKey": "wide/7f/none",
                            "boxes": {"head": [packed]},
                        },
                    },
                    indent=2,
                )
            )
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 1)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["pairing"], {"serverTick": 0, "tickLegacy": 1})
        self.assertEqual(report["captureKey"], "gameTime")
        self.assertEqual(report["pairing"], {"serverTick": 0, "tickLegacy": 1})

    def test_wrong_tick_is_unpaired_not_paired(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.write_pair(home, draw_tick=self.TICK + 7, legacy=True)
            report = self.run_f3b(home)
        self.assertEqual(report["paired"], 0)
        self.assertEqual(len(report["unpaired"]), 1)
        self.assertEqual(report["unpaired"][0]["pairedBy"], "tickLegacy")
        self.assertFalse(report["accepted"])


class ScenarioPlans(unittest.TestCase):
    def test_before_after_runs_cover_kits_poses_and_baselines(self):
        plan = body_pose.before_after_plan()
        self.assertEqual(len(plan), 3 * 3 * 2)
        self.assertEqual({run["run"] for run in plan}, {"a", "b", "c"})
        self.assertEqual(
            set(body_pose.BEFORE_AFTER_KITS),
            {"infantry_red", "engineer_blue", "sniper_blue"},
        )
        for run in plan:
            self.assertEqual(run["stationary_ticks"], 25)
            self.assertEqual(run["hold"], "rifle")

    def test_performance_plan_gates(self):
        plan = body_pose.performance_plan()
        self.assertEqual(plan["fakes"], 64)
        self.assertEqual(plan["warmup_s"], 60)
        self.assertEqual(plan["measure_s"], 120)
        self.assertEqual(plan["commit_p95_us"], 120)
        self.assertEqual(plan["clip_p95_us"], 10)
        self.assertEqual(plan["total_p95_ms"], 8)

    def test_explosion_plan_has_three_cases_without_multiplier(self):
        plan = body_pose.explosion_plan()
        self.assertEqual([c["case"] for c in plan], ["i", "ii", "iii"])
        self.assertTrue(all(c["multiplier"] is None for c in plan))

    def test_f3b_check_tolerance(self):
        shapes = [box((0, i * 0.7, 0)) for i in range(6)]
        self.assertTrue(body_pose.f3b_check(shapes, shapes)["pass"])
        moved = [box((0.001, i * 0.7, 0)) for i in range(6)]
        failed = body_pose.f3b_check(shapes, moved)
        self.assertFalse(failed["pass"])
        self.assertAlmostEqual(failed["worst"], 0.001)

    def test_lifecycle_check_with_corrections(self):
        dump = {
            "epoch": 5,
            "epoch_expected": 5,
            "frame": {"tick": 1},
            "serverTick": 100,
            "uniform": "uniform:infantry_red",
        }
        result = body_pose.lifecycle_check(
            dump, 100, "moved wrongly x=1", "infantry_red"
        )
        self.assertTrue(result["pass"])
        self.assertEqual(result["correctionsObserved"], 1)
        quiet = body_pose.lifecycle_check(dump, 100, "all calm", "infantry_red")
        self.assertEqual(quiet["corrections"], "inconclusive")
        self.assertFalse(quiet["pass"])
        stale = dict(dump, epoch_expected=6)
        self.assertFalse(
            body_pose.lifecycle_check(stale, 100, "moved too quickly", "infantry_red")[
                "pass"
            ]
        )

    def test_plan_cli_prints_json(self):
        for name in ("before-after", "perf", "explosion"):
            with patch("builtins.print") as printed:
                args = SimpleNamespace(plan=name)
                self.assertEqual(body_pose.run(args), 0)
                json.loads(printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
