"""Main Rich Live loop driving the orchestrator TUI.

Runs in the supervisor process. Reads worker state via filesystem
(see `snapshot.aggregate`) at 4 Hz, handles single-key hotkeys via
`KeyReader`, and re-renders the layout each tick. Exits cleanly on `q`,
Ctrl+C, or when the supplied `should_stop` callable returns True
(used by the supervisor to tear down on SIGTERM).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.live import Live

from albedo.tui import widgets
from albedo.tui.history import HistoryStore
from albedo.tui.input import KEY_CTRL_K, KEY_ESCAPE, KeyReader
from albedo.tui.layout import build_layout
from albedo.tui.snapshot import LogTailer, aggregate

log = logging.getLogger(__name__)

REFRESH_HZ = 4
REFRESH_INTERVAL = 1.0 / REFRESH_HZ
REFRESH_MS = int(REFRESH_INTERVAL * 1000)
STATS_QUERY_INTERVAL_SECONDS = 30


@dataclass(slots=True)
class TuiState:
    focused_agent: str | None = None
    paused: bool = False
    show_warnings: bool = False
    search_filter: str = ''
    quit_requested: bool = False


@dataclass(slots=True)
class TuiContext:
    state_dir: Path
    project_name: str
    expected_workers: int
    log_dir: Path
    session_summary_provider: Callable[[], str] | None = None
    daily_summary_provider: Callable[[], str] | None = None
    session_cost_provider: Callable[[], float] | None = None
    daily_cost_provider: Callable[[], float] | None = None
    warnings_provider: Callable[[], str] | None = None
    shutdown_in_progress: Callable[[], bool] | None = None
    terminate_worker: Callable[[str], bool] | None = None
    should_stop: Callable[[], bool] = field(default=lambda: False)
    token_cap: int = 0
    ledger: object | None = None


class TuiApp:
    def __init__(self, ctx: TuiContext) -> None:
        self._ctx = ctx
        self._state = TuiState()
        self._tailer = LogTailer(ctx.log_dir)
        self._console = Console()
        self._history = HistoryStore()
        # Workspace URL is constant for the session; cache the first one
        # we see so event-log links keep rendering after every worker
        # returns to polling and `linear_base_url` no longer finds a URL
        # on any live worker status.
        self._linear_base_url = ''

    def run(self) -> None:
        ctx = self._ctx
        # `transient=True` clears the alt screen on exit so the terminal is
        # restored to its scrollback state. `screen=True` puts us in the
        # alternate screen buffer for the duration of the run.
        with (
            Live(
                console=self._console,
                screen=True,
                refresh_per_second=REFRESH_HZ,
                transient=True,
            ) as live,
            KeyReader() as keys,
        ):
            while not self._state.quit_requested and not ctx.should_stop():
                self.handle_keys(keys.poll(timeout=REFRESH_INTERVAL))
                snapshot = aggregate(
                    state_dir=ctx.state_dir,
                    log_tailer=self._tailer,
                    expected_workers=ctx.expected_workers,
                    project_name=ctx.project_name,
                )
                self._history.tick(snapshot, ctx.ledger)  # type: ignore[arg-type]
                self._linear_base_url = widgets.linear_base_url(
                    snapshot, fallback=self._linear_base_url
                )
                size = self._console.size
                shutdown = _call_bool(ctx.shutdown_in_progress)
                renderable = build_layout(
                    snapshot,
                    width=size.width,
                    height=size.height,
                    focused_agent=self._state.focused_agent,
                    paused=self._state.paused,
                    show_warnings=self._state.show_warnings,
                    session_summary=_call(ctx.session_summary_provider),
                    daily_summary=_call(ctx.daily_summary_provider),
                    session_cost_usd=_call_float(ctx.session_cost_provider),
                    daily_cost_usd=_call_float(ctx.daily_cost_provider),
                    warnings=_call(ctx.warnings_provider),
                    history=self._history,
                    token_cap=ctx.token_cap,
                    refresh_ms=REFRESH_MS,
                    shutdown_in_progress=shutdown,
                    linear_base_url=self._linear_base_url,
                )
                live.update(renderable)

    def state(self) -> TuiState:
        """Expose the current UI state — handy for tests and remote control."""
        return self._state

    def handle_keys(self, keystrokes: list[str]) -> None:
        """Apply a batch of keystrokes to the state machine.

        Public so tests can drive the state machine without spinning up a
        real Live render loop, and so a future remote-control transport
        can replay events.

        Quit is intentionally NOT bound to a single key — Ctrl+C is the
        only exit, delivered as SIGINT through the supervisor's signal
        handler (cbreak preserves ISIG). Letter keys are easy to hit by
        accident and would tear down running spawns.
        """
        for key in keystrokes:
            if key == KEY_ESCAPE:
                self._state.focused_agent = None
                continue
            if key == KEY_CTRL_K:
                focused = self._state.focused_agent
                terminate = self._ctx.terminate_worker
                if focused is not None and terminate is not None:
                    try:
                        terminate(focused)
                    except Exception as exc:
                        log.warning('TUI terminate_worker failed: %s', exc)
                continue
            if key in {'p', 'P'}:
                self._state.paused = not self._state.paused
                continue
            if key in {'w', 'W'}:
                self._state.show_warnings = not self._state.show_warnings
                continue
            if key.isdigit():
                target = _normalize_focus(key)
                # Pressing the focused agent's digit again returns to the
                # main view, so the digit acts as a toggle.
                if self._state.focused_agent == target:
                    self._state.focused_agent = None
                else:
                    self._state.focused_agent = target
                continue


def _normalize_focus(digit: str) -> str | None:
    # Plain digit selects matching agent id. '0' maps to agent-10
    # so workers 1..10 are reachable on a numeric row.
    return '10' if digit == '0' else digit


def _call(provider: Callable[[], str] | None) -> str:
    if provider is None:
        return ''
    try:
        return provider()
    except Exception as exc:
        log.warning('TUI provider failed: %s', exc)
        return ''


def _call_float(provider: Callable[[], float] | None) -> float:
    if provider is None:
        return 0.0
    try:
        return float(provider())
    except Exception as exc:
        log.warning('TUI provider failed: %s', exc)
        return 0.0


def _call_bool(provider: Callable[[], bool] | None) -> bool:
    if provider is None:
        return False
    try:
        return bool(provider())
    except Exception as exc:
        log.warning('TUI provider failed: %s', exc)
        return False


def run_tui(
    ctx: TuiContext,
    *,
    in_thread: bool = False,
) -> threading.Thread | None:
    """Convenience launcher.

    With `in_thread=True`, runs the app in a daemon thread and returns it
    so the supervisor can join later. Otherwise blocks in the current
    thread until the user quits or `should_stop()` flips.
    """
    app = TuiApp(ctx)
    if in_thread:
        t = threading.Thread(target=app.run, name='tui', daemon=True)
        t.start()
        return t
    app.run()
    return None


def wait_until(predicate: Callable[[], bool], *, timeout: float) -> bool:
    """Test helper kept here so unit tests don't need to import time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False
