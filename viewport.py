"""Measure the terminal attached to this process, including when MCP uses pipes."""

import os


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ConsoleScreenBufferInfo(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.SHORT * 2),
            ("cursor", wintypes.SHORT * 2),
            ("attributes", wintypes.WORD),
            ("window", wintypes.SMALL_RECT),
            ("maximum", wintypes.SHORT * 2),
        ]

    get_console_info = ctypes.WinDLL("kernel32").GetConsoleScreenBufferInfo
    get_console_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ConsoleScreenBufferInfo)]
    get_console_info.restype = wintypes.BOOL


def terminal_columns():
    """Return the current visible column count, or None for a detached provider."""
    try:
        if os.name == "nt":
            # CONOUT$ accesses our attached console even with redirected stdout.
            fd = os.open("CONOUT$", os.O_RDONLY | os.O_BINARY)
            try:
                info = ConsoleScreenBufferInfo()
                if not get_console_info(msvcrt.get_osfhandle(fd), ctypes.byref(info)):
                    return None
                columns = info.window.Right - info.window.Left + 1
            finally:
                os.close(fd)
        else:
            # /dev/tty identifies our own controlling terminal. A shared daemon
            # without one gets ENXIO here rather than another client's dimensions.
            with open("/dev/tty", "rb", buffering=0) as tty:
                columns = os.get_terminal_size(tty.fileno()).columns
        return columns if columns > 0 else None
    except OSError:
        return None
