"""Tests for hotkey dispatching and the chunk-splitting key reader.

Direct termios behaviour is platform-specific and impractical to fake, so
we cover:

* `split_chunk` — escape-sequence parsing without a real terminal
* `TuiApp.handle_keys` — the user-facing state machine for hotkeys
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from albedo.tui.app import TuiApp, TuiContext
from albedo.tui.input import KEY_CTRL_K, KEY_ESCAPE, split_chunk


def _app(terminate_worker: Callable[[str], bool] | None = None) -> TuiApp:
    ctx = TuiContext(
        state_dir=Path('/tmp'),
        project_name='sample',
        expected_workers=3,
        log_dir=Path('/tmp'),
        terminate_worker=terminate_worker,
    )
    return TuiApp(ctx)


def test_split_chunk_separates_plain_keys() -> None:
    assert list(split_chunk('q1')) == ['q', '1']


def test_split_chunk_escape_alone_is_one_token() -> None:
    assert list(split_chunk(KEY_ESCAPE)) == [KEY_ESCAPE]


def test_split_chunk_csi_sequence_kept_together() -> None:
    seq = KEY_ESCAPE + '[A'
    assert list(split_chunk(seq + 'q')) == [seq, 'q']


def test_q_letter_does_not_quit() -> None:
    """Quit was intentionally unbound — only SIGINT (Ctrl+C) ends the loop."""
    app = _app()
    app.handle_keys(['q', 'Q'])
    assert app.state().quit_requested is False


def test_digit_focuses_agent() -> None:
    app = _app()
    app.handle_keys(['2'])
    assert app.state().focused_agent == '2'


def test_zero_maps_to_agent_10() -> None:
    app = _app()
    app.handle_keys(['0'])
    assert app.state().focused_agent == '10'


def test_escape_clears_focus() -> None:
    app = _app()
    app.handle_keys(['3', KEY_ESCAPE])
    assert app.state().focused_agent is None


def test_pressing_focused_digit_again_returns_to_main() -> None:
    app = _app()
    app.handle_keys(['2'])
    assert app.state().focused_agent == '2'
    app.handle_keys(['2'])
    assert app.state().focused_agent is None


def test_pressing_zero_twice_toggles_agent_10() -> None:
    app = _app()
    app.handle_keys(['0'])
    assert app.state().focused_agent == '10'
    app.handle_keys(['0'])
    assert app.state().focused_agent is None


def test_different_digit_switches_focus_without_toggling() -> None:
    app = _app()
    app.handle_keys(['2', '3'])
    assert app.state().focused_agent == '3'


def test_p_toggles_paused() -> None:
    app = _app()
    app.handle_keys(['p'])
    assert app.state().paused is True
    app.handle_keys(['P'])
    assert app.state().paused is False


def test_w_toggles_warnings() -> None:
    app = _app()
    app.handle_keys(['w'])
    assert app.state().show_warnings is True
    app.handle_keys(['w'])
    assert app.state().show_warnings is False


def test_ctrl_k_with_focused_agent_invokes_callback() -> None:
    calls: list[str] = []

    def fake_terminate(agent_id: str) -> bool:
        calls.append(agent_id)
        return True

    app = _app(terminate_worker=fake_terminate)
    app.handle_keys(['2', KEY_CTRL_K])
    assert calls == ['2']


def test_ctrl_k_without_focus_is_noop() -> None:
    calls: list[str] = []

    def fake_terminate(agent_id: str) -> bool:
        calls.append(agent_id)
        return True

    app = _app(terminate_worker=fake_terminate)
    app.handle_keys([KEY_CTRL_K])
    assert calls == []
    assert app.state().focused_agent is None


def test_ctrl_k_without_terminate_callback_is_silent() -> None:
    """If TuiContext is built without terminate_worker, Ctrl+K must not raise."""
    app = _app(terminate_worker=None)
    app.handle_keys(['1', KEY_CTRL_K])
    assert app.state().focused_agent == '1'


def test_bare_k_does_not_kill() -> None:
    calls: list[str] = []

    def fake_terminate(agent_id: str) -> bool:
        calls.append(agent_id)
        return True

    app = _app(terminate_worker=fake_terminate)
    app.handle_keys(['1', 'k', 'K'])
    assert calls == []


def test_ctrl_k_swallows_callback_exception() -> None:
    def boom(_agent_id: str) -> bool:
        raise RuntimeError('child gone')

    app = _app(terminate_worker=boom)
    app.handle_keys(['2', KEY_CTRL_K])
    # Reaching here means the exception was logged, not propagated.
    assert app.state().focused_agent == '2'


def test_ctrl_c_byte_is_ignored_at_app_level() -> None:
    """In cbreak mode the OS turns Ctrl+C into SIGINT before it ever reaches
    the app, so the raw \\x03 byte (if it does sneak through) is a no-op."""
    app = _app()
    app.handle_keys(['\x03'])
    assert app.state().quit_requested is False
