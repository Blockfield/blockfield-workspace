"""Port of CombatSceneClock's arrival + servo branch: which frame rates starve the servo?"""

import math
import random
import statistics


def servo(occ, delay, dil=0.1):
    if delay <= occ <= delay + 1:
        return 1.0
    err = occ - (delay + 0.5)
    return 1 + math.copysign(min(dil, 0.05 * abs(err)), err)


class Clock:
    def __init__(s, fix=False):
        s.fix = fix
        s.high = -1
        s.smooth = 0
        s.jit = 0
        s.time = None
        s.rate = 1.0
        s.next = 0.0
        s.batches = 0
        s.delay = 3
        s.last = None
        s.backlog = False
        s.fired = 0
        s.slew = -1e18
        s.missed = False

    def arrival(s, tick, now):
        dev = now - tick * 50
        s.backlog = s.batches > 0 and dev - s.smooth >= (50 if s.backlog else 150)
        s.high = tick
        if s.backlog:
            return
        if s.batches == 0:
            s.smooth = dev
            s.delay = 3
        else:
            s.smooth += 0.1 * (dev - s.smooth)
            s.jit += 0.1 * (abs(dev - s.smooth) - s.jit)
        s.batches += 1
        if s.fix and s.next != math.inf and s.batches > 1:
            s.missed = True
        s.next = s.smooth + (s.high + 0.5) * 50
        if now - s.slew >= 500 and abs(s.rate - 1) <= 0.05000001:
            want = max(
                2, min(6, 3 if s.batches < 20 else 2 + math.ceil(s.jit / 50 * 1.5))
            )
            if s.delay != want:
                s.delay += (want > s.delay) - (want < s.delay)
            s.slew = now

    def frame(s, now):
        dt = 0 if s.last is None else max(0, now - s.last)
        s.last = now
        if s.time is None:
            s.time = s.high - s.delay
            return
        if s.fix and s.missed:
            # The slot passed between frames: sample as of that slot, at this frame's phase.
            s.rate = servo(s.high - s.time + (now - s.next) / 50, s.delay)
            s.missed = False
            s.next = math.inf
            s.fired += 1
        elif now >= s.next:
            s.rate = servo(s.high - s.time, s.delay)
            s.next = math.inf
            s.fired += 1
        s.time = min(s.high, s.time + s.rate * dt / 50)


def run(period, jit, seed, fix, secs=60, net=2.0, start_occ=None):
    r = random.Random(seed)
    c = Clock(fix)
    tick = 1000
    nextarr = tick * 50.0
    t = nextarr + r.uniform(0, 50)
    occ = []
    rates = []
    end = t + secs * 1000
    while t < end:
        t += max(1, period + r.uniform(-jit, jit))
        while (
            nextarr + r.uniform(0, net) <= t + 0
        ):  # everything that arrived is drained at frame start
            c.arrival(tick, t)
            tick += 1
            nextarr = tick * 50.0
        if c.high < 0:
            continue
        c.frame(t)
        if c.batches > 25:
            occ.append(c.high - c.time)
            rates.append(c.rate)
    return occ, rates, c.fired, tick - 1000


if __name__ == "__main__":
    print(
        "period fps | stock: occ median p95 max finalRate servoFired/ticks | fixed: occ median p95 max servoFired/ticks  (worst of 8 seeds by max occ)"
    )
    for period, jit in (
        (7, 1),
        (16.7, 2),
        (25, 3),
        (33, 4),
        (40, 5),
        (45, 5),
        (50, 0.2),
        (50, 4),
        (52, 5),
        (54, 5),
        (58, 6),
        (66, 8),
        (83, 10),
        (100, 12),
    ):
        out = []
        for fix in (False, True):
            best = None
            for seed in range(8):
                occ, rates, fired, ticks = run(period, jit, seed, fix)
                q = sorted(occ)
                row = (
                    q[-1],
                    statistics.median(occ),
                    q[int(0.95 * len(q))],
                    rates[-1],
                    fired,
                    ticks,
                )
                if best is None or row[0] > best[0]:
                    best = row
            out.append(best)
        a, b = out
        print(
            f"{period:5} {1000 / period:5.0f} | {a[1]:6.2f} {a[2]:6.2f} {a[0]:6.2f} rate {a[3]:.3f} fired {a[4]}/{a[5]} | {b[1]:5.2f} {b[2]:5.2f} {b[0]:5.2f} fired {b[4]}/{b[5]}"
        )
