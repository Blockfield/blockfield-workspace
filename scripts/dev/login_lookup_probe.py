#!/usr/bin/env python3
"""BEFORE/AFTER probe for login-lookups: time Carpet `/player <name> spawn`.

A profile-cache miss makes Carpet resolve the name through Mojang
`findProfilesByNames` on the server thread; a cache hit never touches the
network. Subcommands (server start/stop is the caller's job):

  strip <usercache>  remove every SCENARIO_BOTS entry -> BEFORE state (lookups real)
  spawn <out.json>   time `player <name> spawn` for probe bots via RCON against the
                     running stand server, then kill them; record keep-up lines
                     appended to server.out during the window.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

DEV = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
from rcon import Rcon  # noqa: E402  (scripts/dev/rcon.py, plain stdlib)

spec = importlib.util.spec_from_file_location("devstack", DEV / "devstack.py")
devstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devstack)

PROBE_BOTS = ["hitbot", "TabletBot", "bot01", "bot02", "bot03"]
LOG = devstack.SERVER / "server.out"


def cmd_strip(cache_path):
    path = Path(cache_path)
    bots = {b.lower() for b in devstack.SCENARIO_BOTS}
    entries = json.loads(path.read_text(encoding="utf-8"))
    kept = [
        e
        for e in entries
        if not (isinstance(e, dict) and str(e.get("name", "")).lower() in bots)
    ]
    removed = len(entries) - len(kept)
    path.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    print(f"stripped {removed} scenario-bot entries, kept {len(kept)}")


def cmd_spawn(out_path):
    rcon = Rcon()
    start_size = LOG.stat().st_size if LOG.is_file() else 0
    spawns = []
    for name in PROBE_BOTS:
        t0 = time.monotonic()
        try:
            reply = rcon.cmd(f"player {name} spawn")
        except Exception as exc:  # noqa: BLE001 - record and continue
            reply = f"ERROR: {exc}"
        dt = time.monotonic() - t0
        spawns.append({"name": name, "seconds": round(dt, 3), "reply": reply[:200]})
        print(f"{name}: {dt:.2f}s -> {reply[:120]}", flush=True)
        time.sleep(2)
    for name in PROBE_BOTS:
        try:
            rcon.cmd(f"player {name} kill")
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
    appended = (
        LOG.read_bytes()[start_size:].decode("utf-8", errors="replace")
        if LOG.is_file()
        else ""
    )
    keepups = [line for line in appended.splitlines() if "Can't keep up" in line]
    mojang = [
        line
        for line in appended.splitlines()
        if any(k in line.lower() for k in ("mojang", "sessionserver", "timed out"))
    ]
    result = {"spawns": spawns, "keepups": keepups, "mojang_lines": mojang}
    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"keep-ups in window: {len(keepups)}, mojang-ish lines: {len(mojang)}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("strip", "spawn"):
        raise SystemExit(f"usage: {__file__} strip <usercache> | spawn <out.json>")
    if sys.argv[1] == "strip":
        cmd_strip(sys.argv[2])
    else:
        cmd_spawn(sys.argv[2])
