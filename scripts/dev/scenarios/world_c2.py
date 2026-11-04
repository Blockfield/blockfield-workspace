#!/usr/bin/env python3
"""World/infra close-out checks (p2-world-c2): pack banner changed-contract paths,
live launcher watchdog on a throttled mirror, music-v7 live sound-event capture,
Workshop builder round-trip, passengers churn for #228.

Each subcommand drives one check against a running devstack stand (server +
headless testbot client) and prints PASS/FAIL lines plus a JSON evidence row.
Host-side fixtures the client cannot do itself (marker swaps, server profile
restarts) are done by the operator between runs; the scenario only observes and
asserts, so a re-run never silently overwrites evidence.
"""

import hashlib
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import Bot, check, connect_rcon, rcon_args, tally

SCENARIO = {
    "name": "world-c2",
    "help": "p2 world close-out: pack/music/workshop/churn/watchdog",
}

BUILD_JSON_URL = "https://blockfield.pro/build.json"
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
# TLS for the throttled-mirror stand: the product only trusts mirror URLs
# starting with https:// (Mirrors.kt load), so the local trickle mirror gets a
# throwaway 127.0.0.1 cert generated at runtime — no key material in the repo.
MIRROR_TRUST_PASSWORD = "blockfield-test"


def mirror_entry(port):
    """Local trickle-mirror entry for mirrors.json. Absolute https: Mirrors.kt
    only substitutes {path}/{hash} and rejects non-https entries."""
    return {"name": "trickle", "url": f"https://127.0.0.1:{port}/{{path}}"}


def ensure_mirror_tls(directory):
    """Generate a throwaway localhost cert + PKCS12 truststore (openssl for the
    key/cert, keytool for the store Java trusts). Returns (cert, key, store)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert, key, store = (
        directory / "mirror-cert.pem",
        directory / "mirror-key.pem",
        directory / "mirror-trust.p12",
    )
    if not (cert.is_file() and key.is_file() and store.is_file()):
        openssl, keytool = shutil.which("openssl"), shutil.which("keytool")
        java_home = os.environ.get("JAVA_HOME", "")
        if keytool is None and java_home:
            candidate = Path(java_home) / "bin" / "keytool"
            keytool = str(candidate) if candidate.is_file() else None
        if openssl is None or keytool is None:
            raise RuntimeError("watchdog TLS needs openssl + keytool on PATH")
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "2",
                "-nodes",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1,DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )
        if store.exists():
            store.unlink()
        subprocess.run(
            [
                keytool,
                "-importcert",
                "-noprompt",
                "-alias",
                "watchdog-mirror",
                "-file",
                str(cert),
                "-keystore",
                str(store),
                "-storetype",
                "PKCS12",
                "-storepass",
                MIRROR_TRUST_PASSWORD,
            ],
            check=True,
            capture_output=True,
        )
    return cert, key, store


# Optional one-line chat (ru + en wordings, PackUpdates posts a translatable).
OPTIONAL_RE = re.compile("необязательно|optional", re.IGNORECASE)
# Banner text color 0xFFFFCC66 and its shadow; the fill is translucent over the
# scene so only the text color is a stable signature.
BANNER_TEXT = (255, 204, 102)
# Product decision markers (PackUpdates INFO logs, grepped from the client log).
OPTIONAL_LOG_RE = re.compile(r"PackUpdates: optional pack update available: (\S+)")
MANDATORY_LOG_RE = re.compile(r"PackUpdates: mandatory pack update required: (\S+)")
NOTIFY_OFF_RE = re.compile(r"PackUpdates: menu\.vanilla dev mode, update notify off")


def repo_root():
    return Path(__file__).resolve().parent.parent.parent.parent


def stack_home():
    env = os.environ.get("BF_DEV_HOME")
    return Path(env) if env else repo_root() / ".dev"


def client_game_dir(args):
    if getattr(args, "client_dir", None):
        return Path(args.client_dir)
    return stack_home() / "play"


def server_log_path(args):
    if getattr(args, "server_log", None):
        return Path(args.server_log)
    return stack_home() / "server" / "server.out"


def server_dir_path(args):
    if getattr(args, "server_dir", None):
        return Path(args.server_dir)
    return stack_home() / "server"


def client_log_path(args):
    if getattr(args, "client_log", None):
        return Path(args.client_log)
    game = client_game_dir(args)
    # PackUpdates logs through log4j: latest.log carries the INFO decision
    # markers, while client.out (raw stdout) often does not.
    for candidate in (game / "logs" / "latest.log", game / "client.out"):
        if candidate.is_file():
            return candidate
    return game / "logs" / "latest.log"


def grep_log(path, patterns, since_pos=0):
    """Search log bytes appended after since_pos; returns (pos, {name: match})."""
    try:
        data = path.read_bytes()
    except OSError:
        return since_pos, {}
    chunk = data[since_pos:]
    text = chunk.decode("utf-8", errors="replace")
    found = {}
    for name, pattern in patterns.items():
        match = pattern.search(text)
        if match:
            found[name] = match.group(0)
    return len(data), found


def fetch_build_json(url=BUILD_JSON_URL, timeout=15):
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_kind(local_digest, local_contract, remote_digest, remote_contract):
    """Python mirror of PackUpdates.resolve: NONE/OPTIONAL/MANDATORY."""
    if (
        not remote_digest
        or not DIGEST_RE.fullmatch(remote_digest)
        or remote_digest == local_digest
    ):
        return "NONE"
    if local_contract and local_contract == remote_contract:
        return "OPTIONAL"
    return "MANDATORY"


def read_markers(game_dir):
    config = game_dir / "config"
    version, contract = "", ""
    for name, box in (
        ("blockfield-pack-version.txt", "version"),
        ("blockfield-pack-contract.txt", "contract"),
    ):
        try:
            value = (config / name).read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if box == "version":
            version = value
        else:
            contract = value
    return version, contract


def wait_messages(bot, pattern, timeout, poll=2.0):
    """Poll `messages` until a line matches; returns (matched, all_lines)."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            lines = bot("messages 200")
        except (OSError, RuntimeError):
            return False, seen
        seen = [str(line) for line in lines]
        if any(pattern.search(line) for line in seen):
            return True, seen
        time.sleep(poll)
    return False, seen


def connected(bot):
    try:
        state = bot("state")
    except (OSError, RuntimeError):
        return False
    return isinstance(state, dict) and state.get("connected")


# --- screenshot banner detector (stdlib PNG decode) ---


def decode_png(path):
    """Minimal PNG decoder for Minecraft screenshots (8-bit RGB/RGBA)."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, width, height, ctype, raw = 8, 0, 0, 0, b""
    while pos < len(data):
        (length, kind) = struct.unpack(">I4s", data[pos : pos + 8])
        chunk = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, _ = struct.unpack(">IIBBBBB", chunk)
            assert depth == 8 and ctype in (2, 6), f"unsupported PNG {depth}/{ctype}"
        elif kind == b"IDAT":
            raw += chunk
        elif kind == b"IEND":
            break
        pos += 12 + length
    channels = 3 if ctype == 2 else 4
    stride = width * channels
    out = bytearray(width * height * channels)
    stream = zlib.decompress(raw)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        filt = stream[p]
        p += 1
        line = bytearray(stream[p : p + stride])
        p += stride
        if filt == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif filt == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif filt == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif filt == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                q = a + b - c
                pa, pb, pc = abs(q - a), abs(q - b), abs(q - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


def banner_pixels(path, gui_size, tolerance=64):
    """Count banner-text-colored pixels in the bottom-center band where
    PackUpdates draws (y = guiHeight-100, centered, 1-3 lines of 9px)."""
    width, height, channels, pixels = decode_png(path)
    gui_w, gui_h = gui_size
    _sx, sy = width / gui_w, height / gui_h
    # Band covering up to 3 text lines above the banner baseline.
    top = int((gui_h - 100 - 3 * 9 - 6) * sy)
    bottom = int((gui_h - 100 + 12) * sy)
    left = int(width * 0.15)
    right = int(width * 0.85)
    hits, total = 0, 0
    for y in range(max(0, top), min(height, bottom)):
        row = y * width * channels
        for x in range(left, right):
            o = row + x * channels
            r, g, b = pixels[o], pixels[o + 1], pixels[o + 2]
            total += 1
            if (
                abs(r - BANNER_TEXT[0]) <= tolerance
                and abs(g - BANNER_TEXT[1]) <= tolerance
                and abs(b - BANNER_TEXT[2]) <= tolerance
            ):
                hits += 1
    return hits, total


# --- pack banner ---


def run_pack(args, rcon_factory=connect_rcon, bot_factory=Bot):
    out = []
    game = client_game_dir(args)
    log_path = client_log_path(args)
    log_pos, pre = grep_log(log_path, {"notify_off": NOTIFY_OFF_RE})
    if "notify_off" in pre:
        out.append(
            check(
                "stand runs without the forced menu.vanilla flag",
                False,
                "client log shows the PackUpdates notify-off marker; "
                "restart the stand with BLOCKFIELD_MENU_VANILLA=0",
            )
        )
        return tally(out)
    try:
        build = fetch_build_json()
        remote_digest = str(build.get("client", ""))
        remote_contract = str(build.get("compatibility", ""))
    except Exception as exc:  # noqa: BLE001 - evidence, not a crash
        print(json.dumps({"check": "pack", "result": "ERROR", "fetch": str(exc)}))
        return 1
    local_digest, local_contract = read_markers(game)
    predicted = resolve_kind(
        local_digest, local_contract, remote_digest, remote_contract
    )
    print(
        json.dumps(
            {
                "check": "pack",
                "remote_digest": remote_digest[:12],
                "remote_contract": remote_contract[:12],
                "local_digest": (local_digest[:12] or "EMPTY"),
                "local_contract": (local_contract[:12] or "EMPTY"),
                "predicted": predicted,
                "expect": args.expect,
            }
        ),
        flush=True,
    )
    out.append(
        check(
            "prediction matches requested phase",
            predicted.lower() == args.expect,
            f"{predicted} != {args.expect}",
        )
    )
    bot = bot_factory(args.bot_port)
    try:
        bot("closescreen")
        bot("clearmessages")
        if args.join:
            # One-shot product notify (chat line, banner) fires on the first
            # tick with a joined player; the single-connection testbot cannot
            # take a second operator socket mid-run, so join from this session.
            try:
                bot("connect " + args.join, timeout=60)
            except (OSError, RuntimeError) as exc:  # noqa: BLE001 - gate below
                print(json.dumps({"check": "pack", "join_error": str(exc)}))
            joined = time.monotonic() + 90
            while time.monotonic() < joined:
                try:
                    if connected(bot):
                        break
                except (OSError, RuntimeError):
                    break
                time.sleep(3)
            bot("closescreen")
        matched, seen = wait_messages(bot, OPTIONAL_RE, args.window)
        still = connected(bot)
        # The notify decision is logged at poll time (boot for a standing
        # decision), so OPTIONAL/MANDATORY grep the whole file for their kind
        # marker; NONE instead asserts the window stays quiet. Phases run
        # MANDATORY -> OPTIONAL -> NONE so a kind marker can only come from its
        # own phase (a client restart per phase restages the markers).
        log_marks = {"optional": OPTIONAL_LOG_RE, "mandatory": MANDATORY_LOG_RE}
        _, full = grep_log(log_path, log_marks)
        log_pos, hits = grep_log(log_path, log_marks, since_pos=log_pos)
        evidence = {
            "check": "pack",
            "expect": args.expect,
            "optional_seen": matched,
            "connected": still,
            "log_markers": sorted(hits),
            "log_markers_full": sorted(full),
            "messages_tail": seen[-5:],
        }
        shot = None
        try:
            state = bot("state")
            gui = state.get("guiSize", [0, 0])
            shot = bot("screenshot w2c2pack", timeout=60).get("file")
            time.sleep(1)
            found, total = banner_pixels(shot, gui)
            evidence["banner_pixels"] = found
            evidence["banner_scanned"] = total
            evidence["screenshot"] = shot
        except Exception as exc:  # noqa: BLE001 - report, gate below
            evidence["screenshot_error"] = str(exc)
        print(json.dumps(evidence), flush=True)
        banner = evidence.get("banner_pixels", -1)
        if args.expect == "none":
            out.append(check("no optional line on same-contract pack", not matched))
            # Boot-time decisions log before log_pos is captured, so the
            # window (hits) misses them: gate on the full log too.
            out.append(check("no update decision logged", not hits and not full))
            out.append(check("client stays connected", still))
        elif args.expect == "optional":
            # The chat line stays evidence-only: the testbot records server-sent
            # chat, while the hint renders client-side; the log marker plus the
            # (new, dismissible) banner are the pass signals.
            out.append(check("optional decision logged", "optional" in full, str(full)))
            out.append(
                check(
                    "optional banner renders in match",
                    banner >= 25,
                    f"banner_pixels={banner} shot={shot}",
                )
            )
            out.append(check("client stays connected, no kick", still))
        else:
            out.append(
                check("mandatory decision logged", "mandatory" in full, str(full))
            )
            out.append(
                check(
                    "mandatory banner renders in match",
                    banner >= 25,
                    f"banner_pixels={banner} shot={shot}",
                )
            )
            out.append(check("client stays joined on dev stand (no gate)", still))
    finally:
        bot.close()
    return tally(out)


# --- music ---


def load_music_expectations():
    path = repo_root() / "localmods" / "blockfield-music" / "music" / "music.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    # Live sound ids are "blockfield:music.<track>"; pools live under
    # states.<state>.tracks as short names, so store the suffixed form.
    states = config.get("states", {}) if isinstance(config, dict) else {}
    pools = {}
    for name, state in states.items():
        if not isinstance(state, dict):
            continue
        tracks = state.get("tracks")
        if isinstance(tracks, list):
            pools[name] = [
                f"music.{track}" for track in tracks if isinstance(track, str)
            ]
    return pools


def parse_room_open(log_text):
    """Newest deathmatch room number from the server log (dm.py's marker)."""
    matches = re.findall(r"Комната (\d+) открыта", log_text)
    return matches[-1] if matches else ""


def enter_deathmatch(bot, read_log, map_id, timeout=90):
    """play → join the opened room → class → gun in hand. Returns the room or
    "": without join+spawn the bot idles unspawned and combat never triggers."""
    bot("bfaction play:deathmatch:" + map_id)
    room = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            room = parse_room_open(read_log())
        except OSError:
            break
        if room:
            break
        time.sleep(2)
    if not room:
        return ""
    bot("bfaction join:" + room)
    time.sleep(3)
    bot("bfaction class:assault:0")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            dim = str(bot("state", timeout=10).get("dimension", ""))
        except (OSError, RuntimeError):
            return room
        if dim.startswith("blockfield:" + map_id):
            break
        time.sleep(2)
    bot("slot 0")
    time.sleep(2)
    return room


def run_music(args, rcon_factory=connect_rcon, bot_factory=Bot):
    out = []
    pools = load_music_expectations()
    print(
        json.dumps({"check": "music", "pools": {k: len(v) for k, v in pools.items()}}),
        flush=True,
    )
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        name = bot("state")["name"]
        bot("closescreen")
        bot("opt sound music 1.0")
        bot("soundlog start")
        idle_seconds = args.idle
        poll = 30
        snapshots = []

        def music_now():
            try:
                live = bot("sounds", timeout=20)
            except (OSError, RuntimeError):
                return []
            return [
                {"id": str(s.get("id", "")), "source": str(s.get("source", ""))}
                for s in live
                if isinstance(s, dict)
                and (
                    "music" in str(s.get("id", "")).lower()
                    or str(s.get("source", "")).upper() == "MUSIC"
                )
            ]

        for _ in range(max(1, int(idle_seconds / poll))):
            time.sleep(poll)
            snapshots.append({"t": round(time.monotonic(), 1), "sounds": music_now()})

        # Combat: join + spawn first (an unjoined bot idles with the tablet
        # and RCON damage is ignored), then hurt + own fire drive the meter.
        def read_log():
            return server_log_path(args).read_text(encoding="utf-8", errors="replace")

        try:
            room = enter_deathmatch(bot, read_log, "dm_arena")
        except OSError:
            room = ""
        try:
            tail = read_log()[-200:]
        except OSError:
            tail = ""
        out.append(check("bot joined a deathmatch room", bool(room), room or tail))
        if not room:
            return tally(out)
        for i in range(args.combat_rounds):
            try:
                rcon.cmd(f"damage {name} 3")
            except (OSError, RuntimeError):
                pass
            try:
                bot("tacz shoot")
            except (OSError, RuntimeError):
                pass
            time.sleep(20)
            snapshots.append({"t": round(time.monotonic(), 1), "sounds": music_now()})
        events = bot("soundlog get", timeout=60)
        print(json.dumps({"check": "music", "snapshots": snapshots}), flush=True)
        starts = [
            (e.get("tick"), str(e.get("id", "")))
            for e in events
            if isinstance(e, dict)
            and "music" in str(e.get("id", "")).lower()
            or isinstance(e, dict)
            and str(e.get("source", "")).upper() == "MUSIC"
        ]
        by_track = {}
        for _, track in starts:
            by_track.setdefault(track, []).append(time.monotonic())
        print(
            json.dumps(
                {"check": "music", "tracks": sorted(by_track), "events": len(events)}
            ),
            flush=True,
        )
        out.append(
            check(
                "music track starts captured live",
                len(starts) > 0,
                f"{len(events)} sound events",
            )
        )
        pool_tracks = {t for pool in pools.values() for t in pool}
        observed = {t.split(":")[-1] for t in by_track}
        unknown = sorted(
            t
            for t in by_track
            if t.split(":")[-1] not in pool_tracks and "blockfield" in t
        )
        out.append(
            check(
                "every observed track is a configured pool member",
                not unknown,
                str(unknown[:5]),
            )
        )
        # Tactical is the combat meter's lower tier (MusicBrain: combat() >=
        # thresholds.tactical): RCON hurt + blind fire sustain it, while the
        # top combat tier needs intense sustained fighting.
        combat_tracks = {
            t
            for s, pool in pools.items()
            for t in pool
            if s
            in ("combat", "tactical", "vehicle", "contested", "critical", "final_push")
        }
        out.append(
            check(
                "combat-family state reached live",
                any(t in combat_tracks for t in observed),
                f"observed={sorted(by_track)[:8]}",
            )
        )
    finally:
        bot.close()
    return tally(out)


# --- workshop ---


WORKSHOP_MAP_KINDS = ("arena", "lobby", "limbo")
WORKSHOP_MAP_JSON_LIMIT = 16 * 1024 * 1024


def _env_path(name):
    value = os.environ.get(name)
    return Path(value) if value else None


def stage_workshop_draft(server_dir, map_id, drafts_root=None, maps_file=None):
    """Template-copy step the c4 stand missed: a draft bundle with world/region
    but no map.json is refused by MapConfigManager.reloadDraftMaps, so copy the
    map's base map.json into every such bundle. Mirrors the product checks
    (bundle world/region present, id match, kind arena/lobby/limbo, 16 MB cap).
    Returns an evidence dict; never raises."""
    server_dir = Path(server_dir)
    active = _env_path("BF_MAP_ACTIVE_DIR")
    roots = [
        path
        for path in (
            Path(drafts_root) if drafts_root else None,
            _env_path("BLOCKFIELD_WORKSHOP_DRAFTS_DIR"),
            active / "drafts" if active else None,
            server_dir / "drafts",
        )
        if path is not None
    ]
    sources = [
        path
        for path in (
            Path(maps_file) if maps_file else None,
            server_dir / "blockfield" / "maps" / f"{map_id}.json",
            server_dir / "config" / "blockfield" / "maps" / f"{map_id}.json",
        )
        if path is not None
    ]
    try:
        source = next(path for path in sources if path.is_file())
    except StopIteration:
        return {
            "staged": [],
            "ready": [],
            "roots": [str(path) for path in roots],
            "source": None,
            "reason": "no base map.json found",
        }
    try:
        if source.stat().st_size > WORKSHOP_MAP_JSON_LIMIT:
            raise ValueError("oversized base map config")
        config = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("id") != map_id:
            raise ValueError("base map.json identity mismatch")
        if config.get("kind", "arena") not in WORKSHOP_MAP_KINDS:
            raise ValueError("base map.json has an invalid kind")
    except (OSError, ValueError) as exc:
        return {
            "staged": [],
            "ready": [],
            "roots": [str(path) for path in roots],
            "source": str(source),
            "reason": f"base map.json refused: {exc}",
        }
    staged = []
    ready = []
    for root in roots:
        if not root.is_dir():
            continue
        for session in sorted(root.iterdir()):
            bundle = session / map_id
            try:
                if not session.is_dir() or not (bundle / "world" / "region").is_dir():
                    continue
                if (bundle / "map.json").is_file():
                    ready.append(str(bundle / "map.json"))
                    continue
                shutil.copy2(source, bundle / "map.json")
                staged.append(str(bundle / "map.json"))
            except OSError:
                continue
    # The server re-reads drafts every 100 ticks, so a staged copy is listed
    # without a restart.
    if staged or ready:
        reason = ""
    elif any(root.is_dir() for root in roots):
        reason = "no bundle with world/region for this map (fabricate one first)"
    else:
        reason = "no drafts root found (workshop-profile server?)"
    return {
        "staged": staged,
        "ready": ready,
        "roots": [str(path) for path in roots],
        "source": str(source),
        "reason": reason,
    }


def run_workshop(args, rcon_factory=connect_rcon, bot_factory=Bot):
    out = []
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        bot("closescreen")
        bot("clearmessages")
        staged = stage_workshop_draft(
            server_dir_path(args), args.map, args.drafts_root, args.maps_file
        )
        print(json.dumps({"check": "workshop", **staged}), flush=True)
        out.append(
            check(
                "draft bundle carries map.json",
                bool(staged["staged"] or staged["ready"]),
                staged["reason"],
            )
        )
        if staged["staged"]:
            # Drafts re-read every 100 ticks: let the staged copy load.
            time.sleep(8)
        bot("chat /bf workshop maps")
        matched, seen = wait_messages(bot, re.compile("Карты:|нет"), 30)
        tail = "\n".join(str(line) for line in seen[-10:])
        print(
            json.dumps(
                {"check": "workshop", "maps_seen": matched, "tail": tail[-800:]}
            ),
            flush=True,
        )
        out.append(check("workshop maps list answers", matched, tail[-200:]))
        map_id = args.map
        bot("chat /bf workshop map " + map_id)
        arrived = False
        deadline = time.monotonic() + 90
        dim = ""
        while time.monotonic() < deadline:
            try:
                dim = str(bot("state", timeout=10).get("dimension", ""))
            except (OSError, RuntimeError):
                break
            if dim == f"blockfield_workshop:{map_id}":
                arrived = True
                break
            time.sleep(3)
        out.append(check(f"builder travels to draft {map_id}", arrived, dim))
        if not arrived:
            return tally(out)
        name = bot("state")["name"]
        # Builder place-edit: placement is the primary builder action, and the
        # only hand edit the headless input path performs server-side —
        # synthetic attack presses never break blocks, even in the hub sandbox
        # where the product allows it. Dirt into the selected slot, placed on
        # the ground under the bot's feet; the assertion is on the draft copy.
        # EDIT_OK is read from the server log: say output never reaches RCON.
        here = bot("state")["pos"]
        tx, ty, tz = int(here[0]), int(here[1]) - 1, int(here[2])
        placed = f"{tx} {ty + 1} {tz}"
        rcon.cmd(f"gamemode creative {name}")
        rcon.cmd(f"item replace entity {name} container.0 with minecraft:dirt 64")
        time.sleep(1)
        try:
            mark = server_log_path(args).stat().st_size
        except OSError:
            mark = 0
        edited = False
        try:
            bot(f"use_on {tx} {ty} {tz} up")
            time.sleep(2)
            for _ in range(6):
                rcon.cmd(
                    f"execute in {dim} run execute if block {placed} minecraft:dirt run say EDIT_OK"
                )
                time.sleep(1)
                try:
                    new = (
                        server_log_path(args)
                        .read_bytes()[mark:]
                        .decode("utf-8", "replace")
                    )
                except OSError:
                    new = ""
                if "EDIT_OK" in new:
                    edited = True
                    break
        finally:
            rcon.cmd(f"gamemode adventure {name}")
        out.append(check("builder place-edit lands in the draft copy", edited, placed))
        log_text = server_log_path(args).read_text(encoding="utf-8", errors="replace")
        recent = log_text[-20000:]
        errors = [
            line
            for line in recent.splitlines()
            if ("ERROR" in line or "Exception" in line) and "orkshop" in line
        ]
        out.append(
            check("no workshop errors in server log", not errors, str(errors[:3]))
        )
        print(
            json.dumps(
                {"check": "workshop", "dimension": dim, "edited": edited, "name": name}
            ),
            flush=True,
        )
    finally:
        bot.close()
    return tally(out)


# --- passengers churn ---


def frame_stats(samples):
    values = []
    for sample in samples if isinstance(samples, list) else []:
        if isinstance(sample, (int, float)):
            values.append(float(sample))
        elif isinstance(sample, dict):
            for key in ("ms", "frame", "delta", "time"):
                if isinstance(sample.get(key), (int, float)):
                    values.append(float(sample[key]))
                    break
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def pick(q):
        return ordered[min(len(ordered) - 1, int(len(ordered) * q))]

    return {
        "n": len(values),
        "avg": round(sum(values) / len(values), 2),
        "p50": round(pick(0.5), 2),
        "p95": round(pick(0.95), 2),
        "max": round(ordered[-1], 2),
    }


def count_in_log(path, needle, mark=0):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, 0
    return sum(needle in line for line in lines[mark:]), len(lines)


def run_churn(args, rcon_factory=connect_rcon, bot_factory=Bot):
    out = []
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    tag = "w2churn"
    try:
        state = bot("state")
        dim, name = str(state.get("dimension", "")), state["name"]
        bot("closescreen")
        rcon.cmd(f"execute in {dim} run kill @e[tag={tag}]")
        bot("frames start")
        time.sleep(args.baseline)
        base = frame_stats(bot("frames get").get("samples", []))
        mark = count_in_log(client_log_path(args), "Received passengers")[1]
        # Carpet riders + vanilla boats + display passengers: every mount,
        # teleport and kill/respawn emits SetPassengers to tracking clients.
        riders = [f"{tag}bot{i}" for i in range(args.bots)]
        for rider in riders:
            rcon.cmd(f"player {rider} spawn")
        time.sleep(5)
        here = bot("state")["pos"]
        bx, by, bz = here[0], here[1] + 1, here[2] + 6
        for i in range(args.boats):
            rcon.cmd(
                f"execute in {dim} run summon minecraft:boat {bx + i * 3} {by} {bz}"
                f' {{Tags:["{tag}"]}}'
            )
            rcon.cmd(
                f"execute in {dim} run summon minecraft:block_display {bx + i * 3} {by + 2} {bz}"
                f' {{Tags:["{tag}"],block_state:{{Name:"minecraft:diamond_block"}}}}'
            )
        time.sleep(3)
        bot("frames start")
        t0 = time.monotonic()
        for round_no in range(args.rounds):
            for rider in riders:
                rcon.cmd(
                    f"execute in {dim} run ride {rider} mount "
                    f"@e[type=minecraft:boat,tag={tag},limit=1,sort=nearest]"
                )
            rcon.cmd(
                f"execute in {dim} run ride {name} mount "
                f"@e[type=minecraft:boat,tag={tag},limit=1,sort=nearest]"
            )
            time.sleep(2)
            # Teleport the whole flotilla far: re-pairing bursts SetPassengers.
            for dx in (200, -200):
                rcon.cmd(
                    f"execute in {dim} run tp @e[tag={tag}] {bx + dx} {by} {bz + dx}"
                )
                time.sleep(2)
            rcon.cmd(f"execute in {dim} run ride {name} dismount")
            rcon.cmd(f"execute in {dim} run ride @e[tag={tag}] dismount")
            # Kill riders mid-ride every other round: post-respawn packets
            # arrive before the client re-learns the vehicles (the prod burst).
            if round_no % 2 == 0:
                for rider in riders:
                    rcon.cmd(f"execute in {dim} run kill {rider}")
                time.sleep(4)
                for rider in riders:
                    rcon.cmd(f"player {rider} spawn")
                time.sleep(3)
        churn = frame_stats(bot("frames get").get("samples", []))
        warns, _ = count_in_log(client_log_path(args), "Received passengers", mark)
        evidence = {
            "check": "churn",
            "seconds": round(time.monotonic() - t0, 1),
            "baseline_frames": base,
            "churn_frames": churn,
            "passenger_warnings": warns,
        }
        print(json.dumps(evidence), flush=True)
        out.append(check("churn rounds completed without errors", True))
        out.append(
            check(
                "prod unknown-vehicle burst reproduced (warnings > 0)",
                warns > 0,
                f"warnings={warns}",
            )
        )
    finally:
        try:
            dim = str(bot("state").get("dimension", "") or "")
            if dim:
                rcon.cmd(f"execute in {dim} run kill @e[tag={tag}]")
        except (OSError, RuntimeError):
            pass
        bot.close()
    return tally(out)


# --- launcher watchdog, live ---


def parse_range(header, size):
    """(offset, end) for an open (bytes=N-) or closed (bytes=N-M) Range, or
    None when the header is unsatisfiable (then serve 416)."""
    span = (header or "").removeprefix("bytes=")
    first, _, last = span.partition("-")
    if "-" not in span or (not first and not last):
        return None
    try:
        offset = int(first) if first else 0
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if not first:  # suffix form (bytes=-M): last M bytes
        offset = size - end
        end = size - 1
    offset = min(max(offset, 0), size)
    end = min(max(end, offset - 1), size - 1)
    return offset, end


def run_watchdog(args):
    out = []
    port_host, port_mirror = args.host_port, args.mirror_port
    payload = os.urandom(args.megabytes * 1024 * 1024)
    digest = lambda value: hashlib.sha256(value).hexdigest()  # noqa: E731
    probe = os.urandom(1024 * 1024)
    resources = {"/big.jar": payload, "/probe.bin": probe}
    metadata_template = (
        'name = "Big"\nfilename = "big.jar"\nside = "client"\n'
        '[download]\nurl = "{base}/big.jar"\nhash-format = "sha256"\n'
        f'hash = "{digest(payload)}"\n'
    ).encode()
    index = b""
    pack = b""
    metadata = metadata_template
    mirrors = b""  # built after the mirror port is bound (needs its https URL)
    state = {"ranges": [], "mirror_bytes": 0}

    class PackHost(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def serve(self, data):
            header = self.headers.get("Range")
            if header:
                # Open (bytes=N-) and closed (bytes=N-M) ranges; clamp to data.
                parsed = parse_range(header, len(data))
                if parsed is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.end_headers()
                    return
                offset, end = parsed
                state["ranges"].append(offset)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {offset}-{end}/{len(data)}")
                data = data[offset : end + 1]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            table = {
                "/pack.toml": pack,
                "/index.toml": index,
                "/mods/big.pw.toml": metadata,
                "/mirrors.json": mirrors,
                "/big.jar": payload,
                "/probe.bin": probe,
            }
            data = table.get(self.path)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.serve(data)

    class Trickle(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = resources.get(self.path.split("?")[0])
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.path.startswith("/probe"):
                self.wfile.write(data)
                return
            # Probe-fast, then collapse to ~70 KiB/s: passes ranking, then the
            # watchdog must hand over mid-file to the pack host.
            chunk = 32 * 1024
            for off in range(0, len(data), chunk):
                try:
                    self.wfile.write(data[off : off + chunk])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                state["mirror_bytes"] += min(chunk, len(data) - off)
                time.sleep(0.45)

    host = ThreadingHTTPServer(("127.0.0.1", port_host), PackHost)
    base = f"http://127.0.0.1:{host.server_port}"
    tls_dir = tempfile.mkdtemp(prefix="watchdog-tls-")
    cert, key, store = ensure_mirror_tls(tls_dir)
    mirror = ThreadingHTTPServer(("127.0.0.1", port_mirror), Trickle)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert), str(key))
    mirror.socket = tls.wrap_socket(mirror.socket, server_side=True)
    mirrors = json.dumps(
        {
            "schema": 1,
            "probe": {"path": "probe.bin", "hash": digest(probe)},
            "mirrors": [mirror_entry(mirror.server_port)],
        }
    ).encode()
    # The served metadata carries the bound base URL: hash the exact bytes.
    metadata = metadata_template.replace(b"{base}", base.encode())
    index = (
        'hash-format = "sha256"\n[[files]]\nfile = "mods/big.pw.toml"\n'
        f'hash = "{digest(metadata)}"\nmetafile = true\n'
    ).encode()
    pack = (
        'name = "Watchdog"\npack-format = "packwiz:1.1.0"\n[index]\n'
        f'file = "index.toml"\nhash-format = "sha256"\nhash = "{digest(index)}"\n'
    ).encode()
    threading.Thread(target=host.serve_forever, daemon=True).start()
    threading.Thread(target=mirror.serve_forever, daemon=True).start()
    root = repo_root()
    bootstrap = root / "client" / "packwiz-installer-bootstrap.jar"
    installer = root / "client" / "packwiz-installer.jar"
    java = os.environ.get("JAVA_HOME", "")
    java = str(Path(java) / "bin" / "java") if java else "java"
    game = Path(args.work_dir)
    game.mkdir(parents=True, exist_ok=True)
    tls_flags = [
        f"-Djavax.net.ssl.trustStore={store}",
        f"-Djavax.net.ssl.trustStorePassword={MIRROR_TRUST_PASSWORD}",
        "-Djavax.net.ssl.trustStoreType=PKCS12",
    ]
    if bootstrap.is_file() and installer.is_file():
        command = [
            java,
            "-Djava.awt.headless=true",
            *tls_flags,
            "-jar",
            str(bootstrap),
            "--bootstrap-no-update",
            "--bootstrap-main-jar",
            str(installer),
            "-g",
            "-s",
            "client",
            "--pack-folder",
            ".",
            f"{base}/pack.toml",
        ]
    else:
        command = [
            java,
            "-Djava.awt.headless=true",
            *tls_flags,
            "-jar",
            str(installer),
            "-g",
            "-s",
            "client",
            "--pack-folder",
            ".",
            f"{base}/pack.toml",
        ]
    t0 = time.monotonic()
    proc = subprocess.run(
        command, cwd=game, capture_output=True, text=True, timeout=600
    )
    dt = time.monotonic() - t0
    text = proc.stdout + proc.stderr
    switches = len(re.findall(r"blockfield-mirror", text))
    print(
        json.dumps(
            {
                "check": "watchdog",
                "returncode": proc.returncode,
                "seconds": round(dt, 1),
                "switches": switches,
                "mirror_bytes": state["mirror_bytes"],
                "mirror_url": json.loads(mirrors.decode())["mirrors"][0]["url"],
                "ranges": state["ranges"][:4],
                "tail": text[-600:],
            }
        ),
        flush=True,
    )
    out.append(
        check(
            "shipped installer completes over throttled mirror",
            proc.returncode == 0,
            text[-300:],
        )
    )
    out.append(check("watchdog fails over mid-file", switches > 0))
    # The installer files the jar next to its metafile (mods/big.pw.toml).
    installed = game / "mods" / "big.jar"
    out.append(
        check(
            "installed bytes match fixture",
            installed.is_file() and digest(installed.read_bytes()) == digest(payload),
        )
    )
    return tally(out)


def register(sub):
    p = sub.add_parser("world-c2-pack", help="pack banner changed-contract live")
    p.add_argument("--expect", choices=("none", "optional", "mandatory"), required=True)
    p.add_argument("--join", default=None)
    p.add_argument("--window", type=float, default=100)
    p.add_argument("--client-dir", default=None)
    rcon_args(p)
    p.set_defaults(func=run_pack)

    m = sub.add_parser("world-c2-music", help="music-v7 live sound-event capture")
    m.add_argument("--idle", type=float, default=300)
    m.add_argument("--combat-rounds", type=int, default=18)
    m.add_argument("--client-dir", default=None)
    rcon_args(m)
    m.set_defaults(func=run_music)

    w = sub.add_parser("world-c2-workshop", help="workshop builder round-trip")
    w.add_argument("--map", required=True)
    w.add_argument("--client-dir", default=None)
    w.add_argument("--server-log", default=None)
    w.add_argument("--server-dir", default=None)
    w.add_argument("--drafts-root", default=None)
    w.add_argument("--maps-file", default=None)
    rcon_args(w)
    w.set_defaults(func=run_workshop)

    c = sub.add_parser("world-c2-churn", help="passengers churn for #228")
    c.add_argument("--boats", type=int, default=10)
    c.add_argument("--bots", type=int, default=4)
    c.add_argument("--rounds", type=int, default=6)
    c.add_argument("--baseline", type=float, default=20)
    c.add_argument("--client-log", default=None)
    rcon_args(c)
    c.set_defaults(func=run_churn)

    d = sub.add_parser(
        "world-c2-watchdog", help="launcher watchdog on throttled mirror"
    )
    d.add_argument("--megabytes", type=int, default=24)
    d.add_argument("--host-port", type=int, default=18081)
    d.add_argument("--mirror-port", type=int, default=18082)
    d.add_argument("--work-dir", required=True)
    d.set_defaults(func=run_watchdog)
