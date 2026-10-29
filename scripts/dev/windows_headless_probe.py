"""Native CI proof using only the child-owned, noninteractive Windows backend."""

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from windows_headless import _api, _check, current_backend  # noqa: E402


class PixelFormatDescriptor(ctypes.Structure):
    _fields_ = [
        ("nSize", wintypes.WORD),
        ("nVersion", wintypes.WORD),
        ("dwFlags", ctypes.c_uint32),
        *[
            (name, wintypes.BYTE)
            for name in (
                "iPixelType",
                "cColorBits",
                "cRedBits",
                "cRedShift",
                "cGreenBits",
                "cGreenShift",
                "cBlueBits",
                "cBlueShift",
                "cAlphaBits",
                "cAlphaShift",
                "cAccumBits",
                "cAccumRedBits",
                "cAccumGreenBits",
                "cAccumBlueBits",
                "cAccumAlphaBits",
                "cDepthBits",
                "cStencilBits",
                "cAuxBuffers",
                "iLayerType",
                "bReserved",
            )
        ],
        ("dwLayerMask", ctypes.c_uint32),
        ("dwVisibleMask", ctypes.c_uint32),
        ("dwDamageMask", ctypes.c_uint32),
    ]


def render_wgl(user, window, library_path):
    current_backend()
    path = Path(library_path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("--wgl requires an existing absolute Mesa DLL path")
    with os.add_dll_directory(str(path.parent)):
        gl = ctypes.WinDLL(str(path), use_last_error=True)
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        signatures = {
            user: {
                "GetDC": ([wintypes.HANDLE], wintypes.HANDLE),
                "ReleaseDC": ([wintypes.HANDLE, wintypes.HANDLE], ctypes.c_int),
            },
            gdi: {
                "ChoosePixelFormat": (
                    [wintypes.HANDLE, ctypes.POINTER(PixelFormatDescriptor)],
                    ctypes.c_int,
                ),
                "SetPixelFormat": (
                    [
                        wintypes.HANDLE,
                        ctypes.c_int,
                        ctypes.POINTER(PixelFormatDescriptor),
                    ],
                    wintypes.BOOL,
                ),
            },
            gl: {
                "wglCreateContext": ([wintypes.HANDLE], wintypes.HANDLE),
                "wglMakeCurrent": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
                "wglDeleteContext": ([wintypes.HANDLE], wintypes.BOOL),
                "glGetString": ([ctypes.c_uint], ctypes.c_char_p),
                "glGetError": ([], ctypes.c_uint),
                "glViewport": ([ctypes.c_int] * 4, None),
                "glClearColor": ([ctypes.c_float] * 4, None),
                "glClear": ([ctypes.c_uint], None),
                "glReadBuffer": ([ctypes.c_uint], None),
                "glReadPixels": (
                    [ctypes.c_int] * 4
                    + [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p],
                    None,
                ),
                "glFinish": ([], None),
            },
        }
        for library, functions in signatures.items():
            for name, (arguments, result) in functions.items():
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, result
        dc = _check(user.GetDC(window))
        context = None
        active = False
        try:
            descriptor = PixelFormatDescriptor()
            descriptor.nSize, descriptor.nVersion = ctypes.sizeof(descriptor), 1
            descriptor.dwFlags = 0x25
            descriptor.cColorBits, descriptor.cAlphaBits = 24, 8
            descriptor.cDepthBits, descriptor.cStencilBits = 24, 8
            pixel_format = _check(gdi.ChoosePixelFormat(dc, ctypes.byref(descriptor)))
            _check(gdi.SetPixelFormat(dc, pixel_format, ctypes.byref(descriptor)))
            context = _check(gl.wglCreateContext(dc))
            _check(gl.wglMakeCurrent(dc, context))
            active = True
            details = {}
            for name, key in (
                ("version", 0x1F02),
                ("renderer", 0x1F01),
                ("vendor", 0x1F00),
            ):
                value = gl.glGetString(key)
                if not value:
                    raise RuntimeError(f"isolated WGL returned no {name}")
                details[name] = value.decode("utf-8", errors="replace")
            version = tuple(
                int(part) for part in details["version"].split()[0].split(".")[:2]
            )
            if version < (3, 2) or "llvmpipe" not in details["renderer"].lower():
                raise RuntimeError(
                    f"isolated WGL needs llvmpipe OpenGL 3.2+: {details}"
                )
            gl.glViewport(0, 0, 32, 32)
            gl.glClearColor(0.25, 0.5, 0.75, 1.0)
            gl.glClear(0x4000)
            gl.glReadBuffer(0x0405)
            gl.glFinish()
            pixel = (ctypes.c_ubyte * 4)()
            gl.glReadPixels(0, 0, 1, 1, 0x1908, 0x1401, pixel)
            error = gl.glGetError()
            if error or any(
                abs(actual - expected) > 2
                for actual, expected in zip(pixel, (64, 128, 191, 255))
            ):
                raise RuntimeError(
                    f"isolated WGL pixel proof failed: error={error}, pixel={list(pixel)}"
                )
            return {**details, "pixel": list(pixel), "dll": str(path)}
        finally:
            if active:
                _check(gl.wglMakeCurrent(None, None))
            if context:
                _check(gl.wglDeleteContext(context))
            _check(user.ReleaseDC(window, dc))


def wait_parent_release(proof, ready_path, release_path):
    ready, release = Path(ready_path), Path(release_path)
    home = Path.cwd().resolve()
    pending = ready.with_suffix(".tmp")
    if (
        ready.resolve().parent != home
        or release.resolve().parent != home
        or ready.resolve() == release.resolve()
        or pending.resolve() == release.resolve()
        or any(path.exists() or path.is_symlink() for path in (ready, release, pending))
    ):
        raise ValueError(
            "native handoff markers must be unused files in the child-owned working directory"
        )
    with pending.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(proof) + "\n")
    pending.replace(ready)
    deadline = time.monotonic() + 15
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "parent did not release its backend handles within 15 seconds"
            )
        time.sleep(0.01)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", action="store_true")
    parser.add_argument("--wgl", metavar="MESA_DLL")
    parser.add_argument("--ready-file", metavar="PATH")
    parser.add_argument("--release-file", metavar="PATH")
    args = parser.parse_args()
    if bool(args.ready_file) != bool(args.release_file):
        parser.error("--ready-file and --release-file must be supplied together")
    backend = current_backend()
    proof = dict(backend)
    if args.window or args.wgl or args.ready_file:
        user, _ = _api()
        user.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_void_p,
        ]
        user.CreateWindowExW.restype = wintypes.HANDLE
        user.DestroyWindow.argtypes = [wintypes.HANDLE]
        user.DestroyWindow.restype = wintypes.BOOL
        window = _check(
            user.CreateWindowExW(
                0,
                "STATIC",
                "Blockfield isolated probe",
                0x80000000,
                0,
                0,
                32,
                32,
                None,
                None,
                None,
                None,
            )
        )
        try:
            proof["window_created"] = True
            if args.wgl:
                proof["gl"] = render_wgl(user, window, args.wgl)
            if current_backend() != backend:
                raise RuntimeError("isolated backend changed while creating its window")
            if args.ready_file:
                wait_parent_release(proof, args.ready_file, args.release_file)
                user.IsWindow.argtypes = [wintypes.HANDLE]
                user.IsWindow.restype = wintypes.BOOL
                _check(user.IsWindow(window))
                if current_backend() != backend:
                    raise RuntimeError(
                        "isolated backend changed after parent handles closed"
                    )
                proof["window_survived_parent_close"] = True
        finally:
            _check(user.DestroyWindow(window))
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
