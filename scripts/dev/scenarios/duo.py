#!/usr/bin/env python3
"""Two-client smoke: both join one FFA room, face off, one gun-damages the other.

Needs both dev clients (testbot 47777 + 47778): with one bot it ends
INCONCLUSIVE (exit 2), never a pass. Proves the two-client wiring end to end — two testbot
sessions against one server, damage numbers on both sides.
"""

import math
import time

from . import readiness as _readiness
from .common import Bot, check, connect_rcon, dimension, number, rcon_args, tally
from .rc import deploy

SCENARIO = {
    "name": "duo",
    "help": "two-client smoke: join, face off, gun damage",
}
RESPAWN = "blockfield_shared:respawn"
GUN = "tacz:modern_kinetic_gun"


def wait_arena(bot, arena, timeout=30):
    """The second bot trails the first through join/class; bound the wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dimension(bot("state")) == arena:
            return
        time.sleep(1)
    raise TimeoutError(f"second client never reached {arena}")


def kit_or_raise(bot, timeout=30):
    """Bounded wait for the kit gun in hand (class picks land during PREPARING)."""
    bot("slot 0")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bot("state").get("held") == GUN:
            return
        time.sleep(1)
    raise TimeoutError(f"never equipped: {bot('state')}")


def ammo(rcon, bot):
    state = bot("state")
    path = (
        f"Inventory[{{Slot:{state['slot']}b}}].components."
        '"minecraft:custom_data".GunCurrentAmmoCount'
    )
    return number(rcon.cmd(f"data get entity {state['name']} {path}"))


def rendered(shooter, radius=32, timeout=15.0):
    """The victim's entity is in the shooter's render list (chunk loaded).

    Under load the teleported victim takes seconds to appear client-side;
    shooting blind always misses and burns the finite mag, so lanes wait for
    it instead. A player entry is type "player" (testbot `entities` only
    names custom-named entities); the shooter itself is already excluded.
    `radius` must cover the lane distance: a 60 m lane never appears in the
    default 32-block query, so callers pass distance + margin.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            seen = shooter(f"entities {radius}").get("entities") or []
        except (OSError, RuntimeError):
            seen = []
        if any(isinstance(e, dict) and e.get("type") == "player" for e in seen):
            return True
        time.sleep(0.5)
    return False


def clamp_inside(value, low, high):
    """Pin a teleport coordinate inside the map bounds (upper edge inclusive:
    max+1.0 is still inside, MapConfigManager.isOutOfBounds)."""
    return min(max(value, low), high + 1.0)


def face_off(
    rcon,
    shooter,
    victim,
    distance=8,
    shots_per_lane=4,
    dead=None,
    bounds=None,
    until_dead=False,
    lanes=None,
    get_log=None,
    arena=None,
    protection_timeout=0,
    ads=False,
):
    """Gun damage at close range: ammo drops on the shooter, HP on the victim.

    One lane per orthogonal offset around the victim's deploy spot: hamburg
    deploy spots routinely put a wall or furniture between the old fixed +X
    offset and the victim, and then every bullet dies on geometry
    (block_contact, no damage) while the gun itself is fine. A lane whose
    probe shots spend ammo but move no HP is abandoned with its ammo delta;
    the first lane that moves HP (or kills) passes at once.
    dead is an optional victim-state predicate reporting a kill; it defaults
    to the shared-respawn-dim check. Modes whose dead wait inside the match
    dimension (deathmatch) pass their own predicate instead.
    bounds is an optional (minX, maxX, minZ, maxZ) map rectangle: lane
    offsets are clamped inside it, so a corner deploy spot on a small arena
    (dm_arena) never parks the shooter outside the battle boundary, which
    kills outsiders. Without bounds the offsets apply raw (large maps).
    until_dead keeps shooting past the first HP drop: damage alone does not
    pass, only dead does (a scratch is not a kill). The lane log then carries
    the HP trail for a gun that cannot finish.
    lanes overrides the four orthogonal offsets (the RC long-range leg adds
    the four diagonals: hamburg blocks every orthogonal lane while the same
    gun kills in the open dm_arena).
    """
    gone = dead if dead is not None else (lambda s: dimension(s) == RESPAWN)
    if lanes is None:
        lanes = (
            (distance, 0, -90),
            (-distance, 0, 90),
            (0, distance, 180),
            (0, -distance, 0),
        )
    target = victim("state")
    x, y, z = target["pos"]
    dim = target["dimension"]
    victim_name = target["name"]
    shooter_name = shooter("state")["name"]
    # The kit mag is finite with no reserve: a dry mag must not read as a pass
    # or as a gun failure. The kit can also vanish mid-leg (round end wipes
    # it), so a missing slot reads as a lane skip, never a leg crash.
    try:
        mag = ammo(rcon, shooter)
    except (OSError, RuntimeError, AssertionError) as error:
        return False, f"gun not loaded ({str(error)[:80]}): kit grant or reload broken"
    if mag < len(lanes) * shots_per_lane:
        return False, f"gun not loaded (ammo {mag:g}): kit grant or reload broken"
    # Fail-closed gate: with a server log and arena (all live callers) every
    # lane is readiness-checked and a protection-active fight refuses to burn
    # the mag; without them (unit callers) the old lane behavior is unchanged.
    gated = get_log is not None and arena is not None
    if gated:
        try:
            vulnerable = _readiness.probe_vulnerable(
                rcon, arena, victim_name, shooter_name, timeout=protection_timeout
            )
        except (OSError, RuntimeError) as error:
            raise _readiness.NotReady("vulnerable", f"probe: {str(error)[:80]}")
        if not vulnerable:
            raise _readiness.NotReady(
                "vulnerable", "spawn protection still active (or probe refused)"
            )
    tried = []
    shot_any = False
    for dx, dz, yaw in lanes:
        tag = f"({dx:g},{dz:g})"
        sx, sz = x + dx, z + dz
        if bounds is not None:
            min_x, max_x, min_z, max_z = bounds
            cx, cz = clamp_inside(sx, min_x, max_x), clamp_inside(sz, min_z, max_z)
            if (cx, cz) != (sx, sz):
                tag += " clamped"
            sx, sz = cx, cz
        # Actual post-clamp separation: a lane clamped against the boundary
        # can pass far below the requested distance, so the evidence records
        # what was really shot (rc-triage: dm_arena lanes clamp to ~30 m).
        sep = math.dist((sx, y + 1, sz), (x, y, z))
        if bounds is not None and sep < distance - 1.0:
            # A lane that lost more than a block to the clamp no longer
            # tests the requested range (rc-triage: a 60 m dm_arena lane
            # clamped to ~3 m can pass immediately): skip it, never shoot
            # it short.
            tried.append(f"{tag},sep={sep:.0f}:shortened, skipped")
            continue
        tag += f",sep={sep:.0f}"
        rcon.cmd(f"execute in {dim} run tp {shooter_name} {sx} {y + 1} {sz} {yaw} 0")
        rcon.cmd(f"execute in {dim} run tp {victim_name} {x} {y} {z} 90 0")
        time.sleep(1.5)
        shooter("slot 0")
        time.sleep(1)
        if not rendered(shooter, radius=distance + 16):
            tried.append(f"{tag}:victim not rendered")
            continue
        if gated:
            # Placement just rendered the victim and the pre-lane probe just
            # proved vulnerability, so both are attested; the rest is
            # re-measured fresh per lane. A lane whose fixture broke (kit
            # wiped, dimension changed) is skipped, never scored.
            ready, _, missing = _readiness.check_ready(
                shooter, victim, rcon, get_log, arena, GUN, los=True, vulnerable=True
            )
            if not ready:
                tried.append(f"{tag}:not ready ({missing})")
                continue
        lane_ammo = None
        try:
            if ads:
                # Hip spread (7 for elitex:ak47) misses at range; the testbot
                # holds the TaCZ aim key ~10 s, so re-assert it per lane and
                # let aim progress reach 1 before the first shot (#250).
                shooter("tacz aim true")
                time.sleep(1.0)
            lane_ammo = ammo(rcon, shooter)
            before_hp = victim("state")["health"]
            if gated:
                # The pre-lane probe's 1 HP may not have reached the client
                # yet; a client baseline would score that late update as a hit.
                server_hp = _readiness.server_health(rcon, arena, victim_name)
                if server_hp is not None:
                    before_hp = min(before_hp, server_hp)
            last_hp = before_hp
            shot_any = True
            for _ in range(shots_per_lane):
                result = shooter(f"aimshoot {victim_name}")
                if result.get("result") == "IS_DRAWING":
                    time.sleep(0.5)
                    result = shooter(f"aimshoot {victim_name}")
                if result.get("result") == "NO_AMMO":
                    break
                time.sleep(0.5)
                state = victim("state")
                if gone(state):
                    return (
                        True,
                        f"kill on lane {tag}:"
                        f" ammo {mag:g} spent, victim out ({dimension(state)})",
                    )
                if state["health"] < before_hp:
                    last_hp = state["health"]
                    if not until_dead:
                        after_ammo = ammo(rcon, shooter)
                        return (
                            True,
                            f"lane {tag}: ammo {lane_ammo:g}->{after_ammo:g},"
                            f" HP {before_hp}->{state['health']}",
                        )
            if until_dead and last_hp < before_hp:
                # The lane hits but the per-lane budget ran out before the
                # kill (TTK above shots_per_lane, e.g. armored AK74 needing
                # 5+ while the budget is 4): stay on this open lane instead
                # of abandoning it for possibly-blocked ones. Bounded by the
                # lane-entry mag, so a blocked lane still costs only its
                # budget and an unkillable victim ends the lane, never hangs.
                # Stops after shots_per_lane misses in a row so a lane that hit
                # once (spread, partial cover) keeps ammo for the next lanes.
                misses = 0
                for _ in range(int(lane_ammo)):
                    if misses >= shots_per_lane:
                        break
                    result = shooter(f"aimshoot {victim_name}")
                    if result.get("result") == "IS_DRAWING":
                        time.sleep(0.5)
                        result = shooter(f"aimshoot {victim_name}")
                    if result.get("result") == "NO_AMMO":
                        break
                    time.sleep(0.5)
                    state = victim("state")
                    if gone(state):
                        return (
                            True,
                            f"kill on lane {tag}:"
                            f" ammo {mag:g} spent, victim out ({dimension(state)})",
                        )
                    if state["health"] < last_hp:
                        last_hp = state["health"]
                        misses = 0
                    else:
                        misses += 1
        except (OSError, RuntimeError, AssertionError) as error:
            # A lost target mid-lane (respawn flicker, render unload) or a kit
            # wiped mid-leg fails the lane, not the scenario: the next lane
            # re-acquires.
            tried.append(f"{tag}:lane lost ({str(error)[:60]})")
            continue
        try:
            spent = lane_ammo - ammo(rcon, shooter)
        except (OSError, RuntimeError, AssertionError) as error:
            tried.append(f"{tag}:lane lost ({str(error)[:60]})")
            continue
        if last_hp < before_hp:
            tried.append(f"{tag}:{spent:g},HP {before_hp}->{last_hp}")
        else:
            tried.append(f"{tag}:{spent:g}")
    if gated and not shot_any:
        # No lane ever shot (shortened, unrendered or not ready): the fixture
        # never became valid, so this is an inconclusive setup, never a gun failure.
        raise _readiness.NotReady("face_off lane", "; ".join(tried) or "no usable lane")
    return (
        False,
        f"no kill in {len(lanes)} lanes ({', '.join(tried)}),"
        f" HP {victim('state')['health']}: lanes blocked or gun broken",
    )


def run(args, rcon_factory=connect_rcon, bot_factory=Bot, get_log=None):
    from devstack import LOG

    logs = get_log or (lambda: LOG.read_text(encoding="utf-8", errors="replace"))
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        try:
            try:
                bot2 = bot_factory(args.bot_port2)
                bot2("state")
            except (OSError, RuntimeError) as error:
                # A solo run is an invalid setup, never a pass: the smoke
                # needs both clients to mean anything.
                raise _readiness.NotReady(
                    "bot2 probe answered",
                    f"second client bot port {args.bot_port2} not answering"
                    f" ({str(error)[:60]}): start it, then rerun",
                )
            try:
                out = []
                _, arena = deploy(bot, logs, "deathmatch", args.map, bot2)
                wait_arena(bot2, arena)
                kit_or_raise(bot)
                kit_or_raise(bot2)
                # Hip spread (7 for the kit gun) misses by design at lane
                # range, so lanes shoot the stand-only zero-spread gun the
                # hitreg/hud legs equip the same way; kit-gun damage stays
                # covered by the dm/rc legs. Deferred: hud_checks imports duo.
                from .hud_checks import ZERO_SPREAD, gun_replace_command

                shooter_name = bot("state")["name"]
                rcon.cmd(gun_replace_command(shooter_name, ZERO_SPREAD))
                bot("slot 0")
                time.sleep(1)
                if ZERO_SPREAD not in rcon.cmd(
                    f"data get entity {shooter_name} "
                    'Inventory[{Slot:0b}].components."minecraft:custom_data".GunId'
                ):
                    out.append(
                        check(
                            "gun damage lands on the second client",
                            False,
                            "admin gun did not land",
                        )
                    )
                    return tally(out) and 1 or 0
                ok, detail = face_off(
                    rcon, bot, bot2, get_log=logs, arena=arena, protection_timeout=60
                )
                out.append(check("gun damage lands on the second client", ok, detail))
                return tally(out) and 1 or 0
            finally:
                bot2.close()
        except _readiness.NotReady as error:
            print(f"INCONCLUSIVE duo smoke: {error}", flush=True)
            if error.field == "bot2 probe answered":
                print(
                    "duo smoke needs two clients: start the second headless client"
                    " with testbot, then rerun",
                    flush=True,
                )
            return _readiness.INCONCLUSIVE
    finally:
        bot.close()


def register(sub):
    parser = sub.add_parser("duo", help=SCENARIO["help"])
    parser.add_argument("--map", default="hamburg")
    parser.add_argument("--bot-port2", type=int, default=47778)
    rcon_args(parser)
    parser.set_defaults(func=run)
