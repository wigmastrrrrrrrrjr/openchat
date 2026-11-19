"""Double-Esc interrupt for long-running turns."""

import os
import re
import select
import sys
import threading
import time
from contextlib import contextmanager


class Interrupted(BaseException):
    """Raised when the user presses Esc twice."""

    def __init__(self, partial=""):
        super().__init__("interrupted")
        self.partial = partial


class _Watcher:
    def __init__(self):
        self.event = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._fd = None
        self._saved = None
        self.active = False

    def start(self):
        if self.active or not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._fd = None
            return False
        self.event.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.active = True
        return True

    def _run(self):
        last = 0.0
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except Exception:
                return
            if not ready:
                continue
            try:
                data = os.read(self._fd, 8)
            except OSError:
                return
            # Ignore arrow keys and other CSI/SS3 sequences.
            plain = re.sub(rb"\x1b[\[O][0-9;]*[A-Za-z~]", b"", data)
            escs = plain.count(b"\x1b")
            if escs:
                now = time.monotonic()
                if escs >= 2 or (now - last <= 0.8):
                    self.event.set()
                    return
                last = now

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.4)
            self._thread = None
        if self._fd is not None and self._saved is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
        self._fd = None
        self._saved = None
        self.active = False


_watcher = _Watcher()


@contextmanager
def watch():
    _watcher.start()
    try:
        yield
    finally:
        _watcher.stop()


def pause():
    """Stop watching so another prompt can own stdin. Returns prior state."""
    was_active = _watcher.active
    if was_active:
        _watcher.stop()
    return was_active


def resume(was_active):
    if was_active:
        _watcher.start()


def check():
    if _watcher.active and _watcher.event.is_set():
        raise Interrupted()
