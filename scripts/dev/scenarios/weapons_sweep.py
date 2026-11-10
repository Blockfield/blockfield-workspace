#!/usr/bin/env python3
"""weapons-sweep: fire every registered weapon once, server must stay up.

For each weapon id (TaCZ gun packs from the stand mods plus SuperbWarfare gun
definitions): give to the bot, select, single fire, hold fire, reload, aim,
cycle fire modes (single + burst hold in AUTO), moving fire, switch away/back,
drop, pick up, die holding it. Every shot goes into the blocks ahead so
explosive rounds detonate against the world; then the nearest range husk target
is shot at with `lookat` aim so bullets hit an entity (BodyPart/headshot
paths), explosive rounds included. After every step the server must answer
RCON, and no new server-log exception may appear for that weapon.

The victim is the nearest range husk target (NoAI, 20 HP, death cancelled by
the range): summoned zombies never survive here, the range deletes foreign
mobs about a second after summoning, so a summon-based leg can never show a
real unfired control. SBW guns run outside the range instead: a daylight-proof
husk is summoned ahead of the stationary bot with its own unfired control.

Fail-before for server#365: an explosive round whose bullet hits a block used
to kill the server (blockfield ProjectileExplosionMixin shadow miss).
"""

import json
import re
import time
import zipfile
from pathlib import Path

from .common import Bot, check, connect_rcon, dimension, rcon_args, tally
from . import hud_checks

SCENARIO = {
    "name": "weapons-sweep",
    "help": "fire every registered weapon; server must stay alive and log-clean",
}

KIT_GUN = "tacz:modern_kinetic_gun"
FIRE_MODES = ("SEMI", "BURST", "AUTO")
LOG_WATCH = re.compile(r"ERROR|Exception|Ticking (entity|server)|field_9190|FATAL")
VICTIM_TYPE = "minecraft:husk"
VICTIM_NAME = "A10a"
VICTIM_DIM = "blockfield_shared:firing_range"
# Pinned range husk (NoAI, 20 HP, death cancelled, always-forceloaded chunk):
# summoned zombies are deleted by the range within a second, per-id tags carry
# colons this server rejects in selectors, and `execute at` answers
# unreliably, so read the named target dimension-wide instead.
# Shared fixture: the sweep only ever reads it (`data get`), never kills it;
# per-run kills are scoped to SBW_VICTIM_SELECTOR, the sweep's own summon tag.
VICTIM_SELECTOR = f"@e[type={VICTIM_TYPE},name={VICTIM_NAME},limit=1]"
# Firing position for the victim leg: 8 m north of the A10a husk with a clear
# lane. The spawn booths are glassed in, so shots from spawn never reach any
# target. Teleported per weapon; the die-phase respawn returns the bot.
VICTIM_LANE_POS = (-18.5, 65.0, 2.5)
VICTIM_LANE_YAW = 0.0
# Unfired-control window: the target's HP must be static over this long with no
# shot, matching the post-shot observation window below. An HP drop without a
# shot must never count as fired evidence (server#365 sweep).
UNFIRED_CONTROL_SLEEP = 2.2
# RCON read retries: full-auto pre-fire and teleports lag the server and a
# single read times out; a timeout is not a missing entity.
READ_RETRIES = 3
READ_RETRY_SLEEP = 0.5
# Overworld SBW victim: the range deletes foreign mobs ~1 s after summoning
# and its lane teleport ejects the bot (server#365 c4 PARTIAL), so the SBW leg
# runs outside the range. A husk (daylight-proof, unlike a zombie) pinned with
# NoAI + PersistenceRequired, summoned 4 m ahead of the stationary bot.
SBW_VICTIM_TYPE = "minecraft:husk"
SBW_VICTIM_TAG = "sbw_sweep_victim"
SBW_VICTIM_SELECTOR = (
    f"@e[type={SBW_VICTIM_TYPE},tag={SBW_VICTIM_TAG},limit=1,sort=nearest]"
)
SBW_VICTIM_AHEAD = 4
# Unfired-control window: the target's HP must be static over this long with no
# shot, matching the post-shot observation window. An HP drop without a shot
# must never count as fired evidence.
SBW_CONTROL_SLEEP = 2.2
# Run-level persistence proof: the overworld must keep a summoned husk alive
# and still for this long before any SBW weapon fires.
SBW_PERSISTENCE_SLEEP = 30
# SBW ammo: a bare give never fires (GunData has no Ammo). The creative box
# feeds every SBW gun; the stand rig vacuums it, and SBW reloads read the rig
# too, so the box only has to exist, not stay in the vanilla inventory.
SBW_CREATIVE_BOX = "superbwarfare:creative_ammo_box"
# Reload wait: SBW fills the mag ~2 s after the R press; the sweep polls the
# gun's Ammo NBT this long before firing anyway (a miss never aborts the leg,
# the drain verdict still decides).
SBW_RELOAD_TIMEOUT = 8.0
SBW_RELOAD_POLL = 1.0
# Die-phase respawn wait: the bot must come back with HP after the kill, or
# the row fails loudly instead of falling through into a passing row.
RESPAWN_TIMEOUT = 25.0

SKIP_ITEMS = (
    "ammo",
    "bullet",
    "pellet",
    "buckshot",
    "slug",
    "shell_",
    "_shell",
    "magazine",
    "attachment",
    "scope",
    "sight",
    "grip",
    "muzzle",
    "suppressor",
    "silencer",
    "goggles",
    "armor",
    "vest",
    "helmet",
    "backpack",
    "radio",
    "tablet",
    "spawn_egg",
    "creative",
)


def tacz_gun_data(mods_dir):
    """({gun_id: jar}, {gun_id: ammo_id}) from every TaCZ gun-data file.

    Packs embed their data tree (`.../custom/<pack>/data/<ns>/data/guns/`)
    and the data file carries no id: `<ns>:<stem>` minus the `_data` suffix.
    ammo_id covers inventory-reload guns only (every shot reads the vanilla
    inventory for a stack whose AmmoId matches); comments stripped (TaCZ data
    json is lenient).
    """
    pattern = re.compile(r"data/([^/]+)/data/guns/([^/]+)_data\.json$")
    found, ammo = {}, {}
    root = Path(mods_dir) if mods_dir else None
    jars = sorted(root.glob("*.jar")) if root and root.is_dir() else []
    for jar in jars:
        try:
            with zipfile.ZipFile(jar) as archive:
                for entry in archive.namelist():
                    match = pattern.search(entry)
                    if not match:
                        continue
                    gun_id = f"{match.group(1)}:{match.group(2)}"
                    if not re.fullmatch(r"[a-z0-9_]+:[a-z0-9_/]+", gun_id):
                        continue
                    found.setdefault(gun_id, jar.name)
                    try:
                        text = archive.read(entry).decode("utf-8", errors="replace")
                    except KeyError:
                        continue
                    text = re.sub(r"//.*", "", text)
                    ammo_id = re.search(r'"ammo"\s*:\s*"([a-z0-9_:]+)"', text)
                    reload = re.search(
                        r'"reload"\s*:\s*\{[^}]*?"type"\s*:\s*"([a-z]+)"',
                        text,
                        re.S,
                    )
                    if ammo_id and reload and reload.group(1) != "magazine":
                        ammo.setdefault(gun_id, ammo_id.group(1))
        except zipfile.BadZipFile:
            continue
    return found, ammo


def tacz_gun_ids(mods_dir):
    """GunId set from every TaCZ gun-data file in the stand mods jars."""
    return tacz_gun_data(mods_dir)[0]


def tacz_inventory_ammo(mods_dir):
    """{gun_id: ammo_id} for inventory-reload guns (see tacz_gun_data)."""
    return tacz_gun_data(mods_dir)[1]


def sbw_weapon_items(mods_dir):
    """SuperbWarfare weapon item ids from the gun definitions in the stand mods.

    Each `assets/superbwarfare/sbw/guns/<id>.json` is one weapon whose item id
    is `superbwarfare:<id>`.
    """
    pattern = re.compile(r"assets/superbwarfare/sbw/guns/([^/]+)\.json$")
    found, skipped = [], []
    root = Path(mods_dir) if mods_dir else None
    jars = sorted(root.glob("*.jar")) if root and root.is_dir() else []
    for jar in jars:
        try:
            with zipfile.ZipFile(jar) as archive:
                for entry in archive.namelist():
                    match = pattern.search(entry)
                    if not match:
                        continue
                    item = f"superbwarfare:{match.group(1)}"
                    if any(part in item.lower() for part in SKIP_ITEMS):
                        skipped.append(item)
                        continue
                    if item not in found:
                        found.append(item)
        except zipfile.BadZipFile:
            continue
    return found, skipped


def items_count(rcon, player, item):
    match = re.search(
        r"(\d+)$",
        rcon.cmd(f"execute if items entity {player} container.* {item}") or "",
    )
    return int(match.group(1)) if match else 0


def ammo_count(rcon, player, slot=0):
    match = re.search(
        r"data: (\d+)$",
        rcon.cmd(
            f"data get entity {player} Inventory[{{Slot:{slot}b}}]"
            '.components."minecraft:custom_data".GunCurrentAmmoCount'
        )
        or "",
    )
    return int(match.group(1)) if match else None


def sbw_ammo_read(rcon, player):
    """(read_ok, ammo): strict mag read for the fired verdict.

    read_ok False means the RCON read itself failed (timeout, parse) — the
    mag state is unknown. (True, None) means the read succeeded but the item
    carries no Ammo NBT: the mag genuinely reads empty. An unreadable value
    is unknown, never evidence (server#365 sweep).
    """
    try:
        reply = rcon.cmd(f"data get entity {player} Inventory[{{Slot:0b}}]") or ""
    except (OSError, RuntimeError):
        return False, None
    match = re.search(r"Ammo:\s*(\d+)", reply)
    if match:
        return True, int(match.group(1))
    # A `data get` success names the entity data; anything else (a bare
    # "ok", an error line) is a failed read, not an empty mag.
    if "entity data:" in reply:
        return True, None
    return False, None


def sbw_ammo(rcon, player):
    """Rounds in the held SBW gun's mag (GunData.Ammo); None when unloaded."""
    return sbw_ammo_read(rcon, player)[1]


def sbw_wait_ammo(rcon, bot, player, detail):
    """Reload through the raw input SBW listens to, then poll the mag.

    Never aborts the leg: a gun that stays unloaded still runs the fire and
    victim steps, and the drain verdict decides. Returns True when loaded.
    """
    bot("rawinput key r")
    deadline = time.monotonic() + SBW_RELOAD_TIMEOUT
    loaded = sbw_ammo(rcon, player)
    while (loaded or 0) <= 0 and time.monotonic() < deadline:
        time.sleep(SBW_RELOAD_POLL)
        loaded = sbw_ammo(rcon, player)
    detail["reloaded_ammo"] = loaded
    return (loaded or 0) > 0


def sbw_settle_ammo(rcon, player, detail, timeout=6.0):
    """Poll the mag until two consecutive reads agree (top-up complete).

    sbw_wait_ammo returns on the first round in the mag while the top-up
    keeps filling through the victim baseline; a pre-shot snapshot then
    sits mid-fill and masks the dip. Returns the settled count or None.
    """
    last = sbw_ammo(rcon, player)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        current = sbw_ammo(rcon, player)
        if current is not None and current == last:
            detail["settled_ammo"] = current
            return current
        last = current
    detail["settled_ammo"] = last
    return last


def alive(rcon, player):
    try:
        return player in (rcon.cmd("list") or "")
    except (OSError, RuntimeError):
        return False


def restore_survival(rcon, player, detail):
    """Leave creative mode (minigun leg) before the drop/pickup/die phases."""
    if not detail.get("creative_for_minigun"):
        return
    try:
        rcon.cmd(f"gamemode survival {player}")
    except (OSError, RuntimeError):
        pass


def _rcon_read(rcon, command):
    """One RCON reply, retrying transient failures; a timeout is not a miss."""
    last = None
    for _ in range(max(1, READ_RETRIES)):
        try:
            return rcon.cmd(command)
        except (OSError, RuntimeError) as error:
            last = error
            time.sleep(READ_RETRY_SLEEP)
    raise last


def health(rcon, player):
    try:
        return hud_checks.parse_health(rcon.cmd(f"data get entity {player} Health"))
    except (ValueError, OSError, RuntimeError):
        return None


def victim_health(rcon, player):
    try:
        return hud_checks.parse_health(
            _rcon_read(
                rcon,
                f"execute in {VICTIM_DIM} run data get entity {VICTIM_SELECTOR} Health",
            )
        )
    except (ValueError, OSError, RuntimeError):
        return None


def victim_pos(rcon, player):
    try:
        return hud_checks.parse_pos(
            _rcon_read(
                rcon,
                f"execute in {VICTIM_DIM} run data get entity {VICTIM_SELECTOR} Pos",
            )
        )
    except (ValueError, OSError, RuntimeError):
        return None


def victim_baseline(rcon, player, detail):
    """Unfired control: target HP must be static over the window with no shot.

    Returns (stable, baseline): baseline is the freshest pre-shot reading for
    the fired comparison. A target that loses HP unfired, or any unreadable
    control read, fails loudly via stable=False, never PASS.
    """
    before = victim_health(rcon, player)
    pos_before = victim_pos(rcon, player)
    time.sleep(UNFIRED_CONTROL_SLEEP)
    baseline = victim_health(rcon, player)
    pos_after = victim_pos(rcon, player)
    detail["victim_control"] = [before, baseline]
    detail["victim_control_pos"] = [pos_before, pos_after]
    # An unreadable value is unknown, never evidence: the control needs two
    # valid, equal readings. A missing/unreadable target fails loudly via
    # stable=False (the caller maps it to control-unreadable), never PASS.
    detail["victim_control_unreadable"] = before is None or baseline is None
    if before is None or baseline is None:
        return False, baseline
    return baseline == before, baseline


def stable_baseline(rcon, player, detail):
    """Unfired control with one retry: the range restores a killed target 2 s
    after cancelling its death, so a control landing in that window retries
    once. The passing control is the shot baseline, so a retry cannot bias the
    fired evidence. Returns (ok, baseline)."""
    stable, baseline = victim_baseline(rcon, player, detail)
    if stable:
        return True, baseline
    detail["victim_rechecked"] = True
    time.sleep(UNFIRED_CONTROL_SLEEP)
    stable, baseline = victim_baseline(rcon, player, detail)
    return stable, baseline


def require_target(rcon, player):
    """True when the range's husk target exists for the victim leg."""
    return victim_health(rcon, player) is not None


def take_firing_position(rcon, player, detail):
    """Teleport onto the A10a lane for a clear 8 m shot at the husk.

    Returns True when the player lands on the lane; False (loud
    victim-lane-unreachable, never a silent miss) otherwise.
    """
    x, y, z = VICTIM_LANE_POS
    try:
        _rcon_read(rcon, f"tp {player} {x} {y} {z} {VICTIM_LANE_YAW} 0")
    except (OSError, RuntimeError):
        return False
    detail["victim_lane"] = [x, y, z]
    try:
        pos = hud_checks.parse_pos(_rcon_read(rcon, f"data get entity {player} Pos"))
    except (ValueError, OSError, RuntimeError):
        return False
    return pos is not None and abs(pos[0] - x) < 2 and abs(pos[2] - z) < 2


def clear_sbw_victims(rcon):
    try:
        rcon.cmd(f"kill {SBW_VICTIM_SELECTOR}")
    except (OSError, RuntimeError):
        pass


def sbw_victim_health(rcon, player):
    try:
        return hud_checks.parse_health(
            rcon.cmd(
                f"execute at {player} run data get entity {SBW_VICTIM_SELECTOR} Health"
            )
        )
    except (ValueError, OSError, RuntimeError):
        return None


def sbw_victim_pos(rcon, player):
    try:
        return hud_checks.parse_pos(
            rcon.cmd(
                f"execute at {player} run data get entity {SBW_VICTIM_SELECTOR} Pos"
            )
        )
    except (ValueError, OSError, RuntimeError):
        return None


def ensure_sbw_victim(rcon, player):
    """One pinned husk ahead of the stationary bot so SBW rounds hit an entity.

    Caret summon is facing-relative, so it lands ahead whatever the yaw is;
    the shooter then aims with absolute `lookat` coordinates, never `look`.
    Husk (not zombie) so daylight cannot kill the unfired control.
    """
    if sbw_victim_health(rcon, player) is None:
        rcon.cmd(
            f"execute at {player} run summon {SBW_VICTIM_TYPE} ^ ^ ^{SBW_VICTIM_AHEAD}"
            f' {{PersistenceRequired:1b,NoAI:1b,Silent:1b,Tags:["{SBW_VICTIM_TAG}"]}}'
        )
        # A caret summon lands at the shooter's height: ahead may be lower
        # ground, and the landing costs HP. Settle before any HP read so
        # fall damage can never pollute the unfired control.
        time.sleep(2.0)


def sbw_baseline(rcon, player, detail):
    """Unfired control: target HP must be static over the window with no shot.

    Returns (stable, baseline): the passing control doubles as the pre-shot
    reading for the fired comparison. A target that loses HP unfired, or any
    unreadable control read, fails loudly via stable=False, never PASS.
    """
    before = sbw_victim_health(rcon, player)
    pos_before = sbw_victim_pos(rcon, player)
    time.sleep(SBW_CONTROL_SLEEP)
    baseline = sbw_victim_health(rcon, player)
    pos_after = sbw_victim_pos(rcon, player)
    detail["victim_control"] = [before, baseline]
    detail["victim_control_pos"] = [pos_before, pos_after]
    # An unreadable value is unknown, never evidence: the control needs two
    # valid, equal readings. A missing/unreadable target fails loudly via
    # stable=False (the caller maps it to control-unreadable), never PASS.
    detail["victim_control_unreadable"] = before is None or baseline is None
    if before is None or baseline is None:
        return False, baseline
    return baseline == before, baseline


def find_flat_spot(bot, rcon, player, tries=5, step=25.0):
    """One-time relocation to ground flat enough for a landing husk.

    The bot then never moves again: every per-weapon victim is summoned onto
    the same flat ground, so landing damage can never pollute an HP reading.
    Returns (found, detail).
    """
    detail = {"tried": []}
    try:
        ppos = hud_checks.parse_pos(rcon.cmd(f"data get entity {player} Pos"))
    except (ValueError, OSError, RuntimeError):
        return False, detail
    x0, y0, z0 = ppos
    for i in range(max(1, tries)):
        x = x0 + i * step
        try:
            rcon.cmd(f"tp {player} {x} {y0} {z0} 0 0")
        except (OSError, RuntimeError):
            detail["tried"].append([x, "tp-failed"])
            continue
        time.sleep(1.0)
        if (health(rcon, player) or 0) < 20:
            detail["tried"].append([x, "player-hurt"])
            continue
        clear_sbw_victims(rcon)
        ensure_sbw_victim(rcon, player)
        hp = sbw_victim_health(rcon, player)
        vpos = sbw_victim_pos(rcon, player)
        try:
            ppos = hud_checks.parse_pos(rcon.cmd(f"data get entity {player} Pos"))
        except (ValueError, OSError, RuntimeError):
            ppos = None
        flat = (
            hp == 20.0
            and vpos is not None
            and ppos is not None
            and abs(vpos[1] - ppos[1]) <= 1.5
        )
        detail["tried"].append([x, hp, vpos, flat])
        if flat:
            detail["spot"] = [x, y0, z0]
            return True, detail
    return False, detail


def verify_sbw_persistence(rcon, player):
    """Run-level proof that the overworld keeps a summoned husk (30 s).

    Returns (ok, detail): ok=False (loud, no weapon fires) when the husk
    vanishes or loses HP unfired, i.e. this dimension deletes foreign mobs
    like the range does.
    """
    detail = {}
    clear_sbw_victims(rcon)
    time.sleep(0.5)
    ensure_sbw_victim(rcon, player)
    time.sleep(1.0)
    before = sbw_victim_health(rcon, player)
    pos_before = sbw_victim_pos(rcon, player)
    if before is None:
        return False, {"persist": [None, None], "persist_pos": [pos_before, None]}
    time.sleep(SBW_PERSISTENCE_SLEEP)
    after = sbw_victim_health(rcon, player)
    pos_after = sbw_victim_pos(rcon, player)
    detail["persist"] = [before, after]
    detail["persist_pos"] = [pos_before, pos_after]
    return after is not None and after == before, detail


def ensure_overworld(bot, rcon, player):
    """SBW leg runs outside the range: leave it after a TaCZ weapon."""
    try:
        state = bot("state")
    except (OSError, RuntimeError):
        return
    if dimension(state).endswith("range"):
        bot("bfaction leave")
        time.sleep(1.5)
        bot("closescreen")
        time.sleep(0.5)


def aim_at_victim(bot, rcon, player):
    """Aim at the victim's chest; False when its position is unreadable."""
    pos = victim_pos(rcon, player)
    if pos is None:
        return False
    x, y, z = pos
    bot(f"lookat {x} {y + 1.0} {z}")
    time.sleep(0.6)
    return True


def return_to_spot(rcon, bot, player, spot, detail):
    """Teleport back onto the verified flat spot before a victim summon.

    The moving-fire phase walks the bot away every weapon; without this each
    victim is summoned at drifted terrain (holes, ledges) and the aim misses.
    Loud failure, never a silent miss.
    """
    if not spot:
        return True
    x, y, z = spot
    try:
        rcon.cmd(f"tp {player} {x} {y} {z} 0 0")
    except (OSError, RuntimeError):
        return False
    time.sleep(0.5)
    try:
        pos = hud_checks.parse_pos(rcon.cmd(f"data get entity {player} Pos"))
    except (ValueError, OSError, RuntimeError):
        return False
    ok = pos is not None and abs(pos[0] - x) < 3 and abs(pos[2] - z) < 3
    detail["recentered"] = ok
    return ok


def sbw_aim_at_victim(bot, rcon, player):
    """Aim at the husk's chest; False when its position is unreadable."""
    pos = sbw_victim_pos(rcon, player)
    if pos is None:
        return False
    x, y, z = pos
    bot(f"lookat {x} {y + 1.0} {z}")
    time.sleep(0.6)
    return True


def new_log_lines(log_path, position):
    """(lines, position); None lines when the log is unreadable (never PASS)."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            handle.seek(position)
            lines = handle.read().splitlines()
            return lines, handle.tell()
    except OSError:
        return None, position


def ensure_in_range(bot, rcon, player):
    state = bot("state")
    if not dimension(state).endswith("range"):
        bot("bfaction range:enter")
        time.sleep(1.5)
        bot("closescreen")
        time.sleep(0.5)
    if (health(rcon, player) or 0) <= 0:
        bot("respawn")
        time.sleep(1.5)
        bot("closescreen")
        if not dimension(bot("state")).endswith("range"):
            bot("bfaction range:enter")
            time.sleep(1.5)
            bot("closescreen")
            time.sleep(0.5)


def tacz_shoot(bot, attempts=5):
    """Single shot with retries: right after join/re-give the client gun data
    may lag a few seconds behind, every attempt reporting UNKNOWN_FAIL."""
    result = {"result": "NO_ATTEMPT"}
    for _ in range(max(1, attempts)):
        result = bot("tacz shoot") or {}
        if result.get("result") == "SUCCESS":
            return result
        time.sleep(2.0)
    return result


def wait_drawn(bot, timeout=12.0):
    """TaCZ draw must finish before the first shot, else UNKNOWN_FAIL."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = bot("tacz status") or {}
        except (OSError, RuntimeError):
            return
        if not status.get("stateLock") and not status.get("drawCooldownMs"):
            return
        time.sleep(0.5)


def sweep_tacz(rcon, bot, player, gun_id, log_path, log_pos, detail, inv_ammo=""):
    ammo_id = inv_ammo or ""
    if ammo_id:
        # Inventory-reload gun (today only tacz:minigun): every shot needs a
        # visible inventory stack whose AmmoId matches, but the stand rig
        # vacuums all loose ammo in <0.3 s and TaCZ's per-shot scan sees only
        # the vanilla inventory (only canReload is rig-aware). Creative skips
        # that check; the victim leg still proves real projectiles. The ammo
        # is still given (right id + AmmoId NBT) so the rig count is honest.
        rcon.cmd(f"gamemode creative {player}")
        detail["creative_for_minigun"] = True
    try:
        return _sweep_tacz(
            rcon, bot, player, gun_id, log_path, log_pos, detail, ammo_id
        )
    finally:
        # One restore for every exit: an early return must never leak
        # creative mode into the next weapon's checks.
        restore_survival(rcon, player, detail)


def _sweep_tacz(rcon, bot, player, gun_id, log_path, log_pos, detail, ammo_id):
    rcon.cmd(hud_checks.gun_replace_command(player, gun_id, ammo=300))
    time.sleep(0.4)
    if ammo_id:
        rcon.cmd(
            f"item replace entity {player} inventory.1 with tacz:ammo"
            f'[minecraft:custom_data={{AmmoId:"{ammo_id}"}}] 1'
        )
        time.sleep(0.5)
    detail["gave"] = items_count(rcon, player, KIT_GUN) >= 1
    if not detail["gave"]:
        return "server-give-failed", log_pos
    if not alive(rcon, player):
        return "server-dead-after-give", log_pos
    bot("slot 0")
    wait_drawn(bot)
    detail["selected"] = bot("state").get("held") == KIT_GUN
    before = ammo_count(rcon, player)
    bot("look 0 55")
    single = tacz_shoot(bot)
    detail["single"] = single.get("result")
    if not alive(rcon, player):
        return "server-dead-after-single", log_pos
    if single.get("result") == "ID_NOT_EXIST" and detail["gave"]:
        # The server hands out a gun id the stand client never loaded
        # (elitex:ak47x class): the sweep missed that weapon, it did not pass.
        # Checked after liveness so a crash is reported as the crash.
        return "client-gun-data-missing", log_pos
    bot("hold key.attack 20")
    time.sleep(1.4)
    after = ammo_count(rcon, player)
    detail["ammo"] = [before, after]
    detail["fired"] = (
        before is not None and after is not None and after < before
    ) or detail["single"] == "SUCCESS"
    if not alive(rcon, player):
        return "server-dead-after-hold", log_pos
    bot("tacz reload")
    time.sleep(1.8)
    if not alive(rcon, player):
        return "server-dead-after-reload", log_pos
    bot("tacz aim true")
    time.sleep(0.4)
    tacz_shoot(bot, attempts=2)
    time.sleep(0.8)
    bot("tacz aim false")
    if not alive(rcon, player):
        return "server-dead-after-aim", log_pos
    modes = {}
    for mode in FIRE_MODES:
        rcon.cmd(
            f"item replace entity {player} hotbar.0 with {KIT_GUN}"
            f'[minecraft:custom_data={{GunId:"{gun_id}",GunCurrentAmmoCount:300,'
            f'HasBulletInBarrel:1b,GunFireMode:"{mode}"}}]'
        )
        time.sleep(0.3)
        wait_drawn(bot)
        modes[mode] = tacz_shoot(bot, attempts=2).get("result")
        time.sleep(0.6)
        if not alive(rcon, player):
            detail["modes"] = modes
            return "server-dead-after-mode-" + mode, log_pos
        if mode == "AUTO":
            bot("hold key.attack 20")
            time.sleep(1.4)
            detail["burst"] = ammo_count(rcon, player)
            if not alive(rcon, player):
                detail["modes"] = modes
                return "server-dead-after-burst", log_pos
    detail["modes"] = modes
    bot("slot 1")
    time.sleep(0.2)
    bot("slot 0")
    wait_drawn(bot)
    bot("hold key.forward 20")
    detail["moving_single"] = tacz_shoot(bot, attempts=2).get("result")
    time.sleep(1.4)
    bot("hold key.forward 0")
    if not alive(rcon, player):
        return "server-dead-after-moving", log_pos
    if not take_firing_position(rcon, player, detail):
        return "victim-lane-unreachable", log_pos
    ok, hp_ref = stable_baseline(rcon, player, detail)
    if not ok:
        stop = (
            "control-unreadable"
            if detail.get("victim_control_unreadable")
            else "victim-died-unfired"
        )
        return stop, log_pos
    if aim_at_victim(bot, rcon, player):
        tacz_shoot(bot, attempts=2)
        time.sleep(0.8)
        hp_mid = victim_health(rcon, player)
    else:
        hp_mid = None
    hp_after = victim_health(rcon, player)
    detail["victim_hp"] = [hp_ref, hp_mid, hp_after]
    # Range targets cancel death and restore HP 2 s later, so the mid-shot
    # reading is the primary evidence; either depressed reading proves a hit.
    detail["victim_hit"] = hp_ref is not None and any(
        h is not None and h < hp_ref for h in (hp_mid, hp_after)
    )
    if not alive(rcon, player):
        return "server-dead-after-victim", log_pos
    detail["reselected"] = bot("state").get("held") == KIT_GUN
    if not alive(rcon, player):
        return "server-dead-after-reselect", log_pos
    return None, log_pos


def sweep_drop_pickup_die(
    rcon, bot, player, item, log_path, log_pos, detail, kind="tacz"
):
    bot("press key.keyboard.q")
    time.sleep(0.6)
    detail["dropped"] = items_count(rcon, player, item) == 0
    if not alive(rcon, player):
        return "server-dead-after-drop", log_pos
    deadline = time.monotonic() + 6
    picked = False
    while time.monotonic() < deadline:
        if items_count(rcon, player, item) >= 1:
            picked = True
            break
        time.sleep(0.5)
    detail["picked_up"] = picked
    bot("slot 0")
    rcon.cmd(f"kill {player}")
    deadline = time.monotonic() + RESPAWN_TIMEOUT
    back = False
    while time.monotonic() < deadline:
        if not alive(rcon, player):
            return "server-dead-after-kill", log_pos
        if (health(rcon, player) or 0) > 0:
            back = True
            break
        time.sleep(0.5)
    detail["respawned"] = back
    if not back:
        # The kill landed but the bot never came back with HP (dead or the
        # health reads failed): loud, never a silent fall-through into a
        # passing row.
        return "respawn-failed", log_pos
    if kind == "tacz":
        ensure_in_range(bot, rcon, player)
    if not alive(rcon, player):
        return "server-dead-after-respawn", log_pos
    return None, log_pos


def sweep_weapon(
    rcon, bot, player, kind, weapon_id, log_path, log_pos, inv_ammo="", spot=None
):
    detail = {"kind": kind}
    if kind == "tacz":
        ensure_in_range(bot, rcon, player)
    else:
        ensure_overworld(bot, rcon, player)
    if kind == "tacz":
        stop, log_pos = sweep_tacz(
            rcon, bot, player, weapon_id, log_path, log_pos, detail, inv_ammo
        )
        if stop:
            return stop, detail, log_pos
        item = KIT_GUN
    else:
        rcon.cmd(f"item replace entity {player} hotbar.0 with {weapon_id}")
        time.sleep(0.4)
        detail["gave"] = items_count(rcon, player, weapon_id) >= 1
        if not detail["gave"]:
            return "server-give-failed", detail, log_pos
        if not alive(rcon, player):
            return "server-dead-after-give", detail, log_pos
        # A bare SBW give never fires (empty mag). The creative box feeds
        # every gun; the rig vacuum collects it and SBW reloads read the rig
        # too. Reload through raw input: KeyMapping-only presses never reach
        # SBW's ClickEventHandler (raw Key event) to dispatch ReloadMessage.
        rcon.cmd(f"item replace entity {player} inventory.0 with {SBW_CREATIVE_BOX}")
        time.sleep(0.5)
        bot("slot 0")
        wait_drawn(bot)
        detail["selected"] = bot("state").get("held") == weapon_id
        bot("look 0 55")
        sbw_wait_ammo(rcon, bot, player, detail)
        # Fire through raw input too: SBW polls its own FIRE mapping (LMB) per
        # tick and the press edge as a raw MouseButton event; testbot's
        # KeyMapping-only holds never reach either.
        bot("rawinput click 1")
        time.sleep(0.8)
        if not alive(rcon, player):
            return "server-dead-after-single", detail, log_pos
        bot("rawinput mouse_down 1")
        time.sleep(1.4)
        bot("rawinput mouse_up 1")
        if not alive(rcon, player):
            return "server-dead-after-hold", detail, log_pos
        # Aimed victim shots while the bot is still on the flat spot: the
        # moving-fire step below walks the bot away, so the victim leg runs
        # before it, never after. Level the view first: the pre-fire look
        # pitches down 55 deg and a caret summon inherits that pitch, which
        # would bury the victim underground (probe5: [16,11] at y=-48).
        bot("look 0 0")
        time.sleep(0.5)
        if not return_to_spot(rcon, bot, player, spot, detail):
            return "victim-spot-unreachable", detail, log_pos
        clear_sbw_victims(rcon)
        time.sleep(0.5)
        ensure_sbw_victim(rcon, player)
        # Top up after the pre-fire drain: a low-cap mag (bocek: 1) would
        # otherwise reach the victim leg dry and every row would read
        # unfired. The drain verdict still decides. Settle first: the
        # top-up keeps filling after the first round lands, and a pre-shot
        # snapshot mid-fill masks the dip.
        sbw_wait_ammo(rcon, bot, player, detail)
        detail["ammo_before_victim"] = sbw_settle_ammo(rcon, player, detail)
        ok, hp_ref = sbw_baseline(rcon, player, detail)
        if not ok:
            stop = (
                "control-unreadable"
                if detail.get("victim_control_unreadable")
                else "victim-died-unfired"
            )
            return stop, detail, log_pos
        if sbw_aim_at_victim(bot, rcon, player):
            bot("rawinput click 1")
            time.sleep(0.8)
            hp_mid = sbw_victim_health(rcon, player)
            ammo_mid = sbw_ammo(rcon, player)
            bot("rawinput mouse_down 1")
            # The mag refills mid-leg (top-up still filling, auto-reload):
            # endpoints alone miss a dip, so track the lowest reading seen
            # while the trigger is held. Firing only removes, reload only
            # adds, so a dip below the pre-shot mag proves the shot.
            ammo_low = ammo_mid
            deadline = time.monotonic() + 1.4
            while time.monotonic() < deadline:
                time.sleep(0.3)
                low = sbw_ammo(rcon, player)
                if low is not None and (ammo_low is None or low < ammo_low):
                    ammo_low = low
            bot("rawinput mouse_up 1")
        else:
            hp_mid = None
            ammo_mid = None
            ammo_low = None
        hp_after = sbw_victim_health(rcon, player)
        before = detail.get("ammo_before_victim")
        after = sbw_ammo(rcon, player)
        if after is None and before is not None:
            # An emptied mag reads no Ammo; a failed read reads the same.
            # One confirm separates the two before calling it drained.
            time.sleep(0.5)
            after = sbw_ammo(rcon, player)
        detail["ammo_after_victim"] = after
        detail["ammo_mid_victim"] = ammo_mid
        detail["ammo_low_victim"] = ammo_low
        detail["victim_hp"] = [hp_ref, hp_mid, hp_after]
        # Coordinator decision (server#365 c9): the fired verdict is the
        # server-side magazine drain with the server alive; the victim HP is
        # secondary evidence, never the verdict. A kill re-read still
        # separates a dead victim from one failed read for the report.
        if hp_ref is not None and hp_mid is None and hp_after is None:
            if sbw_victim_health(rcon, player) is None:
                detail["victim_killed"] = True
            else:
                hp_after = sbw_victim_health(rcon, player)
                detail["victim_hp"] = [hp_ref, hp_mid, hp_after]
        detail["victim_hit"] = hp_ref is not None and any(
            h is not None and h < hp_ref for h in (hp_mid, hp_after)
        )
        seen = [a for a in (ammo_mid, ammo_low, after) if a is not None]
        drained = before is not None and any(a < before for a in seen)
        if not drained and before is not None and not seen:
            # Every leg read came back with no Ammo number: either the mag
            # fired dry (a successful read with no Ammo NBT) or the reads
            # failed. Only a positively read empty mag counts as drained;
            # unreadable is unknown, never evidence, so it fails loudly.
            read_ok, confirmed = sbw_ammo_read(rcon, player)
            if read_ok and confirmed is None:
                detail["ammo_emptied"] = True
                drained = True
            elif read_ok:
                # The confirmation read a number: judge it like any leg read.
                detail["ammo_confirmed"] = confirmed
                drained = confirmed < before
            else:
                detail["ammo_unreadable"] = True
                return "ammo-unreadable", detail, log_pos
        detail["fired"] = drained
        if not alive(rcon, player):
            return "server-dead-after-victim", detail, log_pos
        bot("rawinput mouse_down 1")
        time.sleep(1.4)
        bot("rawinput mouse_up 1")
        bot("hold key.forward 20")
        time.sleep(0.5)
        bot("rawinput click 1")
        time.sleep(1.0)
        bot("hold key.forward 0")
        detail["moving_single"] = True
        if not alive(rcon, player):
            return "server-dead-after-moving", detail, log_pos
        bot("slot 1")
        time.sleep(0.2)
        bot("slot 0")
        detail["reselected"] = bot("state").get("held") == weapon_id
        if not alive(rcon, player):
            return "server-dead-after-reselect", detail, log_pos
        item = weapon_id
    stop, log_pos = sweep_drop_pickup_die(
        rcon, bot, player, item, log_path, log_pos, detail, kind
    )
    lines, log_pos = new_log_lines(log_path, log_pos)
    if lines is None:
        detail["log_unreadable"] = True
        return "server-log-unreadable", detail, log_pos
    hits = [line[-220:] for line in lines if LOG_WATCH.search(line)][:5]
    if hits:
        detail["log_hits"] = hits
        return "server-log-exception", detail, log_pos
    if stop:
        return stop, detail, log_pos
    if not detail.get("gave"):
        return "server-give-failed", detail, log_pos
    if not detail.get("fired"):
        return "inconclusive-no-shot", detail, log_pos
    return None, detail, log_pos


def load_rows(results_path):
    """Previously recorded rows, so a cut run continues where it stopped."""
    rows = []
    try:
        with open(results_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and "weapon" in row:
                    rows.append(row)
    except OSError:
        pass
    return rows


def passes_to_skip(rows):
    """Weapons with a recorded PASS; failures rerun, never silently dropped."""
    return {row["weapon"] for row in rows if row.get("stop") is None}


def merged_verdict(prev_rows, new_rows):
    """All rows with new rows winning; (rows, passed, inconclusive, failed)."""
    by_weapon = {row["weapon"]: row for row in prev_rows}
    by_weapon.update({row["weapon"]: row for row in new_rows})
    rows = list(by_weapon.values())
    passed = sum(1 for row in rows if row.get("stop") is None)
    inconclusive = sum(1 for row in rows if row.get("stop") == "inconclusive-no-shot")
    return rows, passed, inconclusive, len(rows) - passed - inconclusive


def record_result(results_path, row):
    with open(results_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def write_progress(results_path, rows):
    passed = sum(1 for r in rows if r["stop"] is None)
    inconclusive = sum(1 for r in rows if r["stop"] == "inconclusive-no-shot")
    failed = len(rows) - passed - inconclusive
    last = rows[-1] if rows else {}
    progress = results_path.with_name("progress.md")
    try:
        progress.write_text(
            "# weapons-sweep progress\n\n"
            f"- swept: {len(rows)}, pass {passed},"
            f" inconclusive {inconclusive}, fail {failed}\n"
            f"- last: {last.get('weapon')} {last.get('stop')}\n"
            "- resume: rerun with the same --results path\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    from devstack import SERVER

    server_dir = Path(args.server_dir) if args.server_dir else Path(SERVER)
    log_path = server_dir / "logs" / "latest.log"
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    results_path = Path(args.results) if args.results else None
    if results_path:
        try:
            results_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    prev_rows = load_rows(results_path) if results_path else []
    skip = passes_to_skip(prev_rows)
    results = []
    try:
        player = bot("state")["name"]
        assert re.fullmatch(r"[A-Za-z0-9_]{1,16}", player), player
        clear_sbw_victims(rcon)
        if args.kind in ("tacz", "all"):
            ensure_in_range(bot, rcon, player)
            if not require_target(rcon, player):
                print(f"no range target {VICTIM_SELECTOR}; aborting", flush=True)
                return 1
        wanted = set(args.weapons.split(",")) if args.weapons else None
        inventory = []
        inv_ammo = {}
        if args.kind in ("tacz", "all"):
            mods_dir = args.mods_dir or (server_dir / "mods")
            guns = tacz_gun_data(str(mods_dir))
            inv_ammo = guns[1]
            for gun_id, jar in sorted(guns[0].items()):
                if wanted and gun_id not in wanted:
                    continue
                inventory.append(("tacz", gun_id))
        skipped_sbw = []
        if args.kind in ("sbw", "all"):
            mods_dir = args.mods_dir or (server_dir / "mods")
            weapons, skipped_sbw = sbw_weapon_items(str(mods_dir))
            for item in weapons:
                if wanted and item not in wanted:
                    continue
                inventory.append(("sbw", item))
        if args.list_only:
            print(
                json.dumps(
                    {
                        "inventory": [f"{kind}:{wid}" for kind, wid in inventory],
                        "sbw_skipped": skipped_sbw,
                    },
                    indent=1,
                )
            )
            return 0
        print(f"sweeping {len(inventory)} weapons as {player}", flush=True)
        if skip:
            skipped = [w for _, w in inventory if w in skip]
            inventory = [(k, w) for k, w in inventory if w not in skip]
            print(f"resuming: skipping {len(skipped)} passed weapons", flush=True)
        if any(kind == "sbw" for kind, _ in inventory):
            # The whole SBW leg stands or falls on flat overworld ground that
            # keeps a summoned husk: find it once, prove 30 s persistence,
            # then recenter onto it before every victim summon (the moving
            # phase walks the bot away every weapon).
            ensure_overworld(bot, rcon, player)
            found, spot = find_flat_spot(bot, rcon, player)
            print(f"sbw spot: {json.dumps(spot)[:500]}", flush=True)
            if not found:
                print("SBW ABORTED: no flat spot", flush=True)
                return 1
            sbw_spot = spot.get("spot")
            ok, persist = verify_sbw_persistence(rcon, player)
            print(f"sbw persistence: {json.dumps(persist)[:300]}", flush=True)
            if not ok:
                print("SBW ABORTED: overworld does not keep the husk", flush=True)
                return 1
        else:
            sbw_spot = None
        out = []
        log_pos = log_path.stat().st_size if log_path.exists() else 0
        crashed = None
        for kind, weapon_id in inventory:
            try:
                stop, detail, log_pos = sweep_weapon(
                    rcon,
                    bot,
                    player,
                    kind,
                    weapon_id,
                    log_path,
                    log_pos,
                    inv_ammo.get(weapon_id, ""),
                    sbw_spot,
                )
            except (
                OSError,
                RuntimeError,
                TimeoutError,
                ConnectionError,
                EOFError,
            ) as error:
                stop, detail = "rcon-or-bot-lost", {"error": f"{error!r}"[:200]}
            results.append({"weapon": weapon_id, "stop": stop, **detail})
            if results_path:
                record_result(results_path, results[-1])
                merged, _, _, _ = merged_verdict(prev_rows, results)
                write_progress(results_path, merged)
            if stop in (None, "inconclusive-no-shot"):
                out.append(
                    check(f"sweep {weapon_id}", stop is None, json.dumps(detail)[:300])
                )
            else:
                out.append(
                    check(
                        f"sweep {weapon_id}",
                        False,
                        f"{stop} {json.dumps(detail)[:300]}",
                    )
                )
                if stop.startswith("server-dead") or stop in ("rcon-or-bot-lost",):
                    crashed = weapon_id
                    break
        print(json.dumps({"results": results}, indent=1)[:6000])
        tally(out)
        merged, passed, inconclusive, failed = merged_verdict(prev_rows, results)
        print(
            f"merged verdict over {len(merged)} weapons:"
            f" pass {passed}, inconclusive {inconclusive}, fail {failed}",
            flush=True,
        )
        if crashed:
            print(
                f"SERVER DIED during {crashed}; restart the stand then rerun"
                " with the same --results path to continue",
                flush=True,
            )
            return 1
        return 1 if failed else 0
    finally:
        try:
            clear_sbw_victims(rcon)
            bot("bfaction leave")
        except (OSError, RuntimeError):
            pass
        bot.close()


def register(sub):
    parser = sub.add_parser("weapons-sweep", help=SCENARIO["help"])
    parser.add_argument("--kind", choices=("tacz", "sbw", "all"), default="all")
    parser.add_argument("--weapons", default="", help="comma-separated id filter")
    parser.add_argument("--mods-dir", default="", help="stand server mods dir")
    parser.add_argument("--server-dir", default="")
    parser.add_argument(
        "--list-only", action="store_true", help="print inventory, no firing"
    )
    parser.add_argument(
        "--results",
        default="",
        help="jsonl path: append each weapon row + progress.md, skip passed on rerun",
    )
    rcon_args(parser)
    parser.set_defaults(func=run)
