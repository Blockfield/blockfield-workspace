"""Identify and stop a Windows process through the same native handle."""

import ctypes
import os
import socket
from contextlib import contextmanager
from ctypes import wintypes
from functools import cache


@cache
def _api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": (
            [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
            wintypes.HANDLE,
        ),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "GetProcessTimes": (
            [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4,
            wintypes.BOOL,
        ),
        "QueryFullProcessImageNameW": (
            [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        ),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


@contextmanager
def _handle(pid, terminate=False):
    api = _api()
    handle = api.OpenProcess(0x1000 | 0x100000 | int(terminate), False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # An exited PID no longer exists.
            yield api, None
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield api, handle
    finally:
        api.CloseHandle(handle)


def _identity(api, handle, pid):
    if not handle:
        return None
    state = api.WaitForSingleObject(handle, 0)
    if state == 0:
        return None
    if state != 258:
        raise ctypes.WinError(ctypes.get_last_error())
    times = [wintypes.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    if not api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "pid": pid,
        "birth": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
        "exe": buffer.value,
    }


def identity(pid):
    with _handle(pid) as (api, handle):
        return _identity(api, handle, pid)


def matches(record, current):
    return bool(
        current
        and record.get("pid") == current["pid"]
        and type(record.get("birth")) is int
        and record["birth"] > 0
        and record["birth"] == current["birth"]
        and isinstance(record.get("exe"), str)
        and os.path.normcase(record["exe"]) == os.path.normcase(current["exe"])
    )


def stop(record, graceful=None, timeout=30):
    # Holding this handle across verification prevents a reused PID being terminated.
    with _handle(record["pid"], terminate=graceful is None) as (api, handle):
        current = _identity(api, handle, record["pid"])
        if current is None:
            return
        if not matches(record, current):
            raise RuntimeError(
                "Windows process birth/executable no longer matches the record"
            )
        if graceful:
            graceful()
        elif not api.TerminateProcess(handle, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if api.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            raise TimeoutError("Windows process did not stop before the deadline")


def owns_listener(pid, port, peer_port=None):
    """Check the server's listener, or its established connection to a local peer."""
    api = ctypes.WinDLL("iphlpapi", use_last_error=True)
    query = api.GetExtendedTcpTable
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    ]
    query.restype = wintypes.DWORD
    table = 3 if peer_port is None else 5
    for family, stride, port_index, peer_index, pid_index in (
        (2, 6, 2, 4, 5),
        (23, 14, 5, 11, 13),
    ):
        size = wintypes.DWORD()
        result = query(None, ctypes.byref(size), False, family, table, 0)
        if result != 122:
            raise ctypes.WinError(result)
        buffer = ctypes.create_string_buffer(size.value)
        result = query(buffer, ctypes.byref(size), False, family, table, 0)
        if result:
            raise ctypes.WinError(result)
        rows = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32))
        if any(
            rows[1 + index * stride + pid_index] == pid
            and socket.ntohs(rows[1 + index * stride + port_index] & 0xFFFF) == port
            and (
                peer_port is None
                or socket.ntohs(rows[1 + index * stride + peer_index] & 0xFFFF)
                == peer_port
            )
            for index in range(rows[0])
        ):
            return True
    return False
