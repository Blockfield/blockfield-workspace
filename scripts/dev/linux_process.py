"""Identify Linux processes and stop the verified process through a pidfd."""

import os
import select
import signal
from pathlib import Path


def identity(pid):
    process = Path("/proc") / str(pid)
    try:
        fields = process.joinpath("stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return None
        current = {
            "pid": pid,
            "start_ticks": int(fields[19]),
            "exe": os.readlink(process / "exe"),
            "cwd": os.readlink(process / "cwd"),
        }
        fields = process.joinpath("stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X") or int(fields[19]) != current["start_ticks"]:
            return None
        return current
    except (FileNotFoundError, ProcessLookupError):
        return None


def matches(record, current):
    return bool(
        current
        and record.get("pid") == current["pid"]
        and type(record.get("start_ticks")) is int
        and record["start_ticks"] > 0
        and record["start_ticks"] == current["start_ticks"]
        and isinstance(record.get("exe"), str)
        and record["exe"] == current["exe"]
        and isinstance(record.get("cwd"), str)
        and record["cwd"] == current["cwd"]
    )


def stop(record, graceful=None, timeout=30):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Linux process stops require native pidfd support")
    try:
        handle = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        return
    try:
        exited = select.poll()
        exited.register(handle, select.POLLIN)
        if exited.poll(0):
            return
        current = identity(record["pid"])
        if current is None:
            return
        if not matches(record, current):
            raise RuntimeError(
                "Linux process birth/executable/runtime directory no longer matches the record"
            )
        if exited.poll(0):
            return
        if graceful:
            graceful()
        else:
            # A pidfd keeps signals on the verified process even if its PID is reused.
            signal.pidfd_send_signal(handle, signal.SIGTERM)
        if not exited.poll(int(timeout * 1000)):
            # A client can hang in its shutdown hooks (a shader pack under VirtualGL ignored
            # SIGTERM). A signal stop escalates on the same verified pidfd; a graceful (RCON)
            # server stop never does, so its world save is not cut short.
            if graceful:
                raise TimeoutError("Linux process did not stop before the deadline")
            signal.pidfd_send_signal(handle, signal.SIGKILL)
            if not exited.poll(10_000):
                raise TimeoutError("Linux process did not stop before the deadline")
    finally:
        os.close(handle)
