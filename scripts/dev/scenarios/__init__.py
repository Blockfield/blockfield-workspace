#!/usr/bin/env python3
"""Rig scenarios ported onto the devstack API (Python, no bash, no rig paths).

Each module exposes a `register(subparsers)` for `just scenario <name>` plus `SCENARIO`
metadata for the MCP `run_scenario` tool. Shared helpers live in `common.py`.

The testbot `forge <kind>` commands stay rig-only experiments: they forge raw
client->server packets and are not used by any scenario here.
"""

from . import (
    body_pose,
    check_benelli,
    check_consent,
    check_gameplay,
    dm,
    duo,
    entry,
    hitreg,
    hud_checks,
    knife,
    parkour,
    pose_overlay,
    range,
    rc,
    seam,
    stress,
    tablet,
    world_c2,
)

MODULES = (
    body_pose,
    parkour,
    hitreg,
    pose_overlay,
    stress,
    dm,
    duo,
    entry,
    range,
    check_gameplay,
    check_consent,
    check_benelli,
    hud_checks,
    knife,
    tablet,
    rc,
    seam,
    world_c2,
)

SCENARIOS = {m.SCENARIO["name"]: m for m in MODULES}


def register(subparsers):
    for module in MODULES:
        module.register(subparsers)


def describe():
    return [dict(m.SCENARIO) for m in MODULES]
