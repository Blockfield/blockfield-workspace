#!/usr/bin/env python3
"""Shared rig-scenario plumbing: RCON plus a newline-JSON testbot client."""

import inspect
import json
import re
import select
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rcon import Rcon  # noqa: E402  (scripts/dev/rcon.py, plain stdlib)


def connect_rcon(host="127.0.0.1", port=25575, password="leaks"):
    return Rcon(host, port, password)


class Bot:
    """One testbot connection: `bot(cmd)` sends a line, returns the decoded JSON reply."""

    def __init__(self, port=47777, host="127.0.0.1", timeout=40):
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = bytearray()
        self._owed = False
        self._owed_timeouts = 0
        self._dead = False

    def _readline(self, deadline):
        """One newline-terminated reply with a total deadline: remaining time is
        recomputed before every wait, so a slow drip of partial bytes cannot
        outlive the budget (a per-read socket timeout alone would not bound it)."""
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[: newline + 1]
                return line.decode("utf8")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("testbot reply exceeded its deadline")
            ready, _, _ = select.select([self.sock], [], [], remaining)
            if not ready:
                raise TimeoutError("testbot reply exceeded its deadline")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("testbot closed the connection")
            self._buf += chunk

    def _invalidate(self):
        # A broken stream or failed send leaves framing unrecoverable, so the
        # connection is closed instead of reused. A plain reply timeout keeps
        # the socket: at most one reply stays owed and the next call drains it
        # (after 3 consecutive failed drains the connection is closed too).
        self._dead = True
        self._owed = False
        self._owed_timeouts = 0
        self._buf = bytearray()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __call__(self, command, timeout=None):
        if self._dead or self.sock is None:
            raise RuntimeError("bot connection invalidated; create a new Bot")
        if timeout is None:
            # Respect the socket's configured timeout (e.g. MCP's reduced
            # remaining budget): never reset it upward to the default.
            budget = self.sock.gettimeout()
            if budget is None:
                budget = self.timeout
        else:
            budget = max(timeout, 0.001)
        deadline = time.monotonic() + budget
        previous = self.sock.gettimeout()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("testbot call exceeded its deadline before sending")
            if previous is None:
                self.sock.settimeout(remaining)
            else:
                self.sock.settimeout(min(previous, remaining))
            if self._owed:
                # The previous call timed out waiting for this reply. Consume
                # and drop it within the new call's deadline so framing stays
                # in sync; a still-missing line keeps _owed for next time
                # (after 3 consecutive failed drains the connection is closed
                # so the error says what happened), only a broken stream
                # invalidates the connection immediately.
                try:
                    self._readline(deadline)
                except TimeoutError:
                    self._owed_timeouts += 1
                    if self._owed_timeouts >= 3:
                        self._invalidate()
                    raise
                except (ConnectionError, OSError):
                    self._invalidate()
                    raise
                self._owed = False
                self._owed_timeouts = 0
            try:
                self.sock.sendall((command + "\n").encode())
            except (TimeoutError, OSError):
                self._invalidate()
                raise
            try:
                reply = json.loads(self._readline(deadline))
            except TimeoutError:
                self._owed = True
                raise
            except (ConnectionError, OSError):
                self._invalidate()
                raise
        finally:
            if not self._dead and self.sock is not None:
                try:
                    self.sock.settimeout(previous)
                except OSError:
                    pass
        if isinstance(reply, dict) and "error" in reply:
            raise RuntimeError(f"{command}: {reply['error']}")
        return reply

    def wait_for(self, expected, timeout=15.0, interval=0.05):
        """Blocking readiness wait: dict-match on `state`, seq-aware (see wait_for)."""
        matched, state, _ = wait_for(
            lambda remaining: self("state", timeout=remaining),
            lambda s: (
                isinstance(s, dict)
                and all(k in s and s[k] == v for k, v in expected.items())
            ),
            timeout,
            interval,
        )
        return matched, state

    def close(self):
        sock, self.sock = self.sock, None
        self._dead = True
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def check(name, ok, detail=""):
    """One PASS/FAIL line; returns 0/1 for the final tally."""
    print(
        f"{'PASS' if ok else 'FAIL'} {name}"
        + (f" :: {detail}" if not ok and detail else ""),
        flush=True,
    )
    return 0 if ok else 1


def tally(results):
    failed = sum(results)
    print(f"RESULT pass={len(results) - failed} fail={failed}", flush=True)
    return failed


def wait_while(predicate, timeout, interval=1.0):
    """Poll `predicate()` until truthy; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _takes_budget(get_state):
    """True when get_state accepts the remaining-seconds budget positionally."""
    try:
        parameters = inspect.signature(get_state).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for p in parameters
    )


def wait_for(get_state, predicate, timeout, interval=0.05):
    """Blocking event/seq wait: re-evaluate `predicate(state)` only when the testbot
    `tick` seq advances, so a match returns on the first fresh tick instead of after
    a fixed sleep. States without a tick field fall back to every poll.
    `get_state(remaining_seconds)` must accept and enforce the remaining budget
    (pure in-memory getters may ignore it); zero-arg callables are rejected, so
    no wait can outlive its deadline inside an unbounded transport. A match
    observed after the deadline is rejected, never reported as success.
    Returns (matched, last_state, elapsed_seconds)."""
    if not _takes_budget(get_state):
        raise TypeError("wait_for get_state must accept remaining seconds")
    started = time.monotonic()
    deadline = started + timeout
    last_tick, state = None, None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, state, time.monotonic() - started
        try:
            state = get_state(remaining)
        except (OSError, RuntimeError, ValueError):
            state = None
        tick = state.get("tick") if isinstance(state, dict) else None
        if state is not None and (tick is None or tick != last_tick):
            last_tick = tick
            try:
                match = predicate(state)
            except (KeyError, TypeError, ValueError):
                match = False
            if match and time.monotonic() < deadline:
                return True, state, time.monotonic() - started
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, state, time.monotonic() - started
        time.sleep(min(interval, remaining))


def wait_for_port(host, port, timeout=30.0):
    """TCP readiness probe (proxy/server/bot): True once something accepts."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            with socket.create_connection((host, port), timeout=min(2.0, remaining)):
                return True
        except OSError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))


def settle_after_tp(bot, rcon, name, x, y, z, yaw=0, pitch=0, timeout=15.0):
    """Teleport `name` via RCON, then block until the bot reports the new position
    with onGround (the server reports onGround=false for a few ticks after a tp;
    asserting once flakes — poll instead). Returns the settled state."""
    rcon.cmd(
        f"execute in {bot('state')['dimension']} run tp {name} {x} {y} {z} {yaw} {pitch}"
    )
    matched, state, _ = wait_for(
        lambda remaining: bot("state", timeout=remaining),
        lambda s: (
            abs(s["pos"][0] - x) < 0.05
            and abs(s["pos"][1] - y) < 0.1
            and abs(s["pos"][2] - z) < 0.05
            and s.get("onGround")
        ),
        timeout,
    )
    if not matched:
        raise RuntimeError(
            f"settle timeout: {name} never settled at {(x, y, z)}: {state}"
        )
    return state


def reserve_run_label(directory, label):
    """Fail-closed cohort guard: refuse a label whose outputs already exist instead of
    silently overwriting a good chunk (resume under a fresh label instead)."""
    existing = sorted(
        p.name
        for p in Path(directory).glob(f"{label}*")
        if p.is_file() and p.suffix in (".log", ".json", ".ndjson")
    )
    if existing:
        raise RuntimeError(
            f"refusing to reuse completed cohort label {label!r}: {existing[0]} exists"
        )


def dimension(bot_state):
    return bot_state.get("dimension", "")


def is_lobby_dimension(dim):
    return isinstance(dim, str) and (
        dim == "minecraft:overworld"
        or re.fullmatch(
            r"blockfield_lobby:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", dim
        )
        is not None
    )


def number(response):
    match = re.search(r"data: (-?\d+(?:\.\d+)?)[bdfsL]?$", response or "")
    if not match:
        raise AssertionError(response)
    return float(match.group(1))


def carpet_bot_name(name):
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name or ""):
        raise ValueError(f"refusing to drive a suspicious carpet bot name: {name!r}")
    return name


def park_carpet_bots(rcon, names):
    """Kill live Carpet bots before a vote/close teleport.

    A dead fake player disconnects instead of riding the cross-dimension
    teleport whose extra ChunkMap.move NPEs on an unregistered section.
    """
    for name in names:
        rcon.cmd(f"player {carpet_bot_name(name)} kill")


def revive_carpet_bots(rcon, names):
    """Respawn parked Carpet bots after the vote / in the next round."""
    for name in names:
        rcon.cmd(f"player {carpet_bot_name(name)} spawn")


def rcon_args(parser):
    parser.add_argument("--rcon-host", default="127.0.0.1")
    parser.add_argument("--rcon-port", type=int, default=25575)
    parser.add_argument("--rcon-password", default="leaks")
    parser.add_argument("--bot-port", type=int, default=47777)
