#!/usr/bin/env python3
"""Stand/prod seam: the same entry + gate + version assertions against a stand
and against prod (read-only), so differences show up as a diff.

The stand leg runs against the local devstack (direct, or --via-proxy with the
stand proxy from ws-private#173: RakNet port + proxy gate.json). The prod leg
only reads public endpoints (status/RakNet pings, /api/rooms, build.json):
no login, no credentials, nothing written.

Usage:
  just server start [--via-proxy]          # on your own stand (AGENTS.md)
  just scenario seam [--via-proxy] [--tag-prod v2.79.2]

One side-by-side row per check; exit 1 on any entry/gate FAIL. Version cells
are informational: stand-vs-pin mismatch FAILs (G0 identity: the stand must
run the pinned build), stand-vs-prod drift is reported, never failed.
"""

import importlib.util
import json
import sys
import urllib.request

from .common import check, connect_rcon, rcon_args, tally

SCENARIO = {
    "name": "seam",
    "help": "stand/prod seam: same entry+gate+version checks on both, diff output",
}
PROD_TCP = ("play.blockfield.pro", 25565)
PROD_UDP = ("play.blockfield.pro", 25566)
PROD_ROOMS = "https://blockfield.pro/api/rooms"
PROD_BUILD = "https://blockfield.pro/build.json"


def load_probe(root):
    path = root / "server" / "scripts" / "entry_liveness.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("seam_entry_liveness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_json(url):
    request = urllib.request.Request(
        url, headers={"Cache-Control": "no-cache", "User-Agent": "blockfield-seam"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def entry_rows(probe, label, tcp, udp, rooms_url, gate_file, want_game, want_workshop):
    """Entry + gate assertions for one target; (rows, states) with probe detail."""
    rows = []
    try:
        status = probe.status_ping(*tcp, tcp[0])
    except Exception as error:
        rows.append((f"{label} tcp_game", False, f"status ping failed ({error})"))
        game_open, game_motd = False, ""
    else:
        game_motd = probe.text(status.get("description", "")).strip()
        game_open = "blockfield" in game_motd.lower() and not any(
            marker in game_motd for marker in probe.CLOSED_MARKERS
        )
        rows.append(
            (
                f"{label} tcp_game",
                game_open == (want_game == "public"),
                f"motd={game_motd[:60]!r}",
            )
        )
    if udp is None:
        rows.append((f"{label} raknet", None, "no RakNet listener on a direct stand"))
    else:
        try:
            motd = probe.raknet_ping(*udp)
        except Exception as error:
            rows.append(
                (f"{label} raknet", False, f"unconnected ping failed ({error})")
            )
        else:
            rows.append((f"{label} raknet", True, f"pong motd={motd[:60]!r}"))
    if rooms_url is None:
        rows.append((f"{label} rooms", None, "no public rooms on a stand"))
    else:
        try:
            rooms = public_json(rooms_url)
        except Exception as error:
            rows.append((f"{label} rooms", False, f"unreachable ({error})"))
        else:
            rows.append(
                (
                    f"{label} rooms",
                    rooms.get("available") is True,
                    f"build={rooms.get('build')} available={rooms.get('available')}",
                )
            )
    if gate_file is None or not gate_file.is_file():
        rows.append((f"{label} gate_file", None, "no gate file for this target"))
    else:
        try:
            states = probe.gate_file_states(str(gate_file))
        except (OSError, ValueError) as error:
            rows.append((f"{label} gate_file", False, f"unreadable ({error})"))
        else:
            want = {"game": want_game, "workshop": want_workshop}
            rows.append(
                (
                    f"{label} gate_file",
                    all(states.get(leg) == state for leg, state in want.items()),
                    f"{states}",
                )
            )
    return rows


def version_rows(args, root, rcon_factory):
    """G0 identity per target: stand mod vs pins (fails), prod build vs tag."""
    from . import rc

    rows = []
    try:
        pin = rc.pin_version(
            (root / "server" / "mods" / "blockfield-server.pw.toml").read_text()
        )
    except (OSError, ValueError, KeyError) as error:
        rows.append(("stand version == pin", None, f"pin unreadable ({error})"))
        pin = None
    try:
        rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
        loaded = rc.loaded_version(rcon.cmd("bf version"))
    except Exception as error:
        rows.append(("stand version == pin", None, f"stand RCON failed ({error})"))
    else:
        rows.append(
            (
                "stand version == pin",
                None if pin is None else loaded == pin,
                f"stand={loaded} pin={pin}",
            )
        )
    try:
        build = public_json(PROD_ROOMS).get("build")
    except Exception as error:
        rows.append(("prod build", None, f"/api/rooms unreachable ({error})"))
    else:
        rows.append(
            (
                "prod build",
                True,
                f"rooms={build}"
                + (f" want={args.tag_prod}" if args.tag_prod else "")
                + (
                    " MATCH"
                    if not args.tag_prod or build == args.tag_prod
                    else " DRIFT"
                ),
            )
        )
        if args.tag_prod and build != args.tag_prod:
            rows[-1] = (rows[-1][0], False, rows[-1][2])
    return rows


def run(args, rcon_factory=connect_rcon):
    from devstack import HOME, ROOT

    probe = load_probe(ROOT)
    if probe is None:
        print("SKIP seam :: server checkout missing, no entry probe", flush=True)
        return 0
    stand_gate = HOME / "proxy" / "gate.json" if args.via_proxy else None
    stand_rows = entry_rows(
        probe,
        "stand",
        ("127.0.0.1", 25565),
        ("127.0.0.1", 25566) if args.via_proxy else None,
        None,
        stand_gate,
        args.expect_stand_game,
        args.expect_stand_workshop,
    )
    prod_rows = entry_rows(
        probe,
        "prod",
        PROD_TCP,
        PROD_UDP,
        PROD_ROOMS,
        None,
        "public",
        "builders",
    )
    rows = version_rows(args, ROOT, rcon_factory) + stand_rows + prod_rows
    print("--- stand/prod seam ---", flush=True)
    for name, ok, evidence in rows:
        mark = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        print(f"{mark} {name} :: {evidence}", flush=True)
    diffs = [
        f"{s[0][len('stand ') :]}: stand<{s[2]}> vs prod<{p[2]}>"
        for s, p in zip(stand_rows, prod_rows)
        if s[0][len("stand ") :] == p[0][len("prod ") :] and s[2] != p[2]
    ]
    if diffs:
        print("DIFF " + "; ".join(diffs), flush=True)
    else:
        print("DIFF none: stand and prod agree", flush=True)
    results = [
        check(name, True if ok is None else ok, evidence) for name, ok, evidence in rows
    ]
    failed = [name for (name, ok, _), result in zip(rows, results) if ok is False]
    tally(results)
    return 1 if failed else 0


def register(sub):
    parser = sub.add_parser("seam", help=SCENARIO["help"])
    parser.add_argument(
        "--via-proxy",
        action="store_true",
        help="stand runs behind the stand proxy (#173): RakNet leg + proxy gate.json",
    )
    parser.add_argument("--expect-stand-game", default="public")
    parser.add_argument("--expect-stand-workshop", default="closed")
    parser.add_argument(
        "--tag-prod",
        default=None,
        metavar="VTAG",
        help="expected prod build (rooms); mismatch FAILs the prod build row",
    )
    rcon_args(parser)
    parser.set_defaults(func=run)


if __name__ == "__main__":
    sys.exit("use `just scenario seam`")
