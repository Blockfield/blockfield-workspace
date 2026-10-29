"""Unit tests for the Workshop AI benchmark harness (pure parts only)."""

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import wsai_bench as bench  # noqa: E402


class RunJobTest(unittest.TestCase):
    def test_reads_receipts_from_file_log_with_console_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            stand = Path(tmp)
            (stand / "server/logs").mkdir(parents=True)
            (stand / "server/server.out").write_text("console\n")
            self.assertEqual(bench.server_log_lines(stand), ["console"])
            (stand / "server/logs/latest.log").write_text("AI usage {}\n")
            self.assertEqual(bench.server_log_lines(stand), ["AI usage {}"])

    def test_success_generates_once_then_applies_and_captures(self):
        commands = []

        def bot(command, **kwargs):
            commands.append(command)
            if commands.count("workshopai generate") > 1:
                raise AssertionError("successful preview resubmitted")
            return {"stage": "complete", "plan": {}}

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for name in ("set_config_model", "set_mode", "rcon", "hide_hud", "snap"):
                stack.enter_context(patch.object(bench, name))
            stack.enter_context(patch.object(bench.time, "sleep"))
            stack.enter_context(patch.object(bench, "bot", side_effect=bot))
            stack.enter_context(
                patch.object(bench, "bot_full", return_value={"plan": {}})
            )
            stack.enter_context(
                patch.object(bench, "confirmed_microusd", return_value=0)
            )
            stack.enter_context(
                patch.object(bench, "server_log_lines", return_value=[])
            )
            stack.enter_context(
                patch.object(bench, "wait_idle", return_value={"stage": "idle"})
            )
            stack.enter_context(
                patch.object(bench, "wait_stage", return_value={"stage": "preview"})
            )
            record = bench.run_job(
                {
                    "size": [1, 1, 1],
                    "origin": [0, 75, 0],
                    "model": "m",
                    "mode": "fast",
                    "prompt": "tower",
                },
                0,
                Path(tmp),
                Path(tmp),
                {
                    "est_job": 1,
                    "spend_cap": 100,
                    "job_timeout": lambda job: 1,
                    "apply_timeout": 1,
                    "map": "map",
                    "player": "player",
                },
                None,
                {},
            )
        self.assertEqual(record["status"], "preview")
        self.assertEqual(commands.count("workshopai generate"), 1)
        self.assertIn("workshopai apply", commands)
        self.assertIn("workshopai undo", commands)

    def run_job_with_bot(self, tmp, bot):
        with ExitStack() as stack:
            for name in ("set_config_model", "set_mode", "rcon", "hide_hud", "snap"):
                stack.enter_context(patch.object(bench, name))
            stack.enter_context(patch.object(bench.time, "sleep"))
            stack.enter_context(patch.object(bench, "bot", side_effect=bot))
            stack.enter_context(
                patch.object(bench, "bot_full", return_value={"plan": {}})
            )
            stack.enter_context(
                patch.object(bench, "confirmed_microusd", return_value=0)
            )
            stack.enter_context(
                patch.object(bench, "server_log_lines", return_value=[])
            )
            stack.enter_context(
                patch.object(bench, "wait_idle", return_value={"stage": "idle"})
            )
            stack.enter_context(
                patch.object(bench, "wait_stage", return_value={"stage": "preview"})
            )
            return bench.run_job(
                {
                    "size": [1, 1, 1],
                    "origin": [0, 75, 0],
                    "model": "m",
                    "mode": "fast",
                    "prompt": "tower",
                },
                0,
                Path(tmp),
                Path(tmp),
                {
                    "est_job": 1,
                    "spend_cap": 100,
                    "job_timeout": lambda job: 1,
                    "apply_timeout": 1,
                    "map": "map",
                    "player": "player",
                },
                None,
                {},
            )

    def test_throttled_generate_is_resent(self):
        commands = []

        def bot(command, **kwargs):
            commands.append(command)
            if command == "workshopai generate" and commands.count(command) == 1:
                return {"sent": False, "stage": "idle"}
            return {"sent": True, "stage": "complete", "plan": {}}

        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_job_with_bot(tmp, bot)
        self.assertEqual(record["status"], "preview")
        self.assertEqual(commands.count("workshopai generate"), 2)

    def test_unsent_generate_fails_fast(self):
        commands = []

        def bot(command, **kwargs):
            commands.append(command)
            if command == "workshopai generate":
                return {"sent": False, "stage": "idle"}
            return {"sent": True, "stage": "idle"}

        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_job_with_bot(tmp, bot)
        self.assertEqual(record["status"], "no-worker-start")
        self.assertIn("throttle", record.get("error", ""))
        self.assertNotIn("workshopai apply", commands)


class MatrixTest(unittest.TestCase):
    def write(self, raw):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "matrix.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_load_and_validate(self):
        jobs = bench.load_matrix(
            self.write(
                {
                    "defaults": {"mode": "fast", "model": "m/meta"},
                    "jobs": [{"prompt": "tower", "size": [40, 32, 40]}],
                }
            )
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "fast")
        self.assertEqual(jobs[0]["size"], [40, 32, 40])
        self.assertIsNone(jobs[0]["origin"])

    def test_rejects_bad_jobs(self):
        bad = [
            {"jobs": []},
            {"jobs": [{"prompt": "", "size": [1, 1, 1], "model": "m"}]},
            {"jobs": [{"prompt": "x", "size": [0, 1, 1], "model": "m"}]},
            {"jobs": [{"prompt": "x", "size": [1, 1, 129], "model": "m"}]},
            {
                "jobs": [
                    {"prompt": "x", "size": [1, 1, 1], "model": "m", "mode": "turbo"}
                ]
            },
            {"jobs": [{"prompt": "x", "size": [1, 1, 1]}]},
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=str(raw)):
                bench.load_matrix(self.write(raw))

    def test_grid_and_corners(self):
        origin = bench.grid_origin(5, [100, 200], 160, 4)
        self.assertEqual(origin, [100 + 160, None, 200 + 160])
        low, high = bench.select_corners([10, 20, 30], [40, 32, 40])
        self.assertEqual(low, (10, 20, 30))
        self.assertEqual(high, (49, 51, 69))


class SpendTest(unittest.TestCase):
    def test_guard(self):
        self.assertTrue(bench.spend_guard_ok(2_000_000, 600_000, 2_700_000))
        self.assertFalse(bench.spend_guard_ok(2_200_000, 600_000, 2_700_000))
        self.assertTrue(bench.spend_guard_ok(0, 600_000, 600_000))

    def test_bounds_match_server_math(self):
        self.assertEqual(bench.call_bound_microusd(0, 64000), 32205)
        self.assertEqual(bench.call_bound_microusd(0, 8000), 4205)
        self.assertEqual(bench.generation_bound_microusd(), 136858)

    def test_confirmed_reads_ledger(self):
        tmp = Path(tempfile.mkdtemp())
        admin = tmp / "workshop-admin"
        admin.mkdir()
        (admin / "ai-usage.json").write_text(
            json.dumps({"accounts": {"session:a": 100, "other": "x"}}), encoding="utf-8"
        )
        self.assertEqual(bench.confirmed_microusd(tmp), 100)


class LogParseTest(unittest.TestCase):
    def test_usage_line(self):
        receipt = bench.parse_ai_usage(
            '[10:00:01] [x/INFO]: AI usage {"phase":"generation","inputTokens":10,'
            '"outputTokens":20,"costUsd":0.001,"job":"abc"}'
        )
        self.assertEqual(receipt["phase"], "generation")
        self.assertEqual(receipt["job"], "abc")
        self.assertIsNone(bench.parse_ai_usage("no json here"))

    def test_token_totals(self):
        lines = [
            '[03:00:05] [x/INFO]: AI usage {"phase":"generation","inputTokens":100,'
            '"cachedTokens":60,"outputTokens":20,"reasoningTokens":5,"costUsd":0.001}',
            '[03:00:06] [x/INFO]: AI usage {"phase":"repair","inputTokens":50,'
            '"outputTokens":10,"costUsd":0.0005}',
        ]
        metrics = bench.collect_metrics(lines, "job", 0, 10)
        self.assertEqual(
            metrics["tokens"], {"in": 150, "cached": 60, "out": 30, "reasoning": 5}
        )
        self.assertEqual(metrics["tokensByPhase"]["repair"]["cached"], 0)

    def test_call_line(self):
        call = bench.parse_ai_call(
            "[10:00:01] [x/INFO]: AI call phase=polishing mode=fast effort=low maxTokens=16000"
        )
        self.assertEqual(
            call,
            {"phase": "polishing", "mode": "fast", "effort": "low", "maxTokens": 16000},
        )
        self.assertIsNone(bench.parse_ai_call("AI strategy class=OTHER mode=fast"))

    def test_stamps(self):
        self.assertEqual(bench.log_stamp("[01:02:03] hello"), 3723)
        self.assertIsNone(bench.log_stamp("no stamp"))
        self.assertEqual(bench.phase_latency(100, 160), 60)
        self.assertEqual(bench.phase_latency(86390, 10), 20)
        self.assertIsNone(bench.phase_latency(None, 10))

    def test_window_and_quality(self):
        line = "[10:00:05] [x/INFO]: AI call phase=generation mode=fast effort=medium maxTokens=64000"
        self.assertEqual(bench.log_stamp(line), 36005)
        self.assertTrue(bench.in_window(36005, 35900, 36100))
        self.assertFalse(bench.in_window(36005, 100, 200))
        self.assertTrue(bench.in_window(10, 86390, 20))
        self.assertEqual(bench.hms_of(0), 10800)
        quality = bench.parse_ai_quality(
            "[10:00:01] [x/INFO]: AI quality job=abc before="
            '{"cells":1} after={"cells":2}'
        )
        self.assertEqual(quality["after"], {"cells": 2})
        self.assertIsNone(bench.parse_ai_quality("nothing"))

    def test_strategy_volume_and_mode(self):
        lines = [
            "[10:00:01] [x/INFO]: AI strategy class=OTHER mode=fast volume=4096 cells=16384 foo=1",
            "[10:00:02] [x/INFO]: AI call phase=generation mode=fast effort=medium maxTokens=64000",
        ]
        self.assertEqual(bench.parse_strategy_volume(lines), 4096)
        self.assertEqual(bench.parse_generation_mode(lines), "fast")
        self.assertIsNone(bench.parse_strategy_volume(["nothing"]))
        self.assertIsNone(bench.parse_generation_mode(["nothing"]))

    def test_read_ai_fast(self):
        tmp = Path(tempfile.mkdtemp()) / "props"
        self.assertIsNone(bench.read_ai_fast(tmp))
        tmp.write_text("menu.vanilla=true\n", encoding="utf-8")
        self.assertIsNone(bench.read_ai_fast(tmp))
        tmp.write_text("workshop.aiFast=true\n", encoding="utf-8")
        self.assertTrue(bench.read_ai_fast(tmp))


class PlanTest(unittest.TestCase):
    def test_stats(self):
        plan = {
            "operations": [
                {
                    "from": [0, 0, 0],
                    "to": [3, 3, 3],
                    "block": "minecraft:stone",
                    "hollow": True,
                },
                {
                    "from": [0, 0, 0],
                    "to": [1, 0, 0],
                    "block": "minecraft:glass",
                    "hollow": False,
                },
                {
                    "axes": "x",
                    "operations": [
                        {
                            "from": [5, 5, 5],
                            "to": [5, 5, 5],
                            "block": {
                                "mix": [
                                    ["minecraft:stone", 1],
                                    ["minecraft:dirt", 3],
                                ]
                            },
                        },
                    ],
                },
            ]
        }
        stats = bench.plan_stats(plan)
        # 4^3 - 2^3 shell plus a 2-cell bar plus a 1-cell dirt from the mix group.
        self.assertEqual(stats["blocksPlaced"], 56 + 2 + 1)
        self.assertEqual(stats["distinctBlocks"], 3)
        self.assertEqual(stats["topBlocks"][0]["block"], "minecraft:stone")
        self.assertEqual(stats["footprint"], {"min": [0, 0, 0], "max": [5, 5, 5]})

    def test_footprint_classes(self):
        full = {"footprint": {"min": [0, 0, 0], "max": [3, 3, 3]}}
        self.assertEqual(bench.footprint_vs_selection(full, [4, 4, 4]), "exact")
        self.assertEqual(bench.footprint_vs_selection(full, [8, 8, 8]), "inside")
        self.assertEqual(bench.footprint_vs_selection(full, [2, 2, 2]), "overflow")
        self.assertEqual(
            bench.footprint_vs_selection(
                {"footprint": {"min": None, "max": None}}, [2, 2, 2]
            ),
            "empty",
        )


class CameraTest(unittest.TestCase):
    def test_three_shots_differ(self):
        origin, size = [0, 100, 0], [40, 32, 40]
        seen = set()
        for shot in ("a", "b", "c"):
            pos, look = bench.camera_for_plot(origin, size, shot)
            self.assertEqual(look, (20.0, 116.0, 20.0))
            seen.add(tuple(round(v, 1) for v in pos))
            self.assertGreater(pos[1], look[1])
        self.assertEqual(len(seen), 3)

    def test_apply_rate(self):
        self.assertEqual(bench.apply_rate(1000, 10), 100.0)
        self.assertIsNone(bench.apply_rate(1000, 0))


class TariffTest(unittest.TestCase):
    MODELS = [
        "meta/muse-spark-1.3-contributor",
        "anthropic/claude-haiku-5.5",
        "deepseek/deepseek-v4.1-flash-1017",
        "minimax/minimax-m3.1-flash:free",
        "openai/gpt-6-luna",
    ]

    def test_table_matches_openrouter_2026_10_22(self):
        self.assertEqual(
            bench.tariff_for("meta/muse-spark-1.3-contributor"), (0.10, 0.20)
        )
        self.assertEqual(bench.tariff_for("anthropic/claude-haiku-5.5"), (0.10, 0.50))
        self.assertEqual(
            bench.tariff_for("deepseek/deepseek-v4.1-flash-1017"), (0.15, 0.60)
        )
        self.assertEqual(
            bench.tariff_for("minimax/minimax-m3.1-flash:free"), (0.0, 0.0)
        )
        self.assertEqual(bench.tariff_for("openai/gpt-6-luna"), (0.10, 0.50))

    def test_unknown_model_uses_highest_tariff(self):
        self.assertEqual(bench.tariff_for("other/model"), (0.15, 0.60))

    def test_reserve_covers_priciest_matrix_model(self):
        self.assertEqual(bench.reserve_tariff(self.MODELS), (0.15, 0.60))
        self.assertEqual(
            bench.reserve_tariff(["meta/muse-spark-1.3-contributor"]), (0.10, 0.20)
        )

    def test_bounds_follow_tariff(self):
        self.assertEqual(
            bench.call_bound_microusd(0, 64000, tariff=(0.15, 0.60)), 38708
        )
        self.assertEqual(bench.generation_bound_microusd(tariff=(0.15, 0.60)), 195687)
        self.assertEqual(
            bench.call_bound_microusd(0, 64000, tariff=(0.10, 0.20)), 13005
        )
        # Defaults stay the single $0.10/$0.50 test tariff.
        self.assertEqual(bench.call_bound_microusd(0, 64000), 32205)
        self.assertEqual(bench.generation_bound_microusd(), 136858)


class RateLimitTest(unittest.TestCase):
    def test_hits(self):
        lines = [
            "[10:00:01] openrouter 429 on parts",
            "[10:00:02] Rate limit exceeded, backing off",
            "[10:00:03] category=rate_limited",
            "[10:00:04] too many requests",
            "[10:00:05] all clear",
        ]
        self.assertEqual(bench.rate_limit_hits(lines), 4)
        self.assertEqual(bench.rate_limit_hits([]), 0)

    def test_backoff_is_bounded(self):
        self.assertEqual(bench.RATE_LIMIT_BACKOFF_S, (5, 15))
        self.assertEqual(bench.rate_limit_backoff_s(0), 5)
        self.assertEqual(bench.rate_limit_backoff_s(1), 15)
        self.assertIsNone(bench.rate_limit_backoff_s(2))
        self.assertIsNone(bench.rate_limit_backoff_s(-1))
        self.assertLessEqual(
            bench.RATE_LIMIT_MAX_RETRIES, len(bench.RATE_LIMIT_BACKOFF_S)
        )

    def test_classify(self):
        self.assertEqual(
            bench.classify_error(["category=rate_limited"], 2), "rate_limited"
        )
        self.assertEqual(bench.classify_error([], 1), "rate_limited")
        self.assertEqual(
            bench.classify_error(["phase=generation category=auth"], 1), "error"
        )
        self.assertEqual(bench.classify_error(["phase=x category=y"], 0), "error")

    def test_metrics_count_limit_hits(self):
        lines = [
            '[03:00:05] [x/INFO]: AI usage {"phase":"generation","inputTokens":10,'
            '"costUsd":0.001}',
            "[03:00:06] [x/INFO]: AI provider failure: phase=parts category=rate_limited",
        ]
        metrics = bench.collect_metrics(lines, "job", 0, 10)
        self.assertEqual(metrics["rateLimitHits"], 1)
        self.assertEqual(
            bench.classify_error(metrics["failures"], metrics["rateLimitHits"]),
            "rate_limited",
        )


class ReportTest(unittest.TestCase):
    def record(self, **over):
        base = {
            "index": 0,
            "status": "preview",
            "prompt": "p",
            "model": "m",
            "mode": "fast",
            "selection": {"origin": [0, 0, 0], "size": [1, 1, 1]},
            "startLocal": "t",
            "submitToPreviewS": 10.0,
            "metrics": {
                "tokens": {"in": 100, "cached": 60, "out": 20, "reasoning": 5},
                "totalCostUsd": 0.001,
                "callsByPhase": {"generation": 1},
                "latencyByPhaseS": {"generation": [3]},
                "failures": [],
            },
            "plan": {
                "blocksPlaced": 7,
                "distinctBlocks": 2,
                "topBlocks": [],
                "footprintVsSelection": "exact",
            },
        }
        base.update(over)
        return base

    def test_flatten_report_columns(self):
        row = bench.flatten(
            self.record(metrics={**self.record()["metrics"], "failures": ["a", "b"]})
        )
        self.assertEqual(
            (
                row["inputTokens"],
                row["cachedTokens"],
                row["outputTokens"],
                row["reasoningTokens"],
                row["costUsd"],
            ),
            (100, 60, 20, 5, 0.001),
        )
        self.assertEqual(row["totalCostUsd"], 0.001)
        self.assertEqual(row["phaseTimingsS"], '{"generation": [3]}')
        self.assertEqual(row["calls"], '{"generation": 1}')
        self.assertEqual(row["failures"], "a | b")
        self.assertEqual(row["retries"], 2)

    def test_summary_by_model(self):
        records = [
            self.record(
                index=0,
                model="a",
                status="preview",
                submitToPreviewS=10.0,
                metrics={**self.record()["metrics"], "totalCostUsd": 0.001},
            ),
            self.record(
                index=1,
                model="a",
                status="preview",
                submitToPreviewS=30.0,
                metrics={**self.record()["metrics"], "totalCostUsd": 0.003},
            ),
            self.record(
                index=2,
                model="a",
                status="rate_limited",
                submitToPreviewS=None,
                metrics={**self.record()["metrics"], "totalCostUsd": 0.0},
            ),
            self.record(
                index=3,
                model="b",
                status="error",
                submitToPreviewS=5.0,
                metrics={**self.record()["metrics"], "totalCostUsd": 0.002},
            ),
        ]
        summary = bench.summarize_by_model(records)
        self.assertEqual(summary["a"]["jobs"], 3)
        self.assertEqual(summary["a"]["ok"], 2)
        self.assertEqual(summary["a"]["rateLimited"], 1)
        self.assertEqual(summary["a"]["successRate"], round(2 / 3, 3))
        self.assertEqual(summary["a"]["medianWallS"], 20.0)
        self.assertEqual(summary["a"]["medianCostUsd"], 0.001)
        self.assertEqual(summary["b"]["successRate"], 0)
        self.assertEqual(bench.summarize_by_model([]), {})

    def test_save_results_writes_summary(self):
        tmp = Path(tempfile.mkdtemp())
        bench.save_results(tmp, [self.record()])
        summary = json.loads((tmp / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["byModel"]["m"]["jobs"], 1)
        header = (tmp / "results.csv").read_text(encoding="utf-8").splitlines()[0]
        for column in (
            "inputTokens",
            "cachedTokens",
            "outputTokens",
            "reasoningTokens",
            "phaseTimingsS",
            "failures",
        ):
            self.assertIn(column, header)


class PauseTest(unittest.TestCase):
    def test_should_pause(self):
        self.assertTrue(bench.should_pause(2_700_000, 2_700_000))
        self.assertTrue(bench.should_pause(2_800_000, 2_700_000))
        self.assertFalse(bench.should_pause(2_699_999, 2_700_000))

    def test_spend_per_model(self):
        rec = ReportTest.record
        records = [
            rec(self=None, index=0, model="a", metrics={"totalCostUsd": 0.001}),
            rec(self=None, index=1, model="a", metrics={"totalCostUsd": 0.002}),
            rec(self=None, index=2, model="b", metrics={}),
        ]
        self.assertEqual(bench.spend_per_model(records), {"a": 0.003})

    def test_pause_roundtrip_and_resume_filter(self):
        tmp = Path(tempfile.mkdtemp())
        rec = ReportTest.record
        records = [rec(self=None, index=i) for i in range(3)]
        state = bench.write_pause_state(
            tmp, records, [3, 4], 2_700_000, 2_700_000, "cap-reached"
        )
        self.assertEqual(state["completed"], [0, 1, 2])
        self.assertEqual(state["remaining"], [3, 4])
        self.assertEqual(state["spendPerModelUsd"], {"m": 0.003})
        bench.save_results(tmp, records)
        self.assertEqual(bench.load_completed_indices(tmp), {0, 1, 2})
        self.assertEqual(bench.load_completed_indices(Path(tempfile.mkdtemp())), set())


if __name__ == "__main__":
    unittest.main()
