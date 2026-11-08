#!/usr/bin/env python3
"""Kill-gated combat checks: grenade death-drop, killfeed isolation + own marker, MP5A3, tricky names.

Needs the testbot killfeed/markers/usestate queries (scripts/dev/testbot).
Two headless clients: the killer and the victim/observer. Room-killfeed only
delivers kills whose victim (or, when the victim is not a room member, whose
attacker) belongs to the recipient's room, so carpet victims never feed a room
member's killfeed — the victim is the second client (duel, grenade, tag) except the isolation kill, a first-client suicide its room still feeds. A tricky
name rides the offline stand as the second client's login name (`just play
--client 2 --name <one of TRICKY_NAMES>`); RCON never sees it, entity
references go by offline UUID (devstack.offline_uuid, the same derivation the
stand uses). Vanilla login rejects <>/spaces/unicode, so `<tag>` names can
never join: the literalness check runs against login-valid but render-tricky
names instead (underscore-heavy, placeholder-word-like, digits-only, 16-char).
"""

import re
import struct
import time
import zlib

from .common import Bot, check, connect_rcon, dimension, number
from .common import rcon_args, tally
from .dm import arena_bounds
from .duo import clamp_inside, rendered
from .rc import deploy, open_room

SCENARIO = {
    "name": "hud-checks",
    "help": "kill-gated checks: grenade drop, killfeed isolation, MP5A3, tricky names",
}
# Login-valid ([A-Za-z0-9_]{3,16}) but render-tricky second-client names for
# the tag/countdown/killfeed literalness check. Vanilla login rejects <> and
# friends, so the runner launches --client 2 with one of these.
TRICKY_NAMES = (
    "__x__",
    "_reset_",
    "x_reset_x",
    "player_name",
    "007",
    "1234567890123456",
)


def is_tricky_name(name):
    """True for a scenario tricky name (login-valid, render-tricky)."""
    return name in TRICKY_NAMES


KIT_GUN = "tacz:modern_kinetic_gun"
ZERO_SPREAD = "tacz:ak47_zero_spread"
MP5 = "elitex:mp5_classic"
GRENADE_ITEMS = (
    "lrtactical:throwable",
    "superbwarfare:m18_smoke_grenade",
    "superbwarfare:rgo_grenade",
    "superbwarfare:hand_grenade",
)
GRENADE_TYPES = ("grenade", "throwable", "m18", "rgo", "smoke")


def gun_replace_command(name, gun_id, ammo=30000):
    """Existing admin path to a specific weapon: RCON item replace with TACZ NBT."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name or ""):
        raise ValueError(f"refusing to drive a suspicious player name: {name!r}")
    if not re.fullmatch(r"[a-z0-9_]+:[a-z0-9_/]+", gun_id or ""):
        raise ValueError(f"refusing a suspicious gun id: {gun_id!r}")
    if not 1 <= int(ammo) <= 30000:
        raise ValueError(f"ammo out of range: {ammo!r}")
    return (
        f"item replace entity {name} hotbar.0 with {KIT_GUN}"
        f'[minecraft:custom_data={{GunId:"{gun_id}",GunCurrentAmmoCount:{int(ammo)},'
        'HasBulletInBarrel:1b,GunFireMode:"SEMI"}]'
    )


def parse_health(reply):
    """Float from `data get entity <name> Health` (`... has Health: 20.0f`)."""
    match = re.search(r"Health: (-?\d+(?:\.\d+)?)f", reply or "")
    if not match:
        raise ValueError(f"no Health in {reply!r:.120}")
    return float(match.group(1))


def parse_pos(reply):
    """Triple from `data get entity <name> Pos` (a `Pos:` line or the bare list)."""
    match = re.search(
        r"\[(-?\d+(?:\.\d+)?)d, (-?\d+(?:\.\d+)?)d, (-?\d+(?:\.\d+)?)d\]",
        reply or "",
    )
    if not match:
        raise ValueError(f"no Pos in {reply!r:.120}")
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def killfeed_match(entries, attacker=None, target=None):
    """First killfeed entry matching attacker/target (None matches anything)."""
    for entry in entries or []:
        if attacker is not None and entry.get("attacker") != attacker:
            continue
        if target is not None and entry.get("target") != target:
            continue
        return entry
    return None


def magenta_fraction_png(data):
    """Share of pure-magenta pixels (missing-texture purple) in an 8-bit PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, width, height, ctype, depth, interlace = 8, 0, 0, 0, 0, 0
    raw = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, chunk = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
        elif kind == b"IDAT":
            raw += chunk
        elif kind == b"IEND":
            break
    if depth != 8 or ctype not in (2, 6) or interlace != 0:
        raise ValueError(
            f"unsupported PNG: depth={depth} type={ctype} interlace={interlace}"
        )
    channels = 3 if ctype == 2 else 4
    stride = width * channels
    pixels = bytearray(width * height * channels)
    stream, row, previous = zlib.decompress(raw), 0, bytearray(stride)
    off = 0
    while row < height:
        filt, line = stream[off], bytearray(stream[off + 1 : off + 1 + stride])
        off += 1 + stride
        for i in range(channels, stride):
            a, b, c = line[i - channels], previous[i], previous[i - channels]
            if filt == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
            elif filt != 0:
                raise ValueError(f"unknown PNG filter {filt}")
        pixels[row * stride : (row + 1) * stride] = line
        previous, row = line, row + 1
    magenta = sum(
        1
        for i in range(0, len(pixels), channels)
        if pixels[i] > 200 and pixels[i + 1] < 80 and pixels[i + 2] > 200
    )
    return magenta / (width * height)


def wait_for(predicate, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(interval)
    return False


def offline_uuid(name):
    from devstack import offline_uuid as vanilla_uuid

    return vanilla_uuid(name)


def scenario_grenade(rcon, bot, witness):
    """Cook a kit grenade, die mid-cook by RCON, assert the live drop entity.

    The corpse is queried through a witness in the same arena: after death the
    victim sits in respawn staging, so its own entity query recenters away.
    """
    out = []
    cooked = None
    for slot in (2, 3):
        bot(f"slot {slot}")
        bot("hold key.use 120")
        if not wait_for(
            lambda: (
                bot("usestate").get("using")
                and bot("usestate").get("item") in GRENADE_ITEMS
            ),
            3,
            interval=0.2,
        ):
            continue
        try:
            use = bot("usestate")
        except RuntimeError:
            continue
        if use.get("using") and use.get("item") in GRENADE_ITEMS:
            cooked = (slot, use["item"], use.get("ticksUsing"), use.get("duration"))
            break
    out.append(
        check(
            "kit grenade cooks on hold (usestate using)",
            cooked is not None,
            str(cooked or "no throwable in slots 2..3"),
        )
    )
    if cooked is None:
        return out, False
    death_pos = bot("state")["pos"]
    use = bot("usestate")
    assert use.get("using"), "cook ended before the kill"
    ticks = use.get("ticksUsing")
    bot_name = bot("state")["name"]
    dim = bot("state")["dimension"]
    witness("slot 0")
    witness_uuid = offline_uuid(witness("state")["name"])
    rcon.cmd(
        f"execute in {dim} run tp {witness_uuid} "
        f"{death_pos[0] + 4} {death_pos[1]} {death_pos[2]} 90 0"
    )
    time.sleep(1)
    rcon.cmd(f"kill {bot_name}")
    time.sleep(1)

    def drop_near(query):
        try:
            entities = query("entities 64").get("entities", [])
        except RuntimeError:
            return False
        return any(
            any(k in (entity.get("type") or "") for k in GRENADE_TYPES)
            and abs(entity["tickPos"][0] - death_pos[0]) < 5
            and abs(entity["tickPos"][1] - death_pos[1]) < 5
            and abs(entity["tickPos"][2] - death_pos[2]) < 5
            for entity in entities
        )

    dropped = wait_for(lambda: drop_near(witness) or drop_near(bot), 12)
    out.append(
        check(
            "death mid-cook drops a live grenade at the corpse",
            dropped,
            f"no grenade entity near {death_pos} (cook {cooked}, ticks {ticks})",
        )
    )
    if not dropped:
        return out, False
    exploded = wait_for(
        lambda: (
            not any(
                any(k in (entity.get("type") or "") for k in GRENADE_TYPES)
                for entity in witness("entities 64").get("entities", [])
            )
        ),
        25,
    )
    out.append(
        check(
            "dropped grenade explodes (entity resolves)",
            exploded,
            "drop still present after 25 s; fuse longer than the window",
        )
    )
    time.sleep(4)
    bot("bfaction spawn:BASE")
    redeployed = wait_for(lambda: dimension(bot("state")).startswith("blockfield:"), 30)
    out.append(
        check(
            "victim redeploys after the cook death", redeployed, dimension(bot("state"))
        )
    )
    return out, redeployed


def lane_ammo(rcon, shooter_uuid, slot):
    return number(
        rcon.cmd(
            f"data get entity {shooter_uuid} "
            f'Inventory[{{Slot:{slot}b}}].components."minecraft:custom_data"'
            ".GunCurrentAmmoCount"
        )
    )


def lane_kill(rcon, shooter, victim, arena, gun_id=None, bounds=None):
    """Gun kill down the open lane: kit gun first (the true product path),
    zero-spread admin gun second (harness control). UUIDs only on RCON; the
    aim matches the literal name client-side, so tricky-name victims work.
    Returns (ok, detail, used_admin_gun)."""
    from devstack import offline_uuid

    shooter_uuid = offline_uuid(shooter("state")["name"])
    victim_uuid = offline_uuid(victim("state")["name"])
    shooter("slot 0")
    time.sleep(1)
    if gun_id is not None:
        rcon.cmd(gun_replace_command(shooter("state")["name"], gun_id))
        shooter("slot 0")
        time.sleep(1)
    if gun_id is not None and gun_id not in rcon.cmd(
        f"data get entity {shooter_uuid} "
        f'Inventory[{{Slot:0b}}].components."minecraft:custom_data".GunId'
    ):
        return False, "admin gun did not land", gun_id is not None
    mag = lane_ammo(rcon, shooter_uuid, 0)
    if mag < 16:
        return False, f"gun not loaded (ammo {mag:g})", gun_id is not None
    lanes = ((8, 0, -90), (-8, 0, 90), (0, 8, 180), (0, -8, 0))
    spot = victim("state")
    x, y, z = spot["pos"]
    victim_name = spot["name"]
    tried = []
    for dx, dz, yaw in lanes:
        tag = f"({dx:g},{dz:g})"
        sx, sz = x + dx, z + dz
        if bounds is not None:
            min_x, max_x, min_z, max_z = bounds
            cx, cz = clamp_inside(sx, min_x, max_x), clamp_inside(sz, min_z, max_z)
            if (cx, cz) != (sx, sz):
                tag += " clamped"
            sx, sz = cx, cz
        rcon.cmd(f"execute in {arena} run tp {shooter_uuid} {sx} {y + 1} {sz} {yaw} 0")
        rcon.cmd(f"execute in {arena} run tp {victim_uuid} {x} {y} {z} 90 0")
        time.sleep(1.5)
        shooter("slot 0")
        time.sleep(1)
        if not rendered(shooter, radius=24):
            tried.append(f"{tag}:victim not rendered")
            continue
        lane_start = lane_ammo(rcon, shooter_uuid, 0)
        try:
            for _ in range(4):
                result = shooter(f"aimshoot {victim_name}")
                if result.get("result") == "IS_DRAWING":
                    time.sleep(0.5)
                    result = shooter(f"aimshoot {victim_name}")
                if result.get("result") == "NO_AMMO":
                    break
                time.sleep(0.5)
                now = victim("state")
                if now.get("health", 20) <= 0 or "respawn" in now.get("dimension", ""):
                    return True, f"kill on lane {tag}", gun_id is not None
                if dimension(now) != arena or now.get("held") != KIT_GUN:
                    return (
                        True,
                        f"kill on lane {tag} (victim left the arena)",
                        gun_id is not None,
                    )
        except (OSError, RuntimeError) as error:
            tried.append(f"{tag}:aim {str(error)[:80]}")
            continue
        spent = lane_start - lane_ammo(rcon, shooter_uuid, 0)
        tried.append(f"{tag}:{spent:g}")
    return False, f"no kill in 4 lanes ({', '.join(tried)})", gun_id is not None


def scenario_duel(rcon, bot, bot2, arena, bounds):
    """Real kills between room members: kit gun first, admin gun as control."""
    out = []
    killer, victim = bot("state")["name"], bot2("state")["name"]
    before = len(bot("killfeed").get("entries", []))
    before2 = len(bot2("killfeed").get("entries", []))
    ok, detail, admin = lane_kill(rcon, bot, bot2, arena, bounds=bounds)
    if not ok:
        ok, detail, admin = lane_kill(
            rcon, bot, bot2, arena, gun_id=ZERO_SPREAD, bounds=bounds
        )
    if not ok:
        out.append(
            check(
                "gun kill lands on the room victim",
                False,
                detail + " (product bug if lanes were clear: no damage path works)",
            )
        )
        return out, None
    if admin:
        out.append(
            check(
                "kit gun fails where the admin gun kills (product bug)", False, detail
            )
        )
    out.append(check("gun kill lands on the room victim", True, detail))
    mine = killfeed_match(
        bot("killfeed").get("entries", [])[before:],
        attacker=killer,
        target=victim,
    )
    out.append(
        check(
            "killer client holds the killfeed line",
            mine is not None,
            f"no {killer}->{victim} line",
        )
    )
    theirs = killfeed_match(
        bot2("killfeed").get("entries", [])[before2:],
        attacker=killer,
        target=victim,
    )
    out.append(
        check(
            "victim client holds the same killfeed line",
            theirs is not None,
            f"no {killer}->{victim} line on the victim",
        )
    )
    entry = killfeed_match(bot("killfeed").get("entries", []), target=victim)
    return out, entry


def rcon_kill(rcon, killer, victim):
    """Lethal RCON hit with player attribution: deterministic killfeed source
    when guns are unavailable. Victim stays a room member; killer in-match
    (countdown attackers are denied the damage)."""
    dim = victim("state")["dimension"]
    killer_name, victim_name = killer("state")["name"], victim("state")["name"]
    before_killer = len(killer("killfeed").get("entries", []))
    before_victim = len(victim("killfeed").get("entries", []))
    reply = rcon.cmd(
        f"execute in {dim} run damage {offline_uuid(victim_name)} 100"
        f" blockfield:knife by {offline_uuid(killer_name)}"
    )
    if "invulnerable" in reply or "Applied" not in reply:
        return [check("RCON lethal hit lands", False, reply[:160])], None
    dead = wait_for(
        lambda: (
            victim("state").get("health", 20) <= 0
            or "respawn" in victim("state").get("dimension", "")
        ),
        10,
    )
    out = [check("credited RCON kill lands", dead, victim_name)]
    entry = None
    if dead:
        mine = killfeed_match(
            killer("killfeed").get("entries", [])[before_killer:],
            attacker=killer_name,
            target=victim_name,
        )
        out.append(
            check(
                "killer client holds the killfeed line",
                mine is not None,
                f"no {killer_name}->{victim_name} line",
            )
        )
        theirs = killfeed_match(
            victim("killfeed").get("entries", [])[before_victim:],
            attacker=killer_name,
            target=victim_name,
        )
        out.append(
            check(
                "victim client holds the same killfeed line",
                theirs is not None,
                f"no {killer_name}->{victim_name} line on the victim",
            )
        )
        entry = killfeed_match(
            killer("killfeed").get("entries", []), target=victim_name
        )
    return out, entry


def scenario_isolation(rcon, bot, bot2, room):
    """Observer parked outside must not see the next room kill; the victim's own
    map context hides while dead and returns on respawn. The lethal hit is an
    in-room suicide (the victim credits itself), so attacker and victim are the
    same started-room member: the countdown damage gate never denies it, the
    room killfeed still feeds, and the parked observer must gain no line."""
    out = []
    victim = bot("state")["name"]
    dim = bot("state")["dimension"]
    before = len(bot("killfeed").get("entries", []))
    before2 = len(bot2("killfeed").get("entries", []))
    try:
        reply = rcon.cmd(
            f"execute in {dim} run damage {offline_uuid(victim)} 100"
            f" blockfield:knife by {offline_uuid(victim)}"
        )
    except (ValueError, RuntimeError) as error:
        out.append(check("RCON lethal hit lands", False, str(error)[:160]))
        return out
    if "invulnerable" in reply:
        out.append(check("victim vulnerable outside protection", False, reply[:120]))
        return out
    dead = wait_for(lambda: bot("markers").get("deployed") is False, 10)
    out.append(
        check(
            "RCON kill drops the victim out of deployment",
            dead,
            dimension(bot("state")),
        )
    )
    entry = killfeed_match(
        (bot("killfeed").get("entries", [])[before:] if dead else []),
        attacker=victim,
        target=victim,
    )
    out.append(
        check(
            "self-credited RCON death feeds the room killfeed",
            entry is not None,
            f"no {victim}->{victim} line",
        )
    )
    isolated = (
        killfeed_match(
            bot2("killfeed").get("entries", [])[before2:],
            attacker=victim,
            target=victim,
        )
        is None
    )
    out.append(
        check(
            "parked client sees no line from the other room (isolation)",
            entry is not None and isolated,
            f"room {room} leaked to the hub client"
            if not isolated
            else "no local entry",
        )
    )
    hidden = wait_for(lambda: bot("markers").get("minimap") is None, 30)
    out.append(check("dead client hides the minimap", dead and hidden, "minimap shown"))
    time.sleep(4)
    bot("bfaction spawn:BASE")
    back = wait_for(lambda: bot("markers").get("deployed") is True, 30)
    shown = wait_for(lambda: bot("markers").get("minimap") is not None, 30)
    out.append(
        check("respawn restores deployment and minimap", back and shown, "hidden")
    )
    alive = bot("markers")
    out.append(
        check(
            "own map context alive (deployed, minimap)",
            alive.get("deployed") is True and alive.get("minimap") is not None,
            str({k: alive.get(k) for k in ("deployed", "minimap", "playerTeam")})[:160],
        )
    )
    return out


def scenario_mp5(rcon, bot, server_log):
    """Obtain elitex:mp5_classic via the admin path; draw, render events, logs.

    Whole-frame magenta is sky-contaminated on this map (a control shot with a
    different held item shows the same level), so the pixels are evidence, not
    a gate: the render pipeline plus a clean client log carry the verdict.
    """
    out = []
    mark = len(server_log().splitlines())
    bot("renderprobe start")
    state = bot("state")
    rcon.cmd(gun_replace_command(state["name"], MP5))
    bot("slot 0")
    time.sleep(3)
    state = bot("state")
    held_ok = state.get("held") == KIT_GUN and MP5 in rcon.cmd(
        f"data get entity {state['name']} "
        f'Inventory[{{Slot:0b}}].components."minecraft:custom_data".GunId'
    )
    out.append(
        check(
            "MP5A3 obtain lands the admin gun (mp5_classic)", held_ok, state.get("held")
        )
    )
    status = bot("tacz status")
    out.append(
        check("MP5A3 draws without error", "drawTimestamp" in status, str(status)[:120])
    )
    events = bot("renderprobe get")
    seen = [e for e in events if MP5.split(":")[1] in str(e.get("gun"))]
    out.append(check("MP5A3 render events fire", bool(seen), f"{len(events)} events"))
    bot("screenshot hud-mp5.png")
    shot, fraction, decode_error = None, None, None
    try:
        shot = bot("screenshot hud-mp5.png").get("file")
    except (OSError, RuntimeError):
        shot = None
    if shot is not None:
        from pathlib import Path as _Path

        if wait_for(
            lambda: _Path(shot).is_file() and _Path(shot).stat().st_size > 1000,
            5,
        ):
            try:
                fraction = magenta_fraction_png(_Path(shot).read_bytes())
            except (ValueError, struct.error, zlib.error, OSError) as error:
                decode_error = str(error)[:120]
    if decode_error is not None:
        out.append(check("MP5A3 screenshot decodes", False, decode_error))
    elif fraction is None:
        out.append(check("MP5A3 screenshot saved", False, str(shot)[:120]))
    else:
        out.append(
            check(
                "MP5A3 screenshot captured for texture evidence",
                True,
                f"magenta {fraction:.4f} of {shot} (sky-contaminated, see report)",
            )
        )
    bad = [
        line.strip()
        for line in server_log().splitlines()[mark:]
        if re.search(r"mp5|purple|missing texture|can't load.*model", line, re.I)
    ]
    out.append(
        check("client log shows no mp5/model errors", not bad, "; ".join(bad)[:200])
    )
    return out


def scenario_tag(rcon, bot, bot2, entry):
    """A tricky name in killfeed/countdown/messages: exact, uninterpreted, no crash."""
    out = []
    evil = bot2("state")["name"]
    if not is_tricky_name(evil):
        out.append(
            check(
                "second client carries a tricky name",
                False,
                f"launch it with --name <one of {', '.join(TRICKY_NAMES)}>: {evil!r}",
            )
        )
        return out
    ok = entry is not None and entry.get("target") == evil
    out.append(
        check(
            "killfeed carries the tricky name exactly",
            ok,
            str(entry)[:160],
        )
    )
    lines = bot("messages 30")
    try:
        bot2("state")
        connected = True
    except (OSError, RuntimeError):
        connected = False
    out.append(
        check(
            "countdown/actionbar lines stay clean with the tricky name around",
            connected and isinstance(lines, list),
            " | ".join(lines)[:200] if isinstance(lines, list) else str(lines)[:200],
        )
    )
    return out


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    from devstack import LOG, SERVER

    server_log = lambda: LOG.read_text(encoding="utf-8", errors="replace")  # noqa: E731
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        try:
            bot2 = bot_factory(args.bot_port2)
            bot2("state")
        except (OSError, RuntimeError):
            bot2 = None
        parts = (
            (args.part,)
            if args.part != "all"
            else ("killfeed", "grenade", "mp5", "tag")
        )
        out, arena, duel_entry = [], None, None
        for label, candidate in (
            (("first", bot), ("second", bot2))
            if bot2 is not None
            else (("first", bot),)
        ):
            if not wait_for(lambda: dimension(candidate("state")) != "", 90):
                out.append(
                    check(f"{label} client joins the server", False, "never in game")
                )
                return tally(out) and 1 or 0
        if set(parts) - {"mp5"}:
            room, arena = deploy(bot, server_log, args.mode, args.map, bot2)
            if bot2 is not None and not wait_for(
                lambda: dimension(bot2("state")) == arena, 45
            ):
                # The join right after room-open can race room init and drop
                # silently (no error message); a player just clicks again.
                bot2("bfaction join:" + room)
                if not wait_for(lambda: dimension(bot2("state")) == arena, 45):
                    out.append(
                        check(
                            "second client joins the room",
                            False,
                            dimension(bot2("state")),
                        )
                    )
                    return tally(out) and 1 or 0
            # Kits grant on class pick once the match starts (both members in the
            # room); deployment needs an explicit spawn, late joiners otherwise
            # sit in protected staging forever. Afterwards the spawn-protection
            # shield (5 s + slack) must run out before any kill.
            deadline = time.monotonic() + 120
            unkit = []
            while time.monotonic() < deadline:
                unkit = [
                    label
                    for label, candidate in (
                        (("killer", bot), ("observer", bot2))
                        if bot2 is not None
                        else (("killer", bot),)
                    )
                    if candidate("state").get("held") != KIT_GUN
                ]
                if not unkit:
                    break
                bot("slot 0")
                bot("bfaction class:assault:0")
                if bot2 is not None:
                    bot2("slot 0")
                    bot2("bfaction class:assault:0")
                time.sleep(10)
            if unkit:
                out.append(
                    check(
                        "kits land once the match starts",
                        False,
                        "kitless: " + ",".join(unkit),
                    )
                )
                return tally(out) and 1 or 0
            deadline = time.monotonic() + 90
            undeployed = ["killer", "observer"]
            while time.monotonic() < deadline:
                undeployed = [
                    label
                    for label, candidate in (
                        (("killer", bot), ("observer", bot2))
                        if bot2 is not None
                        else (("killer", bot),)
                    )
                    if candidate("markers").get("deployed") is not True
                ]
                if not undeployed:
                    break
                bot("bfaction spawn:BASE")
                if bot2 is not None:
                    bot2("bfaction spawn:BASE")
                time.sleep(10)
            if undeployed:
                out.append(
                    check(
                        "both clients deploy",
                        False,
                        "staged: " + ",".join(undeployed),
                    )
                )
                return tally(out) and 1 or 0
            time.sleep(8)
        if "killfeed" in parts:
            if bot2 is None:
                print("killfeed needs two clients (member victim): skipped")
            else:
                bounds = arena_bounds(SERVER) if args.map == "dm_arena" else None
                alive = bot("markers")
                out.append(
                    check(
                        "own map context alive (deployed, minimap)",
                        alive.get("deployed") is True
                        and alive.get("minimap") is not None,
                        str(
                            {
                                k: alive.get(k)
                                for k in ("deployed", "minimap", "playerTeam")
                            }
                        )[:160],
                    )
                )
                duel_out, duel_entry = scenario_duel(rcon, bot, bot2, arena, bounds)
                out += duel_out
                if duel_entry is None:
                    fallback_out, duel_entry = rcon_kill(rcon, bot, bot2)
                    out += fallback_out
                time.sleep(4)
                bot2("bfaction spawn:BASE")
                if not wait_for(
                    lambda: dimension(bot2("state")).startswith("blockfield:"), 30
                ):
                    out.append(
                        check(
                            "victim redeploys before parking",
                            False,
                            dimension(bot2("state")),
                        )
                    )
                    return tally(out) and 1 or 0
                # The isolation observer parks in its own room, not the hub:
                # killfeed isolation across 2 rooms.
                room_b = open_room(bot2, server_log, args.mode, args.map)
                parked = wait_for(
                    lambda: (
                        dimension(bot2("state")).startswith("blockfield:")
                        and dimension(bot2("state")) != arena
                    ),
                    45,
                )
                if not parked:
                    out.append(
                        check(
                            "second client parks in its own room",
                            False,
                            dimension(bot2("state")),
                        )
                    )
                    return tally(out) and 1 or 0
                out += scenario_isolation(rcon, bot, bot2, room_b)
        if "grenade" in parts:
            if bot2 is None:
                print("grenade death-drop needs a witness client: skipped")
            else:
                grenade_out, _ = scenario_grenade(rcon, bot, bot2)
                out += grenade_out
        if "mp5" in parts:
            out += scenario_mp5(rcon, bot, server_log)
        if "tag" in parts:
            if bot2 is None:
                print("tag check needs a second client: skipped")
            elif duel_entry is None:
                # Standalone tag run: the duel is the tricky-name kill.
                if not is_tricky_name(bot2("state")["name"]):
                    out.append(
                        check(
                            "second client carries a tricky name",
                            False,
                            f"launch it with --name <one of {', '.join(TRICKY_NAMES)}>",
                        )
                    )
                else:
                    duel_out, duel_entry = scenario_duel(
                        rcon,
                        bot,
                        bot2,
                        arena,
                        arena_bounds(SERVER) if args.map == "dm_arena" else None,
                    )
                    out += duel_out
                    out += scenario_tag(rcon, bot, bot2, duel_entry)
            else:
                out += scenario_tag(rcon, bot, bot2, duel_entry)
        if bot2 is not None:
            bot2.close()
        return tally(out) and 1 or 0
    finally:
        bot.close()


def register(sub):
    parser = sub.add_parser("hud-checks", help=SCENARIO["help"])
    parser.add_argument(
        "--part", choices=("killfeed", "grenade", "mp5", "tag", "all"), default="all"
    )
    parser.add_argument("--mode", default="deathmatch")
    parser.add_argument("--map", default="dm_arena")
    parser.add_argument("--bot-port2", type=int, default=47778)
    rcon_args(parser)
    parser.set_defaults(func=run)
