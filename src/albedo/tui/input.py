"""Non-blocking single-key reader for the TUI hotkey loop.

Uses POSIX `termios` to put the controlling tty into cbreak mode (no line
buffering, no echo) and `select` to poll without blocking. Multi-byte
sequences (Esc, arrow keys) are decoded best-effort: the only escape we
care about is `Esc` itself, used to leave drill-down mode.

The `KeyReader` is a context manager so the original tty state is always
restored even on exceptions.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

KEY_ESCAPE = '\x1b'
KEY_BACKSPACE = '\x7f'
KEY_ENTER = '\r'
KEY_CTRL_C = '\x03'
KEY_CTRL_K = '\x0b'


class KeyReader(AbstractContextManager['KeyReader']):
    """Read single keystrokes from stdin in cbreak mode without blocking."""

    def __init__(self, fd: int | None = None) -> None:
        self._fd = fd if fd is not None else sys.stdin.fileno()
        self._original: list[Any] | None = None
        self._enabled = False

    def __enter__(self) -> KeyReader:
        if not os.isatty(self._fd):
            return self
        self._original = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._enabled = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        if self._original is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._original)
        self._enabled = False

    def poll(self, *, timeout: float = 0.0) -> list[str]:
        """Return all keystrokes available now (non-blocking)."""
        if not self._enabled:
            return []
        keys: list[str] = []
        # Use a single select with the requested timeout to avoid burning CPU
        # in callers that pass timeout=0; subsequent reads use 0 timeout to
        # drain the buffer.
        first = True
        while True:
            ready, _, _ = select.select([self._fd], [], [], timeout if first else 0.0)
            first = False
            if not ready:
                break
            chunk = os.read(self._fd, 32).decode('utf-8', errors='replace')
            if not chunk:
                break
            keys.extend(split_chunk(chunk))
        return keys


def split_chunk(chunk: str) -> Iterable[str]:
    """Split a read chunk into discrete keystrokes.

    Escape sequences are treated as a single token so callers can dispatch
    on `'\\x1b'` for Esc without picking up a partial arrow-key sequence.
    """
    i = 0
    while i < len(chunk):
        ch = chunk[i]
        if ch == KEY_ESCAPE:
            # An ESC followed by '[' or 'O' is a CSI/SS3 sequence.
            if i + 1 < len(chunk) and chunk[i + 1] in '[O':
                # Read until a letter (the final byte of CSI).
                j = i + 2
                while j < len(chunk) and not chunk[j].isalpha() and chunk[j] != '~':
                    j += 1
                yield chunk[i : j + 1]
                i = j + 1
                continue
            yield ch
            i += 1
            continue
        yield ch
        i += 1
