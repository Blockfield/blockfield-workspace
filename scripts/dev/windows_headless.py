"""Launch an owned child on a verified, noninteractive Windows window station."""

import ctypes
import math
import os
import subprocess
import sys
import uuid
from ctypes import wintypes
from functools import cache


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        *[
            (name, wintypes.DWORD)
            for name in (
                "dwX",
                "dwY",
                "dwXSize",
                "dwYSize",
                "dwXCountChars",
                "dwYCountChars",
                "dwFillAttribute",
                "dwFlags",
            )
        ],
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class UserObjectFlags(ctypes.Structure):
    _fields_ = [
        ("fInherit", wintypes.BOOL),
        ("fReserved", wintypes.BOOL),
        ("dwFlags", wintypes.DWORD),
    ]


@cache
def _api():
    if sys.platform != "win32":
        raise RuntimeError("the isolated Windows backend requires Windows")
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        user: {
            "CreateWindowStationW": (
                [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p],
                wintypes.HANDLE,
            ),
            "CloseWindowStation": ([wintypes.HANDLE], wintypes.BOOL),
            "GetProcessWindowStation": ([], wintypes.HANDLE),
            "SetProcessWindowStation": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateDesktopW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                ],
                wintypes.HANDLE,
            ),
            "CloseDesktop": ([wintypes.HANDLE], wintypes.BOOL),
            "GetThreadDesktop": ([wintypes.DWORD], wintypes.HANDLE),
            "GetUserObjectInformationW": (
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
        },
        kernel: {
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "GetCurrentThreadId": ([], wintypes.DWORD),
            "DuplicateHandle": (
                [
                    wintypes.HANDLE,
                    wintypes.HANDLE,
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.HANDLE),
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ],
                wintypes.BOOL,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "InitializeProcThreadAttributeList": (
                [
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(ctypes.c_size_t),
                ],
                wintypes.BOOL,
            ),
            "UpdateProcThreadAttribute": (
                [
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
            "CreateProcessW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.LPCWSTR,
                    ctypes.POINTER(StartupInfoEx),
                    ctypes.POINTER(ProcessInformation),
                ],
                wintypes.BOOL,
            ),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetExitCodeProcess": (
                [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        },
    }
    for library, functions in signatures.items():
        for name, (arguments, result) in functions.items():
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
    return user, kernel


def _check(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def object_name(user, handle):
    name = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    _check(
        user.GetUserObjectInformationW(
            handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
        )
    )
    return name.value


def station_visible(user, station):
    flags = UserObjectFlags()
    needed = wintypes.DWORD()
    _check(
        user.GetUserObjectInformationW(
            station, 1, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed)
        )
    )
    return bool(flags.dwFlags & 1)


def current_backend():
    """Only the isolated child calls this, before creating any window."""
    user, kernel = _api()
    expected_station = os.environ.get("BF_HEADLESS_STATION", "")
    expected_desktop = os.environ.get("BF_HEADLESS_DESKTOP", "")
    owner = os.environ.get("BF_HEADLESS_OWNER", "")
    if (
        not owner
        or not expected_station.startswith("Blockfield-")
        or not expected_desktop.startswith("Blockfield-")
    ):
        raise RuntimeError("isolated backend receipt is missing")
    station = _check(user.GetProcessWindowStation())
    desktop = _check(user.GetThreadDesktop(kernel.GetCurrentThreadId()))
    actual = {
        "station": object_name(user, station),
        "desktop": object_name(user, desktop),
        "owner": owner,
        "visible": station_visible(user, station),
    }
    if (
        actual["station"] != expected_station
        or actual["desktop"] != expected_desktop
        or actual["visible"]
    ):
        raise RuntimeError("isolated child backend identity does not match its receipt")
    return actual


class IsolatedProcess:
    def __init__(self, user, kernel, information, station, desktop, backend, identity):
        self._user, self._kernel = user, kernel
        self._handle = information.hProcess
        self._station, self._desktop = station, desktop
        self.pid = int(information.dwProcessId)
        self.backend, self.identity = backend, identity

    def poll(self):
        state = self._kernel.WaitForSingleObject(self._handle, 0)
        if state == 258:
            return None
        if state != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        _check(self._kernel.GetExitCodeProcess(self._handle, ctypes.byref(code)))
        return code.value

    def wait(self, timeout=30):
        if timeout is None or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("an isolated child wait requires a finite deadline")
        state = self._kernel.WaitForSingleObject(
            self._handle, min(int(timeout * 1000), 0xFFFFFFFE)
        )
        if state == 258:
            raise subprocess.TimeoutExpired(self.pid, timeout)
        if state != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def terminate(self):
        if self.poll() is None:
            _check(self._kernel.TerminateProcess(self._handle, 1))
            self.wait(timeout=5)

    def close(self):
        for attribute, close in (
            ("_handle", self._kernel.CloseHandle),
            ("_desktop", self._user.CloseDesktop),
            ("_station", self._user.CloseWindowStation),
        ):
            handle = getattr(self, attribute)
            if handle:
                _check(close(handle))
                setattr(self, attribute, None)


def _file_handle(file):
    import msvcrt

    return msvcrt.get_osfhandle(file.fileno())


def launch(command, cwd, env, log_file, owner):
    """Assign a verified private backend and retain the child's native process handle."""
    user, kernel = _api()
    if not command or not isinstance(owner, str) or not owner:
        raise ValueError("an isolated launch needs a command and session owner")
    token = uuid.uuid4().hex
    station_name, desktop_name = f"Blockfield-{token}", f"Blockfield-{token}-game"
    station = desktop = None
    information = ProcessInformation()
    inherited = []
    attributes = None
    initialized = False
    try:
        station = _check(user.CreateWindowStationW(station_name, 1, 0x037F, None))
        if object_name(user, station) != station_name or station_visible(user, station):
            raise RuntimeError(
                "Windows backend must be a new noninteractive window station"
            )
        previous = _check(user.GetProcessWindowStation())
        _check(user.SetProcessWindowStation(station))
        try:
            desktop = _check(
                user.CreateDesktopW(desktop_name, None, None, 0, 0x00C7, None)
            )
        finally:
            _check(user.SetProcessWindowStation(previous))
        if object_name(user, desktop) != desktop_name:
            raise RuntimeError("Windows backend desktop identity does not match")

        current = kernel.GetCurrentProcess()
        with open(os.devnull, "rb") as stdin:
            for file in (stdin, log_file):
                copied = wintypes.HANDLE()
                _check(
                    kernel.DuplicateHandle(
                        current,
                        _file_handle(file),
                        current,
                        ctypes.byref(copied),
                        0,
                        True,
                        2,
                    )
                )
                inherited.append(copied.value)
        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(size.value)
        _check(
            kernel.InitializeProcThreadAttributeList(
                attributes, 1, 0, ctypes.byref(size)
            )
        )
        initialized = True
        handles = (wintypes.HANDLE * len(inherited))(*inherited)
        _check(
            kernel.UpdateProcThreadAttribute(
                attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None
            )
        )
        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.lpDesktop = station_name + "\\" + desktop_name
        startup.StartupInfo.dwFlags = 0x100
        startup.StartupInfo.hStdInput = inherited[0]
        startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = inherited[1]
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        environment = dict(os.environ if env is None else env)
        environment.pop("DISPLAY", None)
        environment.pop("WAYLAND_DISPLAY", None)
        environment.update(
            ALSOFT_DRIVERS="null",
            BF_HEADLESS_STATION=station_name,
            BF_HEADLESS_DESKTOP=desktop_name,
            BF_HEADLESS_OWNER=owner,
        )
        if any(
            "\0" in key or "=" in key or "\0" in value
            for key, value in environment.items()
        ):
            raise ValueError("invalid Windows child environment")
        block = ctypes.create_unicode_buffer(
            "\0".join(
                f"{key}={value}"
                for key, value in sorted(
                    environment.items(), key=lambda item: item[0].casefold()
                )
            )
            + "\0\0"
        )
        arguments = ctypes.create_unicode_buffer(
            subprocess.list2cmdline(list(map(str, command)))
        )
        flags = 0x00080000 | 0x00000400 | 0x08000000 | 0x00000200 | 0x00000004
        _check(
            kernel.CreateProcessW(
                str(command[0]),
                arguments,
                None,
                None,
                True,
                flags,
                block,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(information),
            )
        )
        # A suspended console/JVM thread may not have connected to USER32 yet.
        ctypes.set_last_error(0)
        assigned = user.GetThreadDesktop(information.dwThreadId)
        if not assigned and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        if assigned and object_name(user, assigned) != desktop_name:
            raise RuntimeError(
                "suspended Windows child did not acquire its isolated desktop"
            )
        import windows_process

        # Verify through the same newly created handle before any child code can run.
        identity = windows_process._identity(
            windows_process._api(), information.hProcess, int(information.dwProcessId)
        )
        if identity is None:
            raise RuntimeError("isolated Windows child exited before registration")
        backend = {
            "station": station_name,
            "desktop": desktop_name,
            "thread": int(information.dwThreadId),
            "owner": owner,
            "noninteractive": True,
            "assignment": "lpDesktop",
            "child_desktop_verified": bool(assigned),
        }
        if kernel.ResumeThread(information.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        process = IsolatedProcess(
            user, kernel, information, station, desktop, backend, identity
        )
        station = desktop = None
        information.hProcess = None
        return process
    except BaseException:
        if information.hProcess:
            _check(kernel.TerminateProcess(information.hProcess, 1))
            if kernel.WaitForSingleObject(information.hProcess, 5000) != 0:
                raise RuntimeError(
                    "failed isolated child did not exit within 5 seconds"
                )
        raise
    finally:
        if initialized:
            kernel.DeleteProcThreadAttributeList(attributes)
        for handle in inherited:
            _check(kernel.CloseHandle(handle))
        for handle in (information.hThread, information.hProcess):
            if handle:
                _check(kernel.CloseHandle(handle))
        if desktop:
            _check(user.CloseDesktop(desktop))
        if station:
            _check(user.CloseWindowStation(station))
