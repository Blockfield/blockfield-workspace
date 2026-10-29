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
    hitreg,
    parkour,
    range,
    stress,
)

MODULES = (
    body_pose,
    parkour,
    hitreg,
    stress,
    dm,
    range,
    check_gameplay,
    check_consent,
    check_benelli,
)

SCENARIOS = {m.SCENARIO["name"]: m for m in MODULES}


def register(subparsers):
    for module in MODULES:
        module.register(subparsers)


def describe():
    return [dict(m.SCENARIO) for m in MODULES]
