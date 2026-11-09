#!/usr/bin/env python3
"""weapons-sweep: fire every registered weapon once, server must stay up.

For each weapon id (TaCZ gun packs from the stand mods plus SuperbWarfare gun
definitions): give to the bot, select, single fire, hold fire, reload, aim,
cycle fire modes (single + burst hold in AUTO), moving fire, switch away/back,
drop, pick up, die holding it. Every shot goes into the blocks ahead so
explosive rounds detonate against the world; then a NoAI zombie victim is
summoned ahead of the player and shot at with `lookat` aim so bullets hit an
entity (BodyPart/headshot paths), explosive rounds included. After every step
the server must answer RCON, and no new server-log exception may appear for
that weapon.

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
VICTIM_TYPE = "minecraft:zombie"
VICTIM_TAG = "sweep_victim"
VICTIM_SELECTOR = f"@e[type={VICTIM_TYPE},tag={VICTIM_TAG},limit=1,sort=nearest]"
# Unfired-control window: the victim's HP must be static over this long with no
# shot, matching the post-shot observation window below. Zombie daylight burn
# used to kill the victim on its own, so HP drops without a shot once counted
# as fired evidence (server#365 sweep).
UNFIRED_CONTROL_SLEEP = 2.2

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


def tacz_gun_ids(mods_dir):
    """GunId set from every TaCZ gun-data file in the stand mods jars.

    Packs embed their data tree (`.../custom/<pack>/data/<ns>/data/guns/`)
    and the data file carries no id: `<ns>:<stem>` minus the `_data` suffix.
    """
    pattern = re.compile(r"data/([^/]+)/data/guns/([^/]+)_data\.json$")
    found = {}
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
                    if re.fullmatch(r"[a-z0-9_]+:[a-z0-9_/]+", gun_id):
                        found.setdefault(gun_id, jar.name)
        except zipfile.BadZipFile:
            continue
    return found


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


def alive(rcon, player):
    try:
        return player in (rcon.cmd("list") or "")
    except (OSError, RuntimeError):
        return False


def health(rcon, player):
    try:
        return hud_checks.parse_health(rcon.cmd(f"data get entity {player} Health"))
    except (ValueError, OSError, RuntimeError):
        return None


def victim_health(rcon, player):
    try:
        return hud_checks.parse_health(
            rcon.cmd(
                f"execute at {player} run data get entity {VICTIM_SELECTOR} Health"
            )
        )
    except (ValueError, OSError, RuntimeError):
        return None


def victim_pos(rcon, player):
    try:
        return hud_checks.parse_pos(
            rcon.cmd(f"execute at {player} run data get entity {VICTIM_SELECTOR} Pos")
        )
    except (ValueError, OSError, RuntimeError):
        return None


def secure_victim_environment(rcon):
    """Midnight + frozen clock so the NoAI zombie victim cannot burn on its own.

    Called once per sweep run; the per-weapon unfired control below stays as
    the asserting guard.
    """
    rcon.cmd("time set midnight")
    rcon.cmd("gamerule doDaylightCycle false")


def victim_baseline(rcon, player, detail):
    """Unfired control: victim HP must be static over the window with no shot.

    Returns (stable, baseline): baseline is the freshest pre-shot reading for
    the fired comparison. A victim that loses HP unfired (daylight burn) fails
    loudly via stable=False; a missing victim stays stable with a None baseline
    so the shot path still reports inconclusive-no-shot, never PASS.
    """
    before = victim_health(rcon, player)
    pos_before = victim_pos(rcon, player)
    time.sleep(UNFIRED_CONTROL_SLEEP)
    baseline = victim_health(rcon, player)
    pos_after = victim_pos(rcon, player)
    detail["victim_control"] = [before, baseline]
    detail["victim_control_pos"] = [pos_before, pos_after]
    if before is None:
        return True, baseline
    return baseline is not None and baseline == before, baseline


def ensure_victim(rcon, player):
    """One NoAI zombie ahead of the player so bullets hit an entity, not blocks.

    Caret summon is facing-relative, so it lands ahead whatever the yaw is;
    the shooter then aims with absolute `lookat` coordinates, never `look`.
    The iron helmet stops daylight burn on its own (belt and braces with the
    midnight clock from secure_victim_environment).
    """
    if victim_health(rcon, player) is None:
        # Level the pitch first: the pre-fire phases leave the player looking
        # down, and caret summon inherits that rotation, landing the zombie in
        # the ground where it dies within a second (every unfired "victim_hp
        # [20.0, None]" row). Keep position and yaw (down-lane), zero pitch.
        rcon.cmd(f"execute at {player} run tp {player} ~ ~ ~ ~ 0")
        rcon.cmd(
            f"execute at {player} run summon {VICTIM_TYPE} ^ ^ ^8"
            f' {{PersistenceRequired:1b,NoAI:1b,Silent:1b,Tags:["{VICTIM_TAG}"],'
            'ArmorItems:[{},{},{},{id:"minecraft:iron_helmet",Count:1b}]}'
        )


def ensure_stable_victim(rcon, player, detail):
    """Summon + unfired control, with one re-summon if the first victim dies
    on its own: a delayed blast from the pre-fire-into-blocks phases can catch
    the summon spot. The passing control is the shot baseline, so a re-summon
    cannot bias the fired evidence. Returns (ok, baseline)."""
    ensure_victim(rcon, player)
    stable, baseline = victim_baseline(rcon, player, detail)
    if stable:
        return True, baseline
    detail["victim_resummoned"] = True
    clear_victims(rcon)
    time.sleep(UNFIRED_CONTROL_SLEEP)
    ensure_victim(rcon, player)
    stable, baseline = victim_baseline(rcon, player, detail)
    return stable, baseline


def clear_victims(rcon):
    try:
        rcon.cmd(f"kill {VICTIM_SELECTOR}")
    except (OSError, RuntimeError):
        pass


def aim_at_victim(bot, rcon, player):
    """Aim at the victim's chest; False when its position is unreadable."""
    pos = victim_pos(rcon, player)
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


def sweep_tacz(rcon, bot, player, gun_id, log_path, log_pos, detail):
    rcon.cmd(hud_checks.gun_replace_command(player, gun_id, ammo=300))
    time.sleep(0.4)
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
    ok, hp_ref = ensure_stable_victim(rcon, player, detail)
    if not ok:
        return "victim-died-unfired", log_pos
    if aim_at_victim(bot, rcon, player):
        tacz_shoot(bot, attempts=2)
        time.sleep(0.8)
    hp_after = victim_health(rcon, player)
    detail["victim_hp"] = [hp_ref, hp_after]
    # A dead/missing victim after the shot means a round connected. hp_ref is
    # the post-control baseline, so a daylight-burn drop can never count here.
    detail["victim_hit"] = hp_ref is not None and (
        hp_after is None or hp_after < hp_ref
    )
    if not alive(rcon, player):
        return "server-dead-after-victim", log_pos
    detail["reselected"] = bot("state").get("held") == KIT_GUN
    if not alive(rcon, player):
        return "server-dead-after-reselect", log_pos
    return None, log_pos


def sweep_drop_pickup_die(rcon, bot, player, item, log_path, log_pos, detail):
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
    deadline = time.monotonic() + 25
    back = False
    while time.monotonic() < deadline:
        if not alive(rcon, player):
            return "server-dead-after-kill", log_pos
        if (health(rcon, player) or 0) > 0:
            back = True
            break
        time.sleep(0.5)
    detail["respawned"] = back
    ensure_in_range(bot, rcon, player)
    if not alive(rcon, player):
        return "server-dead-after-respawn", log_pos
    return None, log_pos


def sweep_weapon(rcon, bot, player, kind, weapon_id, log_path, log_pos):
    detail = {"kind": kind}
    ensure_in_range(bot, rcon, player)
    if kind == "tacz":
        stop, log_pos = sweep_tacz(
            rcon, bot, player, weapon_id, log_path, log_pos, detail
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
        bot("slot 0")
        wait_drawn(bot)
        detail["selected"] = bot("state").get("held") == weapon_id
        bot("look 0 55")
        bot("attack")
        time.sleep(0.8)
        if not alive(rcon, player):
            return "server-dead-after-single", detail, log_pos
        bot("hold key.attack 20")
        time.sleep(1.4)
        bot("hold key.use 20")
        time.sleep(1.2)
        if not alive(rcon, player):
            return "server-dead-after-hold", detail, log_pos
        bot("hold key.attack 20")
        time.sleep(1.4)
        bot("hold key.forward 20")
        time.sleep(0.5)
        bot("attack")
        time.sleep(1.0)
        bot("hold key.forward 0")
        detail["moving_single"] = True
        if not alive(rcon, player):
            return "server-dead-after-moving", detail, log_pos
        ok, hp_ref = ensure_stable_victim(rcon, player, detail)
        if not ok:
            return "victim-died-unfired", detail, log_pos
        if aim_at_victim(bot, rcon, player):
            bot("attack")
            time.sleep(0.8)
            bot("hold key.attack 20")
            time.sleep(1.4)
        hp_after = victim_health(rcon, player)
        detail["victim_hp"] = [hp_ref, hp_after]
        # SBW guns expose no ammo counter: a wounded or killed victim confirms
        # a shot (a dead victim reads None, which is a hit, not a miss).
        # hp_ref is the post-control baseline, so a daylight-burn drop can
        # never count as fired evidence.
        detail["fired"] = hp_ref is not None and (hp_after is None or hp_after < hp_ref)
        if not alive(rcon, player):
            return "server-dead-after-victim", detail, log_pos
        bot("slot 1")
        time.sleep(0.2)
        bot("slot 0")
        detail["reselected"] = bot("state").get("held") == weapon_id
        if not alive(rcon, player):
            return "server-dead-after-reselect", detail, log_pos
        item = weapon_id
    stop, log_pos = sweep_drop_pickup_die(
        rcon, bot, player, item, log_path, log_pos, detail
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
        clear_victims(rcon)
        secure_victim_environment(rcon)
        wanted = set(args.weapons.split(",")) if args.weapons else None
        inventory = []
        if args.kind in ("tacz", "all"):
            mods_dir = args.mods_dir or (server_dir / "mods")
            for gun_id, jar in sorted(tacz_gun_ids(str(mods_dir)).items()):
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
        out = []
        log_pos = log_path.stat().st_size if log_path.exists() else 0
        crashed = None
        for kind, weapon_id in inventory:
            try:
                stop, detail, log_pos = sweep_weapon(
                    rcon, bot, player, kind, weapon_id, log_path, log_pos
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
            clear_victims(rcon)
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
