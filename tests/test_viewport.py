"""A statusline server uses pipes and may have a separate controlling terminal."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ViewportTests(unittest.TestCase):
    def test_detached_process_ignores_stale_columns(self):
        options = ({"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt"
                   else {"start_new_session": True})
        result = subprocess.run(
            [sys.executable, "-c", "from viewport import terminal_columns; print(terminal_columns())"],
            cwd=ROOT, env={**os.environ, "COLUMNS": "999"}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5, check=True, **options,
        )
        self.assertEqual(result.stdout.strip(), "None")

    @unittest.skipUnless(os.name == "posix", "Uses a POSIX pseudo-terminal")
    def test_resize_while_stdio_is_piped(self):
        import pty

        master, slave = pty.openpty()
        try:
            # This child owns the PTY but speaks to the test over ordinary pipes.
            child = """
import fcntl, os, struct, sys, termios
from viewport import terminal_columns
tty_fd = int(sys.argv[1])
fcntl.ioctl(tty_fd, termios.TIOCSCTTY, 0)
assert not os.isatty(0) and not os.isatty(1)
for cols in (128, 52, 101):
    fcntl.ioctl(tty_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0))
    print(terminal_columns())
"""
            result = subprocess.run(
                [sys.executable, "-c", child, str(slave)], cwd=ROOT, pass_fds=(slave,),
                start_new_session=True, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=5, check=True,
            )
            self.assertEqual(result.stdout.splitlines(), ["128", "52", "101"])
        finally:
            os.close(master)
            os.close(slave)
