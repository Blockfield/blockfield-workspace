#!/usr/bin/env python3
"""Release-candidate battery: version gate plus smoke of the release-critical paths.

One summary table row per check with evidence lines; non-zero exit on any FAIL.
Needs two headless clients with the testbot; the run refuses to start when the
second bot port does not answer (a bot-less second client must fail loudly,
never degrade to SKIP rows).
"""

import json
import re
import time
import tomllib
from pathlib import Path

from . import check_gameplay, hitreg, readiness
from .common import (
    Bot,
    check,
    connect_rcon,
    dimension,
    is_lobby_dimension,
    rcon_args,
    tally,
    wait_in_game,
)

SCENARIO = {
    "name": "rc",
    "help": "release-candidate battery: versions, kills, tablet, hitreg, logs, save-all",
}
RESPAWN = "blockfield_shared:respawn"
ROOM_RE = re.compile(r"Комната (\d+) открыта")
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)(?:\+mc\d+\.\d+\.\d+)?\s+commit")
FABRIC_MOD_RE = re.compile(r"blockfield[ _-]v?(\d+\.\d+\.\d+)", re.IGNORECASE)
ERROR_RE = re.compile(
    r"(?i)\b(ERROR|Exception in|Traceback \(most recent|FAILED|panic:)\b"
)
# Bracket level in log4j lines (`[12:16:12] [Render thread/INFO]: ...`).
LEVEL_RE = re.compile(r"\[[^\]]+/([A-Z]+)\]")
# Offline stand, random offline-mode UUIDs: the server async-fetches the fake
# profile from the Mojang session servers and the sandbox answers with an
# HTML error page, whose text lands in the log. The
# `MinecraftClientException: Failed to read value` continuation is allowed
# only on the line directly following the vanilla
# `Couldn't look up profile properties for` WARN (round4-rc
# server-latest.log lines 1351-1352); any other MinecraftClientException
# (different message, or the same message without that WARN above it) still
# fails the scan.
PROFILE_LOOKUP_WARN_RE = re.compile(r"Couldn't look up profile properties for")
OFFLINE_PROFILE_FAILURE_RE = re.compile(
    r"MinecraftClientException: Failed to read value"
)
# Levels that never fail the scan: an INFO line only matches ERROR_RE via a
# quoted substring (the llvmpipe "type=ERROR" shader notice), never a real
# error. WARN/ERROR and lines without a level prefix still fail.
# Must stay equal to scripts/release.py RC_RECORD_FORMAT: the release gate
# reads the file --record writes (a test pins the equality).
RC_RECORD_FORMAT = "blockfield-rc-record/1"
# Fabric logs "Error loading class: <intermediary name> (ClassNotFoundException)"
# for names the refmap does not cover at every clean startup; benign.
# The entries below are the same kind of optional-integration probe seen
# on clean mains (night-config file IO, Nucleoid translations SPI, JER biome
# helper, MouseTweaks entry point): each names the exact class, never a
# prefix, so a real class-load failure for any other class still fails
# the scan.
LOG_SCAN_ALLOW = (
    "Error loading class: net/minecraft/class_",
    "Error loading class: com/electronwill/nightconfig/core/io/IoUtils",
    "Error loading class: xyz/nucleoid/server/translations/impl/language/SystemDelegatedLanguage",
    "Error loading class: jeresources/api/util/BiomeHelper",
    # Same startup probe batch as the three above on a clean dev-environment
    # stand (blockfield-mod docs/review/wire-evidence/measurement.txt:289-292);
    # neither the client nor the server pack ships mousetweaks, the game runs
    # on without it, and the entry names the exact class.
    "Error loading class: yalter/mousetweaks/Main",
    # Raknetify-fabric's optional Quilt-compat mixin
    # (compat.qsl.MixinServerLoginNetworkAddon, @Pseudo target
    # org.quiltmc.qsl.networking.impl.server.ServerLoginNetworkAddon) probes the
    # Quilt class at startup; Quilt is not installed, so Mixin logs one WARN and
    # skips it. Third-party jar, no fork: exact line only (rc-final7).
    "Error loading class: org/quiltmc/qsl/networking/impl/server/ServerLoginNetworkAddon",
    # Rechiseled's optional JEI-compat mixins (mixin.jei.RecipeLayoutInputHandlerMixin
    # + RecipeLayoutUserInputHandlerMixin, both @Pseudo) probe the JEI classes at
    # startup; JEI is not installed, so Mixin logs one WARN each and skips them.
    # Same optional-probe family as the entries above; exact lines only (rc-final7 c2).
    "Error loading class: mezz/jei/library/gui/recipes/RecipeLayoutInputHandler",
    "Error loading class: mezz/jei/gui/recipes/RecipeLayoutWithButtons$RecipeLayoutUserInputHandler",
    # Vanilla EntityType.Builder.build(String) performs a DataFixer lookup and logs
    # "No data fixer registered" for every modded id while discarding the result, so
    # every saveable modded entity logs one ERROR at startup. Fixed at the source where
    # the build(String) call lives in a fork we own (SBW#40, lr-tactical#13, datafixer-2
    # for target_minecart/ride_entity/sulfur_cube). The entries below name the exact
    # entity id whose build(String) call lives outside our forks, so a warning for any
    # other id still fails the scan:
    # - copper_golem: third-party Copper Age Backport 0.1.5, no fork; upstream has no
    #   fix (latest 0.1.5.1-pre changelog 2026-10-03 does not mention data fixers;
    #   modrinth a1llHwl4, version 3GZU7zrg).
    "No data fixer registered for copper_golem",
    # - creaking, happy_ghast, pale_oak_boat, pale_oak_chest_boat: VanillaBackport
    #   1.1.7.10 registers them through the third-party Platform 1.3.3 helper
    #   (EntityRegistry.lambda$entity$0 -> Builder.build(name)); even upstream
    #   VanillaBackport 1.2.2 with Platform 1.4.2 keeps that call, so only a Platform
    #   fork could fix them (modrinth 6xwxDTgf / i6fiqm5y).
    "No data fixer registered for creaking",
    "No data fixer registered for happy_ghast",
    "No data fixer registered for pale_oak_boat",
    "No data fixer registered for pale_oak_chest_boat",
    # fabric-server-launch.jar cold-install mirror fallback (per-mirror notice from FabricService.invokeWithFallbacks, start succeeds on the next mirror): exact service plus manifest only.
    "service FabricService{meta='https://meta.fabricmc.net/', maven='https://maven.fabricmc.net/'} failed: java.io.IOException: Request to https://maven.fabricmc.net/net/minecraft/experimental_versions.json",
)


def pin_version(text):
    """Owned-mod version from a packwiz metafile (filename carries the version)."""
    name = tomllib.loads(text)["filename"].split("+")[0]
    match = re.search(r"-(\d+\.\d+\.\d+)$", name)
    if not match:
        raise ValueError(f"no mod version in filename {name!r}")
    return match.group(1)


def server_mod_source(server_root):
    """(mod_version, upstreamCommit) from the server checkout's mod-server sources.

    The download pin (server/mods/blockfield-server.pw.toml) is gone: the Game
    image jar is built from mod-server (:server:remapJar), so gradle.properties
    mod_version and upstream.properties upstreamCommit are the source of truth.
    """
    server_root = Path(server_root)
    try:
        props = (server_root / "mod-server" / "gradle.properties").read_text(
            encoding="utf-8"
        )
    except OSError as error:
        raise ValueError(f"server mod source unreadable: {error}") from error
    match = re.search(r"^mod_version=(.+)$", props, re.MULTILINE)
    if not match:
        raise ValueError("no mod_version in server mod-server/gradle.properties")
    try:
        upstream = (server_root / "mod-server" / "upstream.properties").read_text(
            encoding="utf-8"
        )
    except OSError:
        upstream_commit = None
    else:
        found = re.search(r"^upstreamCommit=(.+)$", upstream, re.MULTILINE)
        upstream_commit = found.group(1).strip() if found else None
    return match.group(1).strip(), upstream_commit


def loaded_version(reply):
    """Version from a `bf version` reply (`<ver> commit <sha>`, pack prefix optional)."""
    match = VERSION_RE.search(reply or "")
    if not match:
        raise ValueError(f"no loaded mod version in {reply!r:.120}")
    return match.group(1)


def log_mod_version(text):
    """Version from a server/client log (Fabric mod list or readiness line)."""
    match = VERSION_RE.search(text or "") or FABRIC_MOD_RE.search(text or "")
    if not match:
        raise ValueError("no loaded mod version in logs")
    return match.group(1)


CALM_LEVELS = ("INFO", "DEBUG", "TRACE")

# Software-GL stand renderer (llvmpipe/softpipe, no GPU on the stand): it
# reports reduced GL/GLSL caps, and one capability notice lands at ERROR.
# Narrow and renderer-gated: the log must name the software renderer AND
# the line itself must be an explicit-ERROR OpenGL/GLSL capability notice
# (unsupported cap / software fallback). Anything else mentioning llvmpipe
# (a crash, a different component) still fails the scan.
SOFTPIPE_RENDERER_RE = re.compile(r"(?i)\b(llvmpipe|softpipe|swrast)\b")
SOFTPIPE_CAP_RE = re.compile(
    r"(?i)\b(GLSL|OpenGL)\b.*\b(not supported|unsupported|unavailable|"
    r"capabilit|fallback|software rendering)\b"
    r"|\b(not supported|unsupported|unavailable|"
    r"capabilit|fallback|software rendering)\b.*\b(GLSL|OpenGL)\b"
)
# A genuine context-creation (or other fatal) failure must never hide behind
# the capability exemption, even when it names llvmpipe and an unsupported
# feature: "Failed to create OpenGL context: required version unsupported".
SOFTPIPE_CONTEXT_RE = re.compile(
    r"(?i)context.{0,24}(creat|fail)|creat.{0,24}context|"
    r"failed to (create|initiali[sz]e)"
)


def scan_errors(text, allow=()):
    """Log lines that look like errors, minus allowlisted benign substrings.

    Lines carrying a calm log level ([thread/INFO] etc.) never fail, even when
    they quote the word "error"; lines without a level prefix still fail.
    The software-GL capability notice (below) never fails, but only when the
    same log proves the stand renders on llvmpipe/softpipe.
    """
    text = text or ""
    softpipe_stand = bool(SOFTPIPE_RENDERER_RE.search(text))
    out = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not ERROR_RE.search(line):
            continue
        level = LEVEL_RE.search(line)
        if level and level.group(1) in CALM_LEVELS:
            continue
        if any(needle in line for needle in allow):
            continue
        if (
            softpipe_stand
            and level
            and level.group(1) == "ERROR"
            and SOFTPIPE_CAP_RE.search(line)
            and not SOFTPIPE_CONTEXT_RE.search(line)
        ):
            continue
        if OFFLINE_PROFILE_FAILURE_RE.search(line) and (
            PROFILE_LOOKUP_WARN_RE.search(lines[index - 1]) if index > 0 else False
        ):
            continue
        out.append(line.strip())
    return out


def version_gate(rcon, server_log, client_log, client_pin, server_root):
    """First RC step: loaded server/client mod must equal the workspace pins.

    client_pin is the client metafile path; server_root is the server checkout
    dir, whose mod-server sources carry the expected mod_version (the download
    pin is gone, never a download).
    """
    out = []
    try:
        server_loaded = loaded_version(rcon.cmd("bf version"))
    except (OSError, RuntimeError, ValueError) as error:
        return [("versions served match pins", False, str(error)[:160])], True
    try:
        client_loaded = log_mod_version(client_log)
    except ValueError as error:
        out.append(("versions client log parsed", False, str(error)))
        return out, True
    try:
        want_client = pin_version(Path(client_pin).read_text(encoding="utf-8"))
        want_server, _upstream = server_mod_source(server_root)
    except (OSError, KeyError, ValueError) as error:
        out.append(("versions pins readable", False, str(error)[:160]))
        return out, True
    ok = server_loaded == want_server and client_loaded == want_client
    out.append(
        (
            "versions loaded match pins",
            ok,
            f"server {server_loaded}/{want_server} client {client_loaded}/{want_client}",
        )
    )
    if not ok:
        return out, True
    if server_loaded not in (server_log or ""):
        out.append(
            ("versions server log shows mod", True, "log line absent, rcon rules")
        )
    else:
        out.append(("versions server log shows mod", True, server_loaded))
    return out, False


def leave_to_lobby(bot, timeout=30):
    """Leave any room/range dimension so the next play starts from the lobby.

    The hub silently drops play/join while the bot is parked in the firing
    range, so without this the room-open wait times out with no log line.
    Waits for the join first: renderer-ready is not in-game, and the first
    in-game command throws `Cannot send packets when not in game!` while the
    testbot state still has no dimension. Returns True when the bot reports
    a lobby dimension in time; raises readiness.NotReady when the client
    never joins (an invalid setup, never a product FAIL). One shared
    deadline bounds both waits.
    """
    deadline = time.monotonic() + timeout
    if not wait_in_game(bot, timeout=timeout):
        raise readiness.NotReady(
            "client never joined",
            "testbot state never reported a dimension: in-game commands"
            " would throw `Cannot send packets when not in game!`",
        )
    bot("closescreen")
    bot("bfaction leave")
    while time.monotonic() < deadline:
        if is_lobby_dimension(dimension(bot("state"))):
            return True
        time.sleep(1)
    return is_lobby_dimension(dimension(bot("state")))


def open_room(bot, get_log, mode, map_id, timeout=60):
    """Open a casual room via the hub menu; returns (room number, log snapshot)."""
    mark = len(get_log().splitlines())
    leave_to_lobby(bot)
    bot(f"bfaction play:{mode}:{map_id}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = ROOM_RE.findall("\n".join(get_log().splitlines()[mark:]))
        if match:
            return match[-1]
        time.sleep(1)
    raise TimeoutError(f"room {mode} {map_id} did not open in {timeout}s")


def wait_dim(bot, wanted, timeout, any_of=()):
    """Block until the bot dimension matches; returns the final dimension."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dim = dimension(bot("state"))
        if dim == wanted or dim in any_of:
            return dim
        time.sleep(1)
    return dimension(bot("state"))


def deploy(bot, get_log, mode, map_id, bot2=None, timeout=60):
    """Open a room, pick assault, wait for the arena; returns (room, arena)."""
    if bot2 is not None:
        leave_to_lobby(bot2)
    room = open_room(bot, get_log, mode, map_id, timeout)
    if bot2 is not None:
        bot2("bfaction join:" + room)
        time.sleep(3)
    bot("bfaction class:assault:0")
    if bot2 is not None:
        bot2("bfaction class:assault:0")
    arena = dimension(bot("state"))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not arena.startswith("blockfield:"):
        time.sleep(1)
        arena = dimension(bot("state"))
    if not arena.startswith("blockfield:"):
        raise TimeoutError(f"never deployed: {arena}")
    return room, arena


def wait_lobby(bot, bot2, timeout=30):
    """Bounded wait until both bots report a lobby dimension after leave."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_lobby_dimension(dimension(bot("state"))) and is_lobby_dimension(
            dimension(bot2("state"))
        ):
            return True
        time.sleep(1)
    return False


def has_gun(rcon, bot):
    """True when the kit gun is held or anywhere in the bot inventory.

    A solo FFA can start before the class pick lands; a pick while alive is
    then rejected by design (BlockFieldServer.selectClass), so the bot stays
    kitless with only the command tablet. Callers use this to tell that
    harness-ordering case apart from a genuine shot failure.
    """
    state = bot("state")
    if state.get("held") == "tacz:modern_kinetic_gun":
        return True
    match = re.search(
        r"(\d+)$",
        rcon.cmd(
            f"execute if items entity {state['name']}"
            " container.* tacz:modern_kinetic_gun"
        )
        or "",
    )
    return bool(match and int(match.group(1)))


def long_range(
    rcon, bot, bot2, arena, distance=60, timeout=120, bounds=None, get_log=None
):
    """Gun kill at range: search firing lanes around the victim's deploy spot.

    The old fixed +X offset routinely parked the shooter behind hamburg
    geometry, and then every bullet died on blocks with no damage while the
    gun itself was fine. Reuse duo's lane search, which abandons walled lanes
    (#211: kitgun-zero trace proved the lane, not the gun, was broken).
    bounds clamps the lanes inside the map rectangle (a corner deploy spot
    plus a 60 m offset otherwise parks the shooter outside the battle
    boundary, which kills outsiders and wipes the kit).
    Eight lanes: hamburg blocks every orthogonal lane at 20-60 m while the
    same gun kills at the same distance in the open dm_arena (rc-triage),
    so the four diagonals join the search. Eight lanes times the default
    four shots does not fit the kit mag (30), hence three shots per lane.
    With bounds the victim is parked in a corner first: a 60 m lane needs
    60 m of map, so from a central spot every lane would clamp below range
    and be skipped, failing a working gun without firing.
    """
    # Deferred: duo imports rc.deploy at module load, so a top-level import
    # would be circular.
    from . import duo as _duo

    # The tablet leg leaves slot 8 (command tablet) in hand; face_off reads
    # the mag before its first lane switch, so select the gun first like the
    # duo flow (kit_or_raise) does.
    bot("slot 0")
    time.sleep(1)
    if not has_gun(rcon, bot):
        # The kit can go missing between the grant wait and the first lane
        # (a round end wipes it): one class re-pick, then a residual FAIL is
        # a real kit defect, not this ordering.
        bot("bfaction class:assault:0")
        _duo.kit_or_raise(bot)
    diag = distance / 2**0.5
    lanes = (
        (distance, 0, -90),
        (-distance, 0, 90),
        (0, distance, 180),
        (0, -distance, 0),
        (diag, diag, -135),
        (-diag, diag, 135),
        (-diag, -diag, 45),
        (diag, -diag, -45),
    )
    if bounds is not None:
        # A 60 m lane needs 60 m of map: from a central victim spot every
        # lane clamps below range and face_off skips it, so a working gun
        # fails without firing. Park the victim in a corner first; the
        # inward lanes then fit the full distance.
        vstate = bot2("state")
        corner = (bounds[0] + 2, vstate["pos"][1], bounds[2] + 2)
        rcon.cmd(
            f"execute in {arena} run tp {vstate['name']}"
            f" {corner[0]} {corner[1]} {corner[2]} 0 0"
        )
        # face_off anchors every lane on the victim position it reads first
        # (and re-teleports the victim there per lane): starting before the
        # corner teleport lands re-centers the victim and skips every 60 m
        # lane. Wait until bot2 reports the corner (~1 block tolerance);
        # on timeout fail with that reason instead of searching blind.
        deadline = time.monotonic() + 15
        pos = vstate["pos"]
        while time.monotonic() < deadline:
            try:
                pos = bot2("state")["pos"]
            except (OSError, RuntimeError, KeyError):
                time.sleep(1)
                continue
            if (
                abs(pos[0] - corner[0]) <= 1.0
                and abs(pos[1] - corner[1]) <= 1.0
                and abs(pos[2] - corner[2]) <= 1.0
            ):
                break
            time.sleep(1)
        else:
            return False, f"victim never reached corner {corner}: last {pos}"
    try:
        ok, detail = _duo.face_off(
            rcon,
            bot,
            bot2,
            distance=distance,
            shots_per_lane=3,
            bounds=bounds,
            lanes=lanes,
            get_log=get_log,
            arena=arena,
            protection_timeout=timeout,
            ads=True,
        )
    finally:
        # Later legs (hitreg flick) rely on hip fire: release the held aim key.
        try:
            bot("tacz aim false")
        except (OSError, RuntimeError):
            pass
    return (True, f"{detail} in {arena}") if ok else (False, f"{detail} in {arena}")


def tdm_respawn(rcon, bot, bot2, arena, get_log=None, timeout=120):
    """Spawn at BASE, wait out spawn protection, death, then BASE redeploys.

    A TDM room does not auto-deploy like deathmatch: killing an unspawned
    player is a no-op, so spawn first and prove both bots are in the arena.
    A fresh spawn is shielded (a knife probe still reports invulnerable), so
    the kill waits for vulnerability first like dm.py does; killing into the
    shield is a silent no-op. Dead members wait INSIDE the match dimension by
    design (the respawn platform is in-dim; dm.py proves the same): death is
    the server kill log (instant respawn hides the client flicker), the death
    screen or zero health in the arena — the legacy shared-respawn dim is
    accepted too but never required.

    A TDM room never auto-deploys, so fail here like the deathmatch legs do
    instead of probing a never-deployed victim for the whole timeout: whoever
    skipped the class pick stands kitless, and every knife probe then reads
    invulnerable forever (never damageable, never a respawn).
    The TDM kit lands a minute after the room opens on a slow stand
    (rc-triage: the single spawn plus 30 s kit_or_raise below never equipped
    either bot, while repeated spawns equipped both near the minute mark with
    no re-pick and no kill). On a Timeout, keep offering spawn:BASE like
    dm.wait_deployed until both bots hold the kit gun instead of failing on
    the first quiet stretch.
    """
    from . import duo as _duo

    def equipped():
        try:
            _duo.kit_or_raise(bot, timeout=5)
            _duo.kit_or_raise(bot2, timeout=5)
        except TimeoutError:
            return False
        return True

    bot("bfaction spawn:BASE")
    bot2("bfaction spawn:BASE")
    deployed = wait_dim(bot, arena, timeout), wait_dim(bot2, arena, timeout)
    if deployed != (arena, arena):
        return False, f"never deployed: {deployed[0]}/{deployed[1]}"
    try:
        _duo.kit_or_raise(bot)
        _duo.kit_or_raise(bot2)
    except TimeoutError:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not equipped():
            for b in (bot, bot2):
                try:
                    state = b("state")
                except (OSError, RuntimeError):
                    continue
                # Re-offer spawn to kitless bots even when they already stand
                # in the arena: a spawn refused while the room was still
                # forming leaves them in-arena with tablets otherwise.
                if dimension(state) != arena or state.get("held") != _duo.GUN:
                    try:
                        b("bfaction spawn:BASE")
                    except (OSError, RuntimeError):
                        pass
            time.sleep(1)
        if not equipped():
            try:
                states = (bot("state"), bot2("state"))
            except (OSError, RuntimeError):
                states = ()
            return False, f"never equipped: {[s.get('held') for s in states]}"
    shooter_name = bot("state")["name"]
    victim_name = bot2("state")["name"]
    probe_deadline = time.monotonic() + timeout
    while time.monotonic() < probe_deadline:
        reply = (
            rcon.cmd(
                f"execute in {arena} run damage {victim_name}"
                f" 1 blockfield:knife by {shooter_name}"
            )
            or ""
        )
        if "invulnerable" not in reply:
            break
        time.sleep(1)
    else:
        return False, f"victim never vulnerable in {arena}"
    mark = len((get_log() if get_log else "").splitlines())
    bot2("bfaction kill")
    kill_log = re.compile(re.escape(victim_name) + r" убит")
    deadline = time.monotonic() + timeout
    dead = False
    last = arena
    while time.monotonic() < deadline:
        if kill_log.search(
            "\n".join((get_log() if get_log else "").splitlines()[mark:])
        ):
            dead = True
            break
        try:
            state = bot2("state")
        except (OSError, RuntimeError):
            time.sleep(1)
            continue
        last = dimension(state)
        if last == RESPAWN or (
            last == arena
            and (
                (state.get("screen") or "") == "DeathScreen"
                or (state.get("health") or 0) <= 0
            )
        ):
            dead = True
            break
        time.sleep(1)
    if not dead:
        return False, f"victim never died in {arena}: {last}"
    time.sleep(2)
    bot2("bfaction spawn:BASE")
    dim = wait_dim(bot2, arena, timeout)
    return dim == arena, f"redeployed to {dim}"


def run(
    args, rcon_factory=connect_rcon, bot_factory=Bot, get_log=None, server_dir=None
):
    from devstack import HOME, LOG, ROOT, SERVER

    # Deferred: duo imports rc.deploy at module load, so a top-level import
    # would be circular (dm reaches rc through duo).
    from . import duo as _duo
    from .dm import arena_bounds

    logs = get_log or (lambda: LOG.read_text(encoding="utf-8", errors="replace"))
    game_dir = Path(args.game_dir or HOME / "play")
    client_log_path = game_dir / "logs" / "latest.log"
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    rows = []
    try:
        try:
            bot2 = bot_factory(args.bot_port2)
            bot2("state")
        except (OSError, RuntimeError):
            refusal = (
                f"second client bot port {args.bot_port2} not answering:"
                " start it with `just play --headless --no-gpu --client 2"
                " --testbot <JAR>` (after `just setup --testbot <JAR>`),"
                " then rerun this scenario"
            )
            # A refused run must not leave a stale --record behind for the
            # gate to reuse: it writes its own FAIL record first. The exit is
            # INCONCLUSIVE (2), never a product FAIL: a missing second client
            # is an invalid setup.
            maybe_write_record(args, [("second client bot answering", False, refusal)])
            print(f"INCONCLUSIVE rc: {refusal}", flush=True)
            raise SystemExit(readiness.INCONCLUSIVE)
        gate, stop = version_gate(
            rcon,
            logs(),
            client_log_path.read_text(encoding="utf-8", errors="replace")
            if client_log_path.is_file()
            else "",
            ROOT / "client" / "mods" / "blockfield-client.pw.toml",
            ROOT / "server",
        )
        rows += gate
        if stop:
            maybe_write_record(args, rows)
            return finish(rows)
        try:
            arena = None
            # Two clients hold the room in PREPARING, so the class picks
            # land before the start and the kit is granted on deploy at
            # GAME start (preparation ~15 s after the room opens), not when
            # the arena dimension appears: wait for the kit like duo does
            # instead of asserting 2 s later (old kitless FAIL).
            _, arena = deploy(bot, logs, "deathmatch", args.map, bot2)
            try:
                _duo.kit_or_raise(bot)
            except TimeoutError:
                # A pick sent while the room was still forming may have
                # landed before PREPARING accepted it; one retry, then the
                # FAIL below is a real kit-grant defect, not this ordering.
                bot("bfaction class:assault:0")
                _duo.kit_or_raise(bot)
            bot("slot 0")
            time.sleep(2)
            check_gameplay.shot(rcon, bot)
            rows.append(("first-shot after deploy", True, "ammo -1, SUCCESS"))
        except AssertionError as error:
            rows.append(("first-shot after deploy", False, str(error)[:160]))
        except (RuntimeError, OSError, TimeoutError) as error:
            rows.append(("first-shot after deploy", False, str(error)[:160]))
        try:
            check_gameplay.lobby(rcon, bot)
            bot("closescreen")
            rows.append(
                ("tablet reopen in lobby", True, "tablet in hand, screens closed")
            )
        except (AssertionError, RuntimeError, OSError) as error:
            rows.append(("tablet reopen in lobby", False, str(error)[:160]))
        try:
            # Own open room: hamburg blocks every lane at 20-60 m (rc-triage:
            # 8 walled lanes in hamburg_2 while the same gun kills at the same
            # distance in the open dm_arena), so probe range in dm_arena like
            # the hitreg smoke opens its own room. Fresh room every time: the
            # tablet leg leaves both bots in the lobby. Then wait for the
            # trailer: deploy only proves bot1 arrived, and sampling bot2's
            # lobby position would waste all eight lanes.
            _, arena = deploy(bot, logs, "deathmatch", "dm_arena", bot2)
            _duo.wait_arena(bot2, arena)
            # The kit lands at GAME start, not when the arena appears:
            # searching lanes with an empty slot spends all eight.
            _duo.kit_or_raise(bot)
            # Corner deploy spots plus a 60 m offset park the shooter
            # outside the battle boundary (genericKill, kit wiped): clamp
            # the lanes inside the map rect like the HUD duel does.
            ok, detail = long_range(
                rcon,
                bot,
                bot2,
                arena,
                bounds=arena_bounds(server_dir or SERVER, "dm_arena"),
                get_log=logs,
            )
            rows.append(("long-range kill dm_arena", ok, detail))
        except (TimeoutError, RuntimeError, OSError, AssertionError) as error:
            rows.append(("long-range kill dm_arena", False, str(error)[:160]))
        try:
            # dm_arena is deathmatch-only by design (it has no tdm pool):
            # the tdm leg needs a TDM-capable map, i.e. the battery map.
            room, arena = deploy(bot, logs, "tdm", args.map, bot2)
            _duo.wait_arena(bot2, arena)
            ok, detail = tdm_respawn(rcon, bot, bot2, arena, logs)
            rows.append(("tdm death/respawn at BASE", ok, detail))
        except (TimeoutError, RuntimeError, OSError, AssertionError) as error:
            rows.append(("tdm death/respawn at BASE", False, str(error)[:160]))
        try:
            # Own room: the smoke must not depend on the tdm leg's room
            # (its cascade hid the smoke whenever tdm failed). Leave both
            # bots to the lobby, open a fresh deathmatch, and set up the
            # hitreg lane in it like pose_overlay sets up its own (#174).
            bot("bfaction leave")
            bot2("bfaction leave")
            if not wait_lobby(bot, bot2, timeout=30):
                raise TimeoutError("bots never returned to the lobby")
            room, arena = deploy(bot, logs, "deathmatch", args.map, bot2)
            _duo.wait_arena(bot2, arena)
            # Liveness proxy, not the smoke gun: the kit grant at GAME
            # start would clobber hotbar.0, so wait for it first and let
            # setup re-arm the zero-spread gun on top afterwards. A solo
            # room starts instantly and the pick is rejected while alive,
            # so hold PREPARING with the second client and retry once.
            try:
                _duo.kit_or_raise(bot)
            except TimeoutError:
                bot("bfaction class:assault:0")
                _duo.kit_or_raise(bot)
            hitreg.setup(rcon, dimension(bot("state")), room, bot("state")["name"])
            # The lobby leg leaves slot 8 (tablet) selected while setup
            # arms hotbar.0: select the zero-spread gun first, or run_shots
            # reads the tablet's components and rejects the leg.
            bot("slot 0")
            time.sleep(1)
            # "still": this is the still-target smoke (the evidence string
            # below says so); True was never a valid motion and crashed the
            # leg on `assert motion in MOTIONS` instead of reporting FAIL.
            hitreg.run_shots(rcon, bot, args.hitreg_shots, "still", "track")
            rows.append(("hitreg smoke", True, f"{args.hitreg_shots} still shots"))
        except (
            SystemExit,
            RuntimeError,
            OSError,
            TimeoutError,
            AssertionError,
        ) as error:
            rows.append(("hitreg smoke", False, str(error)[:160]))
        errors = scan_errors(logs(), LOG_SCAN_ALLOW) + scan_errors(
            client_log_path.read_text(encoding="utf-8", errors="replace")
            if client_log_path.is_file()
            else "",
            LOG_SCAN_ALLOW,
        )
        rows.append(
            # Every unexplained line verbatim: the next RC must show the
            # exact text of a new failure instead of a truncated prefix.
            ("log scan clean", not errors, "; ".join(errors) or "no errors")
        )
        reply = rcon.cmd("save-all") or ""
        rows.append(("save-all", "sav" in reply.lower(), reply[:120]))
        maybe_write_record(args, rows)
        return finish(rows)
    except readiness.NotReady as error:
        # A lane fixture that never became valid (or protection still active)
        # is an inconclusive setup, never a product row: record an explicit
        # non-passing readiness result so the release gate cannot reuse the
        # preceding PASS rows as a green battery.
        product_failed = any(ok is False for _, ok, _ in rows)
        rows.append(("readiness gate", False, f"INCONCLUSIVE: {error}"))
        maybe_write_record(args, rows)
        print(f"INCONCLUSIVE rc: {error}", flush=True)
        # A product FAIL already recorded stays exit 1; the abort only hides
        # the checks that did not run.
        return 1 if product_failed else readiness.INCONCLUSIVE
    finally:
        bot.close()


def build_record(tags, rows):
    """Machine-readable RC record for the release gate (scripts/release.py)."""
    return {
        "format": RC_RECORD_FORMAT,
        "battery": "just scenario rc",
        "releases": tags,
        "checks": [
            {
                "name": name,
                "result": "PASS" if ok is True else "SKIP" if ok is None else "FAIL",
                "evidence": evidence,
            }
            for name, ok, evidence in rows
        ],
    }


def maybe_write_record(args, rows):
    tags = {
        side: tag
        for side, tag in (
            ("client", args.tag_client),
            ("server", args.tag_server),
        )
        if tag
    }
    if args.record and not tags:
        raise SystemExit("--record needs --tag-client and/or --tag-server")
    if args.record:
        Path(args.record).write_text(
            json.dumps(build_record(tags, rows), indent=2) + "\n", encoding="utf-8"
        )


def finish(rows):
    """One PASS/FAIL/SKIP line per check with evidence, then the RESULT tally."""
    out = []
    for name, ok, evidence in rows:
        if ok is None:
            print(f"SKIP {name} :: {evidence}", flush=True)
            continue
        if ok and evidence:
            print(f"PASS {name} :: {evidence}", flush=True)
            out.append(check(name, True))
        else:
            out.append(check(name, ok, evidence))
    tally_out = tally(out)
    return tally_out and 1 or 0


def register(sub):
    parser = sub.add_parser("rc", help=SCENARIO["help"])
    parser.add_argument("--map", default="hamburg")
    parser.add_argument("--hitreg-shots", type=int, default=10)
    parser.add_argument("--bot-port2", type=int, default=47778)
    parser.add_argument("--game-dir", default=None)
    parser.add_argument(
        "--record",
        default=None,
        metavar="PATH",
        help="write the machine-readable RC record for the release gate",
    )
    parser.add_argument(
        "--tag-client",
        default=None,
        metavar="VTAG",
        help="client release tag the record covers (with --record)",
    )
    parser.add_argument(
        "--tag-server",
        default=None,
        metavar="VTAG",
        help="server release tag the record covers (with --record)",
    )
    rcon_args(parser)
    parser.set_defaults(func=run)
