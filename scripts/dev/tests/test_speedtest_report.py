import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "speedtest"))
import report  # noqa: E402

# Windows output: CRLF, and the Cloudflare cut that stalls near 16 KB.
SAMPLE = (
    "BF1;meta;country=RU;conn=home;vpn=no;isp=Rostelecom;os=windows\r\n"
    "BF1;home;1;206;33554432;4194304.000;0.05;0.10;8.0;212.8.46.110;exit=0\r\n"
    "BF1;r2;1;000;16384;364.000;0.04;0.20;45.0;172.67.133.218;exit=28\r\n"
    "BF1;github;1;206;8388608;186413.000;0.06;0.30;45.0;185.199.109.133;exit=28\r\n"
    "BF1;github;2;000;0;0.000;0.00;0.00;15.0;;exit=6\r\n"
    "BF1;r2;2;000;16384;364.000;0.04;0.20;45.0;172.67.133.218;exit=28\r\n"
    "BF1;home;2;206;33554432;2097152.000;0.05;0.10;16.0;212.8.46.110;exit=0\r\n"
)


class SpeedtestReportTest(unittest.TestCase):
    def test_best_pass_gives_speed_and_every_pass_counts_breaks(self):
        meta, best = report.tester(SAMPLE)
        self.assertEqual(meta["country"], "RU")
        self.assertEqual(best["home"], (4194304 * 8 / 1e6, {"ok"}))
        # The slow pass gives the speed, the failed one is still reported.
        self.assertEqual(best["github"], (186413 * 8 / 1e6, {"slow", "failed"}))
        self.assertEqual(best["r2"], (None, {"stalled"}))

    def test_a_full_slice_needs_a_clean_exit(self):
        self.assertEqual(report.verdict(206, report.SLICE, 0), "ok")
        self.assertEqual(report.verdict(206, report.SLICE, 56), "failed")
        self.assertEqual(report.verdict(404, 150, 0), "failed")


if __name__ == "__main__":
    unittest.main()
