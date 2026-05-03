"""Visual primitives: sparkline, bar_gauge, boxed chrome, render_tokens.

We assert exact glyph composition for the primitives (deterministic
inputs → deterministic output) and visible substrings for the higher-
level renderables, mirroring the pattern in `test_tui_widgets.py`.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from albedo.tui import widgets


def _capture(renderable: object, width: int = 80) -> str:
    console = Console(width=width, force_terminal=True, color_system=None)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# --- sparkline -----------------------------------------------------------


def test_sparkline_empty_input_renders_blanks() -> None:
    text = widgets.sparkline([], width=8)
    assert text.plain == ' ' * 8


def test_sparkline_zero_width_returns_empty() -> None:
    text = widgets.sparkline([1.0, 2.0], width=0)
    assert text.plain == ''


def test_sparkline_picks_block_glyphs_by_magnitude() -> None:
    text = widgets.sparkline([0, 1, 2, 3, 4, 5, 6, 7], width=8, max_value=7)
    # Zero → blank; the ladder is mapped via floor(ratio * 7), so the
    # peak (7/7) lands on '█'. Intermediate values quantise to the next
    # block up the ladder.
    assert text.plain[0] == ' '
    assert text.plain[-1] == '█'
    # All intermediate cells must be one of the SPARK_BLOCKS glyphs.
    for ch in text.plain[1:]:
        assert ch in widgets.SPARK_BLOCKS


def test_sparkline_downsamples_when_input_longer_than_width() -> None:
    text = widgets.sparkline([0, 0, 8, 8], width=2, max_value=8)
    # Two buckets → averages [0, 8] → blank + full block.
    assert text.plain == ' █'


def test_sparkline_zero_peak_is_all_blanks() -> None:
    text = widgets.sparkline([0, 0, 0], width=4)
    assert text.plain == '    '


def test_sparkline_color_override_applies_flat_style_to_every_cell() -> None:
    # The default per-cell gradient maps the rolling peak to red. Passing
    # `color=` short-circuits that so callers rendering shape-over-time
    # series (e.g. raw rate counts) don't get an alarmist colour scheme
    # purely because of auto-normalisation.
    text = widgets.sparkline([1, 2], width=2, color='cyan')
    spans = [(span.start, span.end, span.style) for span in text.spans]
    assert spans == [(0, 1, 'cyan'), (1, 2, 'cyan')]
    assert all(style == 'cyan' for _, _, style in spans)


def test_sparkline_low_activity_with_color_override_has_no_red_cells() -> None:
    # Mirrors the linear-panel claims/min and done/min case from AI-53:
    # values capped at 2 over the window must not render red even though
    # the rolling peak hits the gradient's red threshold.
    text = widgets.sparkline([0, 1, 2, 1, 0, 2, 1, 0], width=8, color='cyan')
    styles = {span.style for span in text.spans}
    assert 'red' not in styles
    assert styles == {'cyan'}


# --- bar_gauge -----------------------------------------------------------


def test_bar_gauge_no_cap_renders_empty_bar_with_label() -> None:
    text = widgets.bar_gauge(0.0, 0.0, width=6, label='no cap')
    assert text.plain == '░░░░░░  no cap'


def test_bar_gauge_full_renders_all_filled_with_pct() -> None:
    text = widgets.bar_gauge(10.0, 10.0, width=4, show_pct=True)
    assert text.plain == '████  100%'


def test_bar_gauge_show_pct_false_omits_percent_suffix() -> None:
    text = widgets.bar_gauge(5.0, 10.0, width=4, show_pct=False)
    # 50% of 4 = 2 full blocks, no half (2.0 - 2 = 0.0 < 0.5), 2 empty.
    assert text.plain == '██░░'


def test_bar_gauge_partial_renders_half_block() -> None:
    text = widgets.bar_gauge(5.0, 8.0, width=4, show_pct=False)
    # 5/8 * 4 = 2.5 → 2 full + 1 half + 1 empty
    assert text.plain == '██▒░'


def test_bar_gauge_caps_value_at_total() -> None:
    text = widgets.bar_gauge(100.0, 10.0, width=3, show_pct=True)
    assert text.plain == '███  100%'


# --- boxed --------------------------------------------------------------


def test_boxed_renders_title_and_body() -> None:
    panel = widgets.boxed(Text('hello'), title='tokens')
    out = _capture(panel)
    assert 'tokens' in out
    assert 'hello' in out


# --- render_tokens ------------------------------------------------------


def test_render_tokens_with_cap_shows_pct_and_humanized_burn() -> None:
    panel = widgets.render_tokens(
        daily_tokens=750_000,
        cap_tokens=2_000_000,
        input_tokens=180_000,
        output_tokens=90_000,
        cache_tokens=120_000,
        rate_history=[100, 200, 800, 1500, 3200],
    )
    out = _capture(panel, width=60)
    assert 'tokens' in out
    assert '37%' in out  # 750k/2M
    assert '750.0k' in out
    assert 'in 180.0k' in out
    assert 'out 90.0k' in out
    assert 'cache 120.0k' in out
    assert 'rate' in out
    assert '/min' in out


def test_render_tokens_without_cap_says_no_cap() -> None:
    panel = widgets.render_tokens(
        daily_tokens=12_000,
        cap_tokens=0,
    )
    out = _capture(panel, width=60)
    assert 'no cap' in out
    assert '12.0k' in out
