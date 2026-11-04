#!/usr/bin/env python3
"""Knife acceptance: server-authoritative swings and thrusts, live.

Drives LR's own swing intake (`lr attack <left|right>`, the same
triggerIfReady path as the press edge) and scores every leg against a
freshly marked server kill line or a same-life HP decrease — never a delayed
HP subtraction across a death/respawn, which misreads a lethal hit followed
by the instant respawn as negative damage (rc-triage).

Fixture per leg (retrained after every death, never reused from a death
lobby): both bots redeployed into the arena, shooter holding the tagged
dagger (class re-pick if the kit was wiped), victim alive, both at the same
feet Y on walkable in-bounds ground (shooter teleports are clamped inside
the arena rect; an out-of-bounds spot kills the shooter by genericKill and
silently voids every later leg), victim rendered by the shooter, victim
vulnerable (RCON probe). The aim is a true crosshair offset: the shooter
stands dead-ahead and looks `aim_off` degrees past the victim, so the
off-axis legs really test cone coverage instead of the approach direction.

Labels follow the product spec: `lr attack left` drives LR MeleeAction.LEFT,
which the server maps to LIGHT (KnifeSwingMixin), and `right` to HEAVY.
Server shapes (mod origin/main KnifeCombatRules.Attack, mod#276 — read from
origin/main, never the workspace pin, which predates it): LIGHT is a SWING,
reach 2.4 m / half-angle 20 deg; HEAVY is a THRUST, reach 2.8 m /
half-angle 8 deg. shapeFor(clip) picks the shape from the playing clip:
melee_1/melee_2 sweep wide and short, melee_3 stabs narrow and far; LEFT
alternates melee_2/melee_1 by accept count, RIGHT always plays melee_3.
Nominal frontal damage pre-part-multiplier is LIGHT 7 / HEAVY 13 (both
non-lethal on 20 HP, so a full-HP victim never needs a revive between legs
for lethality reasons).

Expected legs: dead-ahead swings and thrusts at 2.0 m hit (inside both
reaches); a LIGHT swing with a 15 deg aim offset hits inside the 20 deg
server cone (no client-cone rescue needed); the 2.9 m pair discriminates
the reaches at one distance — the swing misses past LIGHT 2.4 m (even the
~0.2 m eye-ray face margin only stretches it to ~2.6), the thrust lands
inside HEAVY 2.8 m plus that margin (~3.0, marginal by ~0.1 m); a HEAVY
thrust with a 35 deg aim offset misses far outside its 8 deg cone. Any
failed expected hit, any hit on a must-miss leg, and any inconclusive
fixture fail the battery (nonzero exit).
"""

import math
import re
import time

from pathlib import Path

from .common import (
    Bot,
    check,
    connect_rcon,
    dimension,
    rcon_args,
    settle_after_tp,
    tally,
    wait_for,
)
from . import duo as _duo
from . import readiness as _readiness
from .dm import arena_bounds
from .rc import deploy

SCENARIO = {
    "name": "knife",
    "help": "knife acceptance: swings and thrusts, kill-line verdicts",
}
MELEE = "lrtactical:melee"

# (key, side, dist, aim_off_deg, expect_hit, nominal_damage). Aim offsets are
# true crosshair offsets: the shooter stands dead-ahead and looks past the
# victim. Nominal damage is the frontal pre-part-multiplier value
# (KnifeCombatRules.damage); parts scale it, so the verdict checks the sign
# of the HP move, never the exact number.
LEGS = (
    ("swing-ahead-2m", "left", 2.0, 0, True, 7),
    ("thrust-ahead-2m", "right", 2.0, 0, True, 13),
    ("swing-offaxis-15deg-2m", "left", 2.0, 15, True, 7),
    ("swing-ahead-2.9m", "left", 2.9, 0, False, 0),
    ("thrust-ahead-2.9m", "right", 2.9, 0, True, 13),
    ("thrust-offaxis-35deg-2m", "right", 2.0, 35, False, 0),
)

# Reach / half-angle per playing clip (mod origin/main shapeFor): the swing
# clips share the LIGHT cone, the thrust clip stabs narrow and far.
CLIP_SHAPE = {
    "melee_1": (2.4, 20.0),
    "melee_2": (2.4, 20.0),
    "melee_3": (2.8, 8.0),
}
# Attack side -> clips the server can play for it: LEFT alternates
# melee_2/melee_1 by accept count (clipFor), RIGHT always plays melee_3.
SIDE_CLIPS = {"left": ("melee_2", "melee_1"), "right": ("melee_3",)}

# The impact lands 2/4 ticks after the click, but a stalled stand resolves
# it later; score only once the strike's effect is visible (kill line or HP
# move) or the window lapses, so a late impact never reads as a miss.
STRIKE_OBSERVE_SECONDS = 10.0
STRIKE_POLL_SECONDS = 0.5


def server_log_path():
    import os

    return Path(os.environ.get("BF_DEV_HOME", ".dev/knife"), "server", "server.out")


def get_log():
    path = server_log_path()
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def knife_in_hand(shooter, timeout=20):
    """Slot 2 must hold the tagged dagger; a respawn wipes the kit, so one
    class re-pick is allowed before giving up on the leg."""
    shooter("slot 2")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if shooter("state").get("held") == MELEE:
            return True
        time.sleep(1)
    shooter("bfaction class:assault:0")
    shooter("slot 0")
    time.sleep(3)
    shooter("slot 2")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if shooter("state").get("held") == MELEE:
            return True
        time.sleep(1)
    return False


def revive_victim(victim, timeout=30):
    """Instant respawn does not always fire on its own: request it when the
    victim is dead and wait for positive health. Returns False on timeout."""
    try:
        state = victim("state")
    except (OSError, RuntimeError):
        state = {}
    if (state.get("health") or 0) > 0 and (state.get("screen") or "") != "DeathScreen":
        return True
    try:
        victim("respawn")
    except (OSError, RuntimeError):
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = victim("state")
        except (OSError, RuntimeError):
            time.sleep(1)
            continue
        if (state.get("health") or 0) > 0:
            return True
        time.sleep(1)
    return False


def deployed(bot):
    """True when the bot client reports a live match deploy.

    An instant respawn can leave a healthy victim undeployed in the same
    arena dimension (respawn waiting area), so dimension alone is not proof.
    Falls back to True when the markers probe is unavailable, keeping the
    old dimension-only behavior on older testbots."""
    try:
        markers = bot("markers")
    except (OSError, RuntimeError):
        return True
    if not isinstance(markers, dict):
        return True
    return bool(markers.get("deployed", True))


def redeploy(bot, arena, timeout=30):
    """A leg needs both bots deployed in the arena: a spawn refused while the
    room was still forming, or a respawn wait after a lethal leg, leaves them
    in-arena with tablets otherwise. Offers spawn:BASE (a no-op once
    deployed) until deployment and the fresh kit are confirmed: the respawn
    waiting room shares the dimension, and its first UI packet can be stale."""
    deadline = time.monotonic() + timeout
    if not revive_victim(bot, timeout=timeout):
        return False
    bot("slot 0")
    recovered = False
    while time.monotonic() < deadline:
        try:
            state = bot("state")
            is_deployed = dimension(state) == arena and deployed(bot)
            if is_deployed and state.get("held") == _duo.GUN:
                return True
            if is_deployed and state.get("held") != _duo.GUN and not recovered:
                # spawn:BASE cannot restore an already-deployed player's wiped kit.
                recovered = True
                bot("bfaction class:assault:0")
                bot("slot 0")
        except (OSError, RuntimeError):
            pass
        try:
            bot("bfaction spawn:BASE")
        except (OSError, RuntimeError):
            pass
        time.sleep(0.5)
    return False


def place(shooter, victim, rcon, arena, dist, aim_off_deg, bounds=None):
    """Place the shooter dead-ahead at dist from the victim,
    on the same feet Y (flat geometry), and looks `aim_off_deg` past the
    victim's chest. Both stay one block inside the boundary; collision or
    probe knockback must not push an edge placement out of the match."""
    v = victim("state")
    x, y, z = v["pos"]
    vn, sn = v["name"], shooter("state")["name"]
    if bounds is not None:
        min_x, max_x, min_z, max_z = bounds
        x = min(max(x, min_x + 1), max_x - dist)
        z = min(max(z, min_z + 1), max_z)
    sx, sz = x + dist, z
    settle_after_tp(victim, rcon, vn, x, y, z, yaw=90)
    settled = settle_after_tp(shooter, rcon, sn, sx, y, sz)
    # Let vanilla observe the tp rotation before replaying the previous leg's exact aim;
    # otherwise its unchanged-rotation cache can suppress the new look packet forever.
    tick = settled.get("tick")
    if (
        tick is None
        or not wait_for(
            lambda remaining: shooter("state", timeout=remaining),
            lambda state: state.get("tick", tick) > tick,
            timeout=5,
        )[0]
    ):
        return False
    dx, dz = x - sx, z - sz
    ang = math.atan2(dz, dx) + math.radians(aim_off_deg)
    ax = sx + math.cos(ang) * dist
    az = sz + math.sin(ang) * dist
    shooter(f"lookat {ax} {y + 1.2} {az}")
    time.sleep(1.0)
    if not _duo.rendered(shooter, radius=dist + 16, timeout=10):
        return False
    time.sleep(3.0)  # cooldowns: light 12t / heavy 26t + margin
    return True


def died_since(victim_name, mark):
    """Kill lines for the victim after a log mark (any killer): the RCON vuln
    probe chips 1 HP, so on a 1-HP victim the probe itself is lethal and the
    instant respawn hides it behind fresh full HP and fresh protection."""
    death_re = re.compile(re.escape(victim_name) + r" убит ")
    return any(death_re.search(line) for line in get_log().splitlines()[mark:])


def strike(shooter, victim, rcon, arena, side):
    """One swing; returns (killed, hp_before, hp_after, clip, rejected,
    life_before, life_after). The
    kill window opens here, so only this swing's server kill line can count
    (never the vuln probe's); the same window also carries the server
    intake's rejection line. hp values are None when the victim state is
    unreadable — never a fabricated 0. clip is the playing animation when
    the testbot exposes it, else None (the expected clips come from
    SIDE_CLIPS). rejected is True when the swing never executed: the
    server intake refused it (`knife swing rejected`, exhaustion or stale
    timeline), or execution cannot be confirmed by the reply or damage.
    life_before/after are the victim's kill-line counts around the window: the
    verdict joins the attack to the target life, so an HP delta across a
    respawn is never scored as damage."""
    sn, vn = shooter("state")["name"], victim("state")["name"]
    # Life snapshot precedes the HP baseline: a death+respawn between them then
    # shows up in the window's kill lines and the leg cannot pass on old HP.
    before = get_log().splitlines()
    mark = len(before)
    life_before = _readiness.kill_count("\n".join(before), vn)

    def server_hp():
        # Authoritative: a stalled client may still show pre-probe or pre-regen HP.
        try:
            reply = rcon.cmd(f"execute in {arena} run data get entity {vn} Health")
        except (OSError, RuntimeError):
            return None
        value = re.search(r": ([-+]?\d+(?:\.\d+)?)f?\s*$", reply or "")
        return float(value[1]) if value else None

    hp_before = server_hp()
    kill_re = re.compile(
        _readiness.name_token(vn) + r" убит " + _readiness.name_token(sn)
    )
    reject_re = re.compile(r"knife swing rejected.*" + re.escape(sn) + r"\b")
    reply = shooter(f"lr attack {side}")
    # A stalled stand resolves the impact after a fixed sleep; poll until
    # the strike's effect is visible (kill line, intake rejection, or HP
    # drop) or the window lapses, so a late impact never scores as a miss.
    # Only a server-side drop below the server baseline ends the window early
    # (a rise is regen and keeps polling). An HP move rescans once, as
    # the kill line may land between the scan and the read.
    deadline = time.monotonic() + STRIKE_OBSERVE_SECONDS
    lines = []
    killed = False
    rejections = []
    rejected = isinstance(reply, dict) and reply.get("accepted") is False
    observed = None  # the server drop that ended the window, kept for the verdict
    hp_ref = hp_before  # same-life high-water mark: regen must not hide a hit
    for _ in range(int(STRIKE_OBSERVE_SECONDS / STRIKE_POLL_SECONDS)):
        time.sleep(STRIKE_POLL_SECONDS)
        lines = get_log().splitlines()[mark:]
        killed = any(kill_re.search(line) for line in lines)
        rejections = [line for line in lines if reject_re.search(line)]
        rejected = rejected or bool(rejections)
        if killed or rejected:
            break
        hp_now = server_hp()
        if hp_now is not None and hp_ref is not None and hp_now > hp_ref:
            hp_ref = hp_now
        if not (hp_now is not None and hp_ref is not None and hp_now < hp_ref):
            if time.monotonic() >= deadline:
                break
            continue
        observed = hp_now
        # The impact may have landed between the scan and the read; rescan
        # once so a lethal hit joins its kill line instead of scoring the
        # death as a respawn crossing.
        lines = get_log().splitlines()[mark:]
        killed = any(kill_re.search(line) for line in lines)
        rejections = [line for line in lines if reject_re.search(line)]
        rejected = rejected or bool(rejections)
        break
    life_after = life_before + _readiness.kill_count("\n".join(lines), vn)
    # Server health for the verdict too: a stalled client can lag the drop.
    # An observed drop wins: a later timeout or heal must not erase it.
    hp_after = observed if observed is not None else server_hp()
    # A drop below the high-water mark is judged against it, so 13 -> 20
    # (regen) -> 13 is a hit; a regen-only rise keeps the original baseline
    # and stays inconclusive.
    if hp_after is not None and hp_ref is not None and hp_after < hp_ref:
        hp_before = hp_ref
    # Bracket the final observation: a death plus instant respawn between the
    # kill scan and this read must not pose as the same life. Count the full
    # log directly: life_before already counts the pre-window lines, so adding
    # another whole-log count would double-count earlier deaths.
    life_end = _readiness.kill_count("\n".join(get_log().splitlines()), vn)
    # Current testbots echo the requested side even when the client gate
    # drops it; that echo alone cannot prove a must-miss swing executed.
    confirmed = (isinstance(reply, dict) and reply.get("accepted") is True) or (
        killed
        or (hp_before is not None and hp_after is not None and hp_after < hp_before)
    )
    rejected = rejected or not confirmed
    clip = reply.get("clip") if isinstance(reply, dict) else None
    print(
        f"STRIKE {side}: killed={killed} rejected={rejected} hp {hp_before}->{hp_after}"
        f" clip={clip or '?'} expect one of {','.join(SIDE_CLIPS[side])}"
        f" reply={reply} server_rejections={rejections}",
        flush=True,
    )
    return (
        killed,
        hp_before,
        hp_after,
        clip,
        rejected,
        life_before,
        life_after,
        life_end,
    )


def leg(
    shooter, victim, rcon, arena, bounds, key, side, dist, aim_off, expect_hit, nominal
):
    """One acceptance leg: fixture, strike, verdict. Returns True/False/None
    (pass / fail / inconclusive fixture)."""
    for _ in range(2):
        if not redeploy(shooter, arena) or not redeploy(victim, arena):
            print(f"LEG {key}: INCONCLUSIVE redeploy failed", flush=True)
            return None
        if not knife_in_hand(shooter):
            print(f"LEG {key}: INCONCLUSIVE shooter kitless", flush=True)
            return None
        try:
            sn, vn = shooter("state")["name"], victim("state")["name"]
        except (OSError, RuntimeError):
            print(f"LEG {key}: INCONCLUSIVE state unreadable", flush=True)
            return None
        probe_mark = len(get_log().splitlines())
        if not _readiness.probe_vulnerable(rcon, arena, vn, sn, timeout=30):
            print(f"LEG {key}: INCONCLUSIVE victim never vulnerable", flush=True)
            return None
        try:
            hp = victim("state")["health"]
        except (OSError, RuntimeError):
            continue
        if hp <= 0:
            continue  # the probe killed a 1-HP victim; retry revived
        if died_since(vn, probe_mark):
            # The probe was lethal and the instant respawn already brought
            # the victim back with fresh protection: the fixture (placement
            # and vulnerability) must be rebuilt before striking.
            print(f"LEG {key}: probe killed victim; rebuilding fixture", flush=True)
            continue
        # The attributed probe knocks the victim back. Place only after its
        # hurt ticks clear, otherwise a nominal 2m swing can strike at 2.96m.
        if not place(shooter, victim, rcon, arena, dist, aim_off, bounds):
            print(
                f"LEG {key}: INCONCLUSIVE placement, client tick or render not ready",
                flush=True,
            )
            return None
        break
    else:
        print(f"LEG {key}: INCONCLUSIVE fixture never settled", flush=True)
        return None
    try:
        sp, vp = shooter("state")["pos"], victim("state")["pos"]
    except (OSError, RuntimeError):
        sp, vp = None, None
    if sp and vp:
        try:
            sep = math.dist((sp[0], sp[2]), (vp[0], vp[2]))
        except (TypeError, IndexError):
            sep = None
    else:
        sep = None
    meas = f"{sep:.2f}m" if sep is not None else "unknown"
    print(
        f"LEG {key}: side={side} dist={dist} aim_off={aim_off} meas_sep={meas}"
        f" clips={','.join(SIDE_CLIPS[side])}"
        f" shooter={sp} victim={vp}",
        flush=True,
    )
    if sep is None or abs(sep - dist) > 0.1 or abs(sp[1] - vp[1]) > 0.1:
        print(f"LEG {key}: INCONCLUSIVE placement drift", flush=True)
        return None
    # Final fail-closed snapshot of every readiness field (placement rendered
    # the victim and the probe loop proved vulnerability just above, so both
    # are attested; the rest is re-measured fresh): a kit wiped or a dimension
    # changed between the fixture and the strike must not score as a product
    # hit or miss.
    ready, record, missing = _readiness.check_ready(
        shooter, victim, rcon, get_log, arena, MELEE, los=True, vulnerable=True
    )
    if not ready:
        print(f"LEG {key}: INCONCLUSIVE not ready: {missing}", flush=True)
        return None
    # A lethal hit followed by the instant respawn reads HP 20->20, so the
    # kill line joined to this attack's life decides lethals and a same-life
    # HP drop decides the rest; regen rises and respawn crossings never score.
    reach = max(CLIP_SHAPE[c][0] for c in SIDE_CLIPS[side]) + 0.2
    struck = strike(shooter, victim, rcon, arena, side)
    killed, hp_before, hp_after, clip, rejected, life_before, life_after, life_end = (
        struck
    )
    verdict, reason = _readiness.judge_damage(
        hp_before=hp_before,
        hp_after=hp_after,
        killed=killed,
        life_before=life_before,
        life_after=life_after,
        life_end=life_end,
        expect_hit=expect_hit,
        rejected=rejected,
        clip=clip,
        expect_clips=SIDE_CLIPS[side],
        separation=sep,
        reach=reach,
    )
    if verdict is None:
        print(f"LEG {key}: INCONCLUSIVE {reason}", flush=True)
        return None
    print(
        f"LEG {key}: {'PASS' if verdict else 'FAIL'}"
        f" expected={'hit' if expect_hit else 'miss'}~{nominal}"
        f" killed={killed} hp {hp_before}->{hp_after} ({reason})",
        flush=True,
    )
    return verdict


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    from devstack import SERVER

    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot, bot2 = bot_factory(args.bot_port), bot_factory(args.bot_port2)
    out = []
    try:
        if getattr(args, "resume", False):
            arena = dimension(bot("state"))
            if (
                not arena.startswith(f"blockfield:{args.map}_")
                or dimension(bot2("state")) != arena
            ):
                print(
                    "INCONCLUSIVE resume needs both clients in the same map", flush=True
                )
                return _readiness.INCONCLUSIVE
            if not redeploy(bot, arena) or not redeploy(bot2, arena):
                print("INCONCLUSIVE resume redeploy failed", flush=True)
                return _readiness.INCONCLUSIVE
            _room = "resumed"
        else:
            _room, arena = deploy(bot, get_log, "deathmatch", args.map, bot2)
        print(f"room {_room} arena {arena}", flush=True)
        _duo.wait_arena(bot2, arena)
        try:
            _duo.kit_or_raise(bot)
        except TimeoutError:
            bot("bfaction class:assault:0")
            _duo.kit_or_raise(bot)
        if not knife_in_hand(bot):
            return finish(
                [("knife in hand", False, f"held={bot('state').get('held')}")]
            )
        print("knife in hand", flush=True)
        bounds = arena_bounds(SERVER, args.map)
        print(f"bounds={bounds}", flush=True)
        try:
            shooter_name = bot("state")["name"]
            victim_name = bot2("state")["name"]
        except (OSError, RuntimeError):
            shooter_name, victim_name = "?", "?"
        # Resume key identifies the tested build and the complete leg inputs:
        # without the server version a rebuild into the same arena ID with the
        # same names would skip every leg and return a stale PASS.
        try:
            server_version = rcon.cmd("bf version") or "unknown"
        except (OSError, RuntimeError):
            server_version = "unknown"
        inputs = {
            "fixture": 3,
            "map": args.map,
            "arena": arena,
            "legs": [list(leg) for leg in LEGS],
            "shooter": shooter_name,
            "victim": victim_name,
            "server": server_version,
            "build": _readiness.server_build(),
        }
        # Unknown build identity: never reuse a cached PASS.
        resumable = server_version != "unknown" and inputs["build"] is not None
        stages = _readiness.inputs_hash(inputs)
        root = _readiness.stage_root("knife")
        inconclusive = False
        for key, side, dist, aim_off, expect_hit, nominal in LEGS:
            prev = _readiness.load_stage(root, stages, key)
            if resumable and prev and prev.get("ok") is True:
                out.append(
                    (f"knife {key}", None, f"resumed: {prev.get('evidence', '')}")
                )
                continue
            ok = leg(
                bot,
                bot2,
                rcon,
                arena,
                bounds,
                key,
                side,
                dist,
                aim_off,
                expect_hit,
                nominal,
            )
            _readiness.save_stage(
                root,
                stages,
                key,
                {
                    "ok": ok,
                    "evidence": f"expect_{'hit' if expect_hit else 'miss'}~{nominal}",
                    "inputsHash": stages,
                },
                get_log(),
            )
            if ok is None:
                inconclusive = True
            out.append(
                (
                    f"knife {key}",
                    ok,
                    f"expect_{'hit' if expect_hit else 'miss'}~{nominal}",
                )
            )
        code = finish(out)
        if code == _readiness.PASS and inconclusive:
            return _readiness.INCONCLUSIVE
        return code
    finally:
        bot.close()
        bot2.close()


def finish(rows):
    """One PASS/FAIL/SKIP line per leg, then the tally; nonzero exit on any fail."""
    out = []
    for name, ok, evidence in rows:
        if ok is None:
            print(f"SKIP {name} :: {evidence}", flush=True)
            continue
        if ok:
            print(f"PASS {name} :: {evidence}", flush=True)
            out.append(check(name, True))
        else:
            out.append(check(name, ok, evidence))
    return tally(out) and 1 or 0


def register(sub):
    parser = sub.add_parser("knife", help=SCENARIO["help"])
    parser.add_argument("--map", default="dm_arena")
    parser.add_argument(
        "--resume", action="store_true", help="resume legs in the current arena"
    )
    parser.add_argument("--bot-port2", type=int, default=47778)
    rcon_args(parser)
    parser.set_defaults(func=run)
