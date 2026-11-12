"""Read Boat allowance and inventory; warn without changing remote resources."""

import argparse
import json
import math
import subprocess
import time
from pathlib import Path


def allowance(limits):
    total = float(limits["subscriptionQuotaSeconds"])
    remaining = float(limits["subscriptionRemainingSeconds"])
    if limits.get("subscriptionStatus") == "trialing":
        trial = float(limits["trialComputeCapSeconds"])
        left = trial - float(limits["trialComputeUsedSeconds"])
        total, remaining = min(total, trial), min(remaining, left)
    if not all(math.isfinite(n) for n in (total, remaining)) or total <= 0:
        raise ValueError("Missing or invalid allowance")
    return total, max(0, min(total, remaining))


def assess(total, remaining, planned_rate, now, previous):
    warnings = []
    fraction = remaining / total
    if fraction <= 0.1:
        warnings.append("Критический остаток Boat: не более 10%")
    elif fraction <= 0.2:
        warnings.append("Низкий остаток Boat: не более 20%")
    rate = None
    if previous:
        elapsed = now - previous["time"]
        used = previous["remainingSeconds"] - remaining
        if elapsed >= 300 and used >= 0:
            rate = used / elapsed
            if rate > previous.get("plannedRate", planned_rate) * 1.25:
                warnings.append("Расход Boat превышает план более чем на 25%")
    return warnings, rate


def read_boat(*args):
    result = subprocess.run(
        ["just", "boat", *args, "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planned-rate", type=float)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        limits = dict(
            subscriptionQuotaSeconds=2000000,
            subscriptionRemainingSeconds=1999574,
            subscriptionStatus="trialing",
            trialComputeCapSeconds=90000,
            trialComputeUsedSeconds=426,
        )
        assert allowance(limits) == (90000, 89574)
        limits["trialComputeUsedSeconds"] = 91000
        assert allowance(limits) == (90000, 0)
        limits["subscriptionStatus"] = "active"
        assert allowance(limits) == (2000000, 1999574)
        assert "20%" in assess(100, 20, 1, 0, None)[0][0]
        assert "10%" in assess(100, 10, 1, 0, None)[0][0]
        previous = {"time": 0, "remainingSeconds": 10000}
        assert assess(20000, 8800, 4, 300, previous) == ([], 4)
        assert assess(20000, 8200, 4, 300, previous)[1] == 6
        assert assess(20000, 8200, 4, 300, previous)[0]
        assert assess(20000, 8200, 4, 299, previous)[1] is None
        previous["plannedRate"] = 4
        assert assess(20000, 8800, 1, 300, previous) == ([], 4)
        print("PASS trial cap, paid allowance, warning levels and planned burn")
        return
    if args.planned_rate is None or not math.isfinite(args.planned_rate):
        parser.error("--planned-rate must be a finite nonnegative number")
    if args.planned_rate < 0:
        parser.error("--planned-rate must be nonnegative")
    if not Path("docs/BOAT.md").is_file():
        parser.error("Run from the Blockfield workspace root")
    try:
        status = read_boat("status")
        limits = read_boat("limits")
        inventory = read_boat("list", "--all")
        total, remaining = allowance(limits)
        key = [
            status["account"]["identifier"],
            limits.get("subscriptionCurrentPeriodEnd"),
            limits.get("subscriptionStatus"),
            total,
        ]
        path = Path(".dev/boat-budget.json")
        previous = json.loads(path.read_text()) if path.exists() else None
        if previous and previous.get("key") != key:
            previous = None
        now = time.time()
        warnings, rate = assess(total, remaining, args.planned_rate, now, previous)
        if not limits.get("canStart"):
            warnings.append(
                "Boat запрещает новые запуски: " + str(limits.get("blockedReason"))
            )
        sample = dict(
            key=key, time=now, remainingSeconds=remaining, plannedRate=args.planned_rate
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sample, indent=2) + "\n")
        temporary.replace(path)
        print(
            json.dumps(
                dict(
                    remainingDefaultMinutes=round(remaining / 60, 2),
                    remainingPercent=round(100 * remaining / total, 2),
                    plannedRate=args.planned_rate,
                    intervalPlannedRate=previous.get("plannedRate")
                    if previous
                    else None,
                    measuredRate=rate,
                    rateWindow="Since previous sample; needs at least 5 minutes",
                    warnings=warnings,
                    inventoryComplete=not inventory.get("pageInfo", {}).get(
                        "hasMore", False
                    ),
                    vms=[
                        {
                            k: vm.get(k)
                            for k in ("id", "state", "type", "name", "environment")
                        }
                        for vm in inventory["sandboxes"]
                    ],
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise SystemExit(
            "Boat accounting unavailable; do not assume allowance or cleanup is safe"
        )


if __name__ == "__main__":
    main()
