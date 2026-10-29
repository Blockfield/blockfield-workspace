"""Summarize blockfield-speedtest.txt files: python3 -I scripts/speedtest/report.py FILE...

One file per tester; testers with VPN on are left out. Speed is a target's best pass, while any
stalled or failed pass counts: a host that breaks every other try is no primary source.
"""

import statistics
import sys
from collections import defaultdict

SLICE = 32 << 20  # the 0-33554431 range every pass requests


def verdict(code, size, exit_code):
    if exit_code == 0 and code in (200, 206) and size >= SLICE:
        return "ok"
    if exit_code == 28 and size >= 1 << 20:
        return "slow"  # hit --max-time mid-slice: the measured speed is still real
    if exit_code == 28 and size > 0:
        return "stalled"  # under 1 MiB in 45 s: the Russian cut of Cloudflare stops near 16 KB
    return "failed"


def number(text):
    return float(text.replace(",", ".") or 0)


def tester(text):
    """(meta, {target: (best Mbit/s or None, set of pass verdicts)}) of one result file."""
    meta, passes = {}, defaultdict(list)
    for line in text.splitlines():
        fields = line.strip().split(";")
        if fields[0] != "BF1" or len(fields) < 3:
            continue
        if fields[1] == "meta":
            meta = dict(f.split("=", 1) for f in fields[2:] if "=" in f)
        elif len(fields) >= 11 and fields[10].startswith("exit="):
            target, _, code, size, speed = fields[1:6]
            result = verdict(int(number(code)), number(size), int(fields[10][5:] or -1))
            passes[target].append((result, number(speed) * 8 / 1e6))
    best = {}
    for target, results in passes.items():
        good = [mbit for result, mbit in results if result in ("ok", "slow")]
        best[target] = (max(good, default=None), {result for result, _ in results})
    return meta, best


def row(*cells):
    print("| " + " | ".join(map(str, cells)) + " |")


def main(paths):
    groups = defaultdict(lambda: defaultdict(list))
    skipped = 0
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as handle:
            meta, best = tester(handle.read())
        if meta.get("vpn") != "no":
            skipped += 1
            continue
        group = groups[meta.get("country", "?"), meta.get("conn", "?")]
        for target, outcome in best.items():
            group[target].append(outcome)
    header = (
        "country",
        "connection",
        "target",
        "testers",
        "median Mbit/s",
        "worst Mbit/s",
        "testers with a stall",
        "testers with a failure",
    )
    row(*header)
    row(*["---"] * len(header))
    for (country, conn), targets in sorted(groups.items()):
        for target, outcomes in sorted(targets.items()):
            speeds = [mbit for mbit, _ in outcomes if mbit is not None]
            row(
                country,
                conn,
                target,
                len(outcomes),
                f"{statistics.median(speeds) if speeds else 0:.1f}",
                f"{min(speeds, default=0):.1f}",
                sum("stalled" in results for _, results in outcomes),
                sum("failed" in results for _, results in outcomes),
            )
    if skipped:
        print(f"\n{skipped} file(s) skipped: VPN was on or the answer is missing.")


if __name__ == "__main__":
    main(sys.argv[1:])
