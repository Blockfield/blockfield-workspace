#!/usr/bin/env python3
"""First-shot tap-during-hold: a tap while the draw hold is active must fire once.

A fresh draw arms the client draw guard (FirstShotDrawGuard): a `tacz shoot`
during the hold reports IS_DRAWING and is queued, then replays exactly once
when the server draw is observed. Lost (ammo never drops) or doubled (ammo
drops twice) is a FAIL. A SUCCESS on the immediate tap means the draw already
settled, so the trial did not cover the hold and is retried once.
"""

import time

from .common import Bot, check, connect_rcon, dimension, rcon_args, tally
from .range import DIM as RANGE_DIM
from .range import enter as range_enter

SCENARIO = {
    "name": "first-shot",
    "help": "tap-during-hold: queued tap fires once, never lost nor doubled",
}
GUN = "tacz:modern_kinetic_gun"


def ammo_count(rcon, player, slot=0):
    from .common import number

    path = (
        f"Inventory[{{Slot:{slot}b}}].components."
        '"minecraft:custom_data".GunCurrentAmmoCount'
    )
    try:
        return number(rcon.cmd(f"data get entity {player} {path}"))
    except AssertionError:
        return None


def wait_drawn(bot, timeout=12.0):
    """TaCZ draw settled: no state lock and no draw cooldown (see weapons_sweep)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = bot("tacz status") or {}
        except (OSError, RuntimeError):
            return
        if not status.get("stateLock") and not status.get("drawCooldownMs"):
            return
        time.sleep(0.5)


def tap_trial(rcon, bot, player, index):
    """One fresh draw + immediate tap. Returns (ok, evidence)."""
    before = ammo_count(rcon, player)
    if before is None:
        return False, f"trial {index}: ammo unreadable (kit grant broken)"
    if before < 5:
        bot("tacz reload")
        time.sleep(2.0)
        before = ammo_count(rcon, player)
        if before is None:
            return False, f"trial {index}: ammo unreadable after reload"
    bot("slot 1")
    time.sleep(0.2)
    bot("slot 0")
    result = (bot("tacz shoot") or {}).get("result")
    status = {}
    try:
        status = bot("tacz status") or {}
    except (OSError, RuntimeError):
        pass
    tag = (
        f"trial {index}: tap={result} ammo={before:g}"
        f" drawTs={status.get('drawTimestamp')} cool={status.get('drawCooldownMs')}"
    )
    if result == "SUCCESS":
        # Draw already settled: the tap did not cover the hold. One retry
        # with a tighter draw->tap gap; a second SUCCESS is still a PASS for
        # the first shot, but reports no-hold so the battery cannot mistake
        # it for tap coverage.
        time.sleep(0.5)
        after = ammo_count(rcon, player)
        ok = after is not None and after == before - 1
        return ok, tag + f" no-hold ammo->{after} (draw settled before tap)"
    if result != "IS_DRAWING":
        return False, tag + f" unexpected (want IS_DRAWING, got {result})"
    deadline = time.monotonic() + 6.0
    fired = None
    while time.monotonic() < deadline:
        time.sleep(0.3)
        fired = ammo_count(rcon, player)
        if fired is not None and fired < before:
            break
    if fired is None or fired >= before:
        return False, tag + f" LOST: ammo never dropped ({before:g}->{fired})"
    if before - fired > 1:
        return False, tag + f" DOUBLE: ammo {before:g}->{fired:g} on one tap"
    time.sleep(2.0)
    settled = ammo_count(rcon, player)
    if settled is not None and settled < fired:
        return False, tag + f" DOUBLE: replay fired twice ({before:g}->{settled:g})"
    return True, tag + f" queued tap fired once ({before:g}->{fired:g})"


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    out = []
    try:
        if dimension(bot("state")) != RANGE_DIM and not range_enter(bot):
            out.append(check("range entry", False, dimension(bot("state"))))
            return tally(out) and 1 or 0
        bot("bfaction class:assault:0")
        time.sleep(1)
        bot("slot 0")
        wait_drawn(bot)
        state = bot("state")
        if state.get("held") != GUN:
            out.append(check("kit gun equipped", False, str(state.get("held"))))
            return tally(out) and 1 or 0
        held = covered = lost = doubled = 0
        for index in range(1, args.trials + 1):
            ok, detail = tap_trial(rcon, bot, state["name"], index)
            out.append(check(f"tap-during-hold {index}/{args.trials}", ok, detail))
            if "no-hold" in detail:
                held += 1
            elif ok:
                covered += 1
            elif "LOST" in detail:
                lost += 1
            else:
                doubled += 1
            time.sleep(0.5)
        summary = (
            f"{covered} queued-fired, {held} no-hold, {lost} lost, {doubled} doubled"
        )
        required = max(1, (args.trials + 1) // 2)  # zero/negative trials prove nothing
        if covered < required:
            summary += (
                f" - no coverage: only {covered}/{args.trials} tapped"
                f" during hold (need >= {required})"
            )
        out.append(
            check(
                "hold coverage, zero lost, no doubles",
                lost == 0 and doubled == 0 and covered >= required,
                summary,
            )
        )
        return tally(out) and 1 or 0
    finally:
        bot.close()


def register(sub):
    parser = sub.add_parser("first-shot", help=SCENARIO["help"])
    parser.add_argument("--trials", type=int, default=12)
    rcon_args(parser)
    parser.set_defaults(func=run)
