#!/usr/bin/env python3
"""Fail-closed stand readiness + life-aware damage verdict (offline-testable).

Stand runs burned time because invalid setups were scored as product results:
missing bot2, undeployed or kitless victim, a kill across a respawn read as
damage, reversed action labels, walled/insufficient lanes, rejected attacks.
This module is the single contract so scenarios do not reimplement it:

- `check_ready`: one structured readiness record per engagement. Any
  missing/invalid field -> verdict INCONCLUSIVE, never a product result.
- `judge_damage`: attack event -> target life id -> damage/kill verdict. Health
  deltas across a respawn or a regen tick are not damage; a raw 19->20 rise is
  never negative damage.
- `save_stage`/`load_stage`/`inputs_hash`: record (json) + log written after
  each stage under the task's .dev dir, keyed by inputsHash, so a run resumes.

Exit codes: PASS=0, FAIL=1 (product), INCONCLUSIVE=2 (setup invalid).
`NotReady` subclasses `Exception`, never `RuntimeError`, so existing per-leg
`except (RuntimeError, OSError, ...)` handlers cannot swallow a setup refusal
into a FAIL row.
"""

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

PASS = 0
FAIL = 1
INCONCLUSIVE = 2

FORMAT = "stand-readiness/1"

# First-check-wins: the record names the first missing/invalid field.


class NotReady(Exception):
    """A readiness field is missing/invalid: INCONCLUSIVE, not product FAIL."""

    def __init__(self, field, detail=""):
        super().__init__(f"{field}: {detail}" if detail else field)
        self.field = field
        self.detail = detail


def name_token(name):
    """Regex for a whole player name: Bob must not match SuperBob or Bob_2."""
    return r"(?<![\w])" + re.escape(name) + r"(?![\w])"


def kill_count(log_text, victim_name):
    """Life id: server kill lines naming the victim, counted since the mark.

    The testbot `state`/`markers` probes expose no life id (no death counter,
    entity id or hurt time), so the join key is derived from what the server
    already emits. Only equality/deltas within one window are meaningful.
    """
    death_re = re.compile(name_token(victim_name) + r" убит ")
    return sum(1 for line in (log_text or "").splitlines() if death_re.search(line))


def judge_damage(
    *,
    hp_before,
    hp_after,
    killed,
    life_before,
    life_after,
    life_end,
    expect_hit,
    rejected=False,
    clip=None,
    expect_clips=(),
    separation=None,
    reach=None,
):
    """(True/False/None, reason): pass / product fail / inconclusive fixture.

    `killed` must already be joined to this attack's window (kill line for the
    victim after the strike mark); `life_before/after` are `kill_count` around
    the same window and `life_end` is the count after the final HP read, so a
    death plus instant respawn between the kill scan and that read cannot pose
    as the same life. A lethal hit plus instant respawn reads HP 20->20, so the
    joined kill line decides lethals and a same-life HP drop decides the rest.
    """
    if rejected:
        return None, "attack rejected or unconfirmed: no hit, no miss"
    if clip is not None and tuple(expect_clips) and clip not in expect_clips:
        # The clip/side contract gates every outcome including lethals: a kill
        # by a wrong-clip swing is harness mislabeling, never a product hit.
        return (
            None,
            f"clip/side mismatch: played {clip}, side allows {list(expect_clips)}",
        )
    if killed:
        if life_after <= life_before:
            return None, "kill line without target life change: fixture unreadable"
        if expect_hit:
            return True, "lethal hit joined to this attack's life"
        return False, "kill on a must-miss leg"
    if hp_before is None or hp_after is None:
        return None, "victim state unreadable: no hit, no miss"
    if life_after != life_before or life_end != life_before:
        return None, "respawn crossed between baseline and read: HP delta is not damage"
    if hp_after > hp_before:
        # A regen tick (raw 19->20) is never damage and never negative damage;
        # re-baseline and retry instead of scoring either way.
        return None, f"regen tick: HP rose {hp_before}->{hp_after}: not damage"
    if (
        expect_hit
        and separation is not None
        and reach is not None
        and separation > reach
    ):
        return None, f"insufficient lane: {separation:.2f}m beyond reach {reach:.2f}m"
    hit = hp_after < hp_before
    if hit == expect_hit:
        if hit:
            return True, f"hit: HP {hp_before}->{hp_after}"
        return True, "quiet must-miss: no kill, no HP drop"
    if expect_hit:
        return False, f"no damage on a must-hit leg: HP {hp_before}->{hp_after}"
    return False, f"damage on a must-miss leg: HP {hp_before}->{hp_after}"


def server_health(rcon, arena, name):
    """Server-side Health (None when unreadable): the client may still show
    HP from before an RCON probe, so baselines after a probe come from here."""
    try:
        reply = rcon.cmd(f"execute in {arena} run data get entity {name} Health")
    except (OSError, RuntimeError):
        return None
    value = re.search(r": ([-+]?\d+(?:\.\d+)?)f?\s*$", reply or "")
    return float(value[1]) if value else None


def probe_vulnerable(rcon, arena, victim_name, shooter_name, timeout=0):
    """Chip 1 HP, then leave the victim ready for another probe or attack.

    A prior probe's vanilla hurt cooldown rejects equal damage just like a
    spawn shield. Wait for server HurtTime=0 before and after probing; wall
    time alone is unsafe on a slow stand. A positive timeout also waits out
    a fresh spawn shield; the default remains a single fail-closed probe.
    """
    deadline = time.monotonic() + (timeout if timeout > 0 else 10)

    def cooled():
        while time.monotonic() < deadline:
            reply = rcon.cmd(
                f"execute in {arena} run data get entity {victim_name} HurtTime"
            )
            ticks = re.search(r": (\d+)s?\s*$", reply or "")
            if not ticks:
                return False
            if int(ticks[1]) == 0:
                return True
            time.sleep(0.1)
        return False

    while cooled():
        reply = (
            rcon.cmd(
                f"execute in {arena} run damage {victim_name}"
                f" 1 blockfield:knife by {shooter_name}"
            )
            or ""
        )
        if reply.lower().startswith("applied ") and "damage" in reply.lower():
            return cooled()
        if (
            timeout <= 0
            or "invulnerable" not in reply.lower()
            or time.monotonic() >= deadline
        ):
            break
        time.sleep(0.25)
    return False


def _field(checks, name, ok, detail=""):
    checks[name] = {"ok": bool(ok), "detail": str(detail)}
    return ok


def check_ready(
    shooter,
    victim,
    rcon,
    get_log,
    arena,
    kit,
    *,
    los=None,
    los_probe=None,
    vulnerable=None,
    mods=None,
):
    """(ready, record, missing): structured readiness for one engagement.

    Measured here: bot2 probe answered, both states readable, same dimension
    == arena, both markers deployed, shooter kit held, vulnerability (single
    RCON 1-damage probe when `vulnerable` is None; chips 1 HP, so callers must
    re-baseline HP after — `strike` already reads `hp_before` via RCON after),
    life ids, separation from positions. `los` (caller-measured, e.g. a lane
    that just rendered) or `los_probe(shooter, name, radius_m)` attests line of
    sight. `mods` (server_loaded/server_pin/client_loaded/client_pin) is
    enforced when given, else recorded as skipped: pin enforcement stays in
    `rc.version_gate`. Victim kit is recorded (held value) with deployed+alive
    required; enforcing an exact victim held item needs a live check.
    """
    checks = {}
    try:
        shooter_state = shooter("state")
    except (OSError, RuntimeError) as error:
        _field(checks, "shooter probe answered", False, str(error)[:120])
        return _record(False, checks, {}, None)
    try:
        victim_state = victim("state")
    except (OSError, RuntimeError) as error:
        _field(checks, "bot2 probe answered", False, str(error)[:120])
        return _record(False, checks, {}, None)
    _field(checks, "shooter probe answered", True, shooter_state.get("name", ""))
    _field(checks, "bot2 probe answered", True, victim_state.get("name", ""))
    shooter_name, victim_name = shooter_state.get("name"), victim_state.get("name")
    if not shooter_name or not victim_name:
        _field(checks, "both named", False, f"{shooter_name!r}/{victim_name!r}")
        return _record(False, checks, {}, None)
    _field(checks, "both named", True, f"{shooter_name}/{victim_name}")

    shooter_dim = shooter_state.get("dimension", "")
    victim_dim = victim_state.get("dimension", "")
    if not (shooter_dim == victim_dim == arena):
        _field(
            checks,
            "same dimension",
            False,
            f"{shooter_name} {shooter_dim} vs {victim_name} {victim_dim}, want {arena}",
        )
        return _record(False, checks, {}, None)
    _field(checks, "same dimension", True, arena)

    for label, bot in (("shooter", shooter), ("victim", victim)):
        try:
            markers = bot("markers")
        except (OSError, RuntimeError) as error:
            _field(checks, f"{label} deployed", False, f"markers: {str(error)[:80]}")
            return _record(False, checks, {}, None)
        if not (isinstance(markers, dict) and markers.get("deployed")):
            _field(checks, f"{label} deployed", False, f"markers={markers!r:.120}")
            return _record(False, checks, {}, None)
        _field(checks, f"{label} deployed", True, "markers deployed")

    shooter_held = shooter_state.get("held")
    if shooter_held != kit:
        _field(checks, "shooter kit", False, f"held={shooter_held!r}, want {kit!r}")
        return _record(False, checks, {}, None)
    _field(checks, "shooter kit", True, f"held={kit}")
    victim_held = victim_state.get("held")
    victim_hp = victim_state.get("health")
    if not isinstance(victim_hp, (int, float)) or victim_hp <= 0:
        _field(checks, "victim alive", False, f"health={victim_hp!r}")
        return _record(False, checks, {}, None)
    _field(checks, "victim alive", True, f"health={victim_hp}, held={victim_held!r}")

    if vulnerable is None:
        try:
            vulnerable = probe_vulnerable(rcon, arena, victim_name, shooter_name)
        except (OSError, RuntimeError) as error:
            _field(checks, "vulnerable", False, f"probe: {str(error)[:80]}")
            return _record(False, checks, {}, None)
        vuln_how = "damage probe accepted now"
    else:
        vuln_how = "attested by protection-probe loop"
    if not vulnerable:
        _field(
            checks,
            "vulnerable",
            False,
            "spawn protection still active (or probe refused)",
        )
        return _record(False, checks, {}, None)
    _field(checks, "vulnerable", True, vuln_how)

    log_text = get_log() if get_log is not None else ""
    life = {
        shooter_name: kill_count(log_text, shooter_name),
        victim_name: kill_count(log_text, victim_name),
    }
    _field(
        checks,
        "life ids",
        True,
        f"{shooter_name}={life[shooter_name]} {victim_name}={life[victim_name]}",
    )

    try:
        spos, vpos = shooter_state["pos"], victim_state["pos"]
        separation = math.dist((spos[0], spos[2]), (vpos[0], vpos[2]))
    except (KeyError, TypeError, IndexError):
        _field(checks, "separation", False, "positions unreadable")
        return _record(False, checks, life, None)
    _field(checks, "separation", True, f"{separation:.2f}m")

    los_how = "attested by placement"
    if los is None:
        if los_probe is None:
            _field(checks, "line of sight", False, "not measured")
            return _record(False, checks, life, separation)
        try:
            los = bool(los_probe(shooter, victim_name, separation + 16.0))
            los_how = "probed now"
        except (OSError, RuntimeError) as error:
            _field(checks, "line of sight", False, f"probe: {str(error)[:80]}")
            return _record(False, checks, life, separation)
    if not los:
        _field(checks, "line of sight", False, "victim not rendered by shooter")
        return _record(False, checks, life, separation)
    _field(checks, "line of sight", True, f"victim rendered by shooter ({los_how})")

    if mods is None:
        _field(
            checks, "mods", True, "skipped: pin enforcement stays in rc.version_gate"
        )
    else:
        try:
            server_ok = mods["server_loaded"] == mods["server_pin"]
            client_ok = mods["client_loaded"] == mods["client_pin"]
        except (KeyError, TypeError):
            _field(checks, "mods", False, "mods mapping needs loaded+pin per side")
            return _record(False, checks, life, separation)
        if not (server_ok and client_ok):
            _field(
                checks,
                "mods",
                False,
                f"server {mods.get('server_loaded')}/{mods.get('server_pin')}"
                f" client {mods.get('client_loaded')}/{mods.get('client_pin')}",
            )
            return _record(False, checks, life, separation)
        _field(checks, "mods", True, "loaded match pins")

    return _record(True, checks, life, separation)


def _record(ready, checks, life, separation):
    missing = ""
    for name, check in checks.items():
        if not check["ok"]:
            missing = f"{name}: {check['detail']}"
            break
    record = {
        "format": FORMAT,
        "verdict": "READY" if ready else "INCONCLUSIVE",
        "checks": checks,
        "life": life,
        "separation_m": separation,
    }
    return ready, record, missing


def server_build(server_dir=None):
    """Identity of the server's mod jars (name, size, mtime), or None if unknown.

    A rebuild rewrites the jar, so a resumed PASS from another build no longer matches.
    """
    if server_dir is None:
        home = os.environ.get("BF_DEV_HOME")
        root = Path(home) if home else Path(__file__).resolve().parents[3] / ".dev"
        server_dir = root / "server"
    jars = sorted(Path(server_dir, "mods").glob("*.jar"))
    if not jars:
        return None
    return hashlib.sha1(
        json.dumps(
            [[j.name, j.stat().st_size, j.stat().st_mtime_ns] for j in jars]
        ).encode()
    ).hexdigest()


def inputs_hash(inputs):
    """Stable key for a stage set: same inputs -> same directory."""
    return hashlib.sha1(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def stage_root(scenario):
    """Task-owned stage dir: BF_DEV_HOME when set, else .dev/<scenario>."""
    return Path(os.environ.get("BF_DEV_HOME", f".dev/{scenario}")) / "stages"


def save_stage(root, hash, stage, record, log_text=""):
    """Write <stage>.json + <stage>.log under root/hash; returns the directory."""
    directory = Path(root) / hash
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stage}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / f"{stage}.log").write_text(log_text or "", encoding="utf-8")
    return directory


def load_stage(root, hash, stage):
    """Saved stage record, or None when absent/corrupt (never raises)."""
    try:
        return json.loads(
            (Path(root) / hash / f"{stage}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
