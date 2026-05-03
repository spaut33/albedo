"""Compose the Rich Layout for the orchestrator TUI.

Two modes:

* **overview** — full dashboard: header, workers table, events + linear
  queue side-by-side, footer.
* **drill-down** — same header, smaller workers table, expanded panel with
  per-agent details (stream counters, recent tools, thinking preview)
  taking the place of events+queue.

The layout is rebuilt from scratch on every refresh tick. Building Rich
Layout objects is cheap (no real I/O), and rebuilding sidesteps subtle
state issues with reusing nested Layouts across resizes.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.layout import Layout

from albedo.tui import widgets
from albedo.tui.history import HistoryStore
from albedo.tui.snapshot import AggregatedSnapshot

NARROW_WIDTH_THRESHOLD = 100
SHORT_HEIGHT_THRESHOLD = 24
WORKER_ROWS_BUDGET_OVERVIEW = 6
WORKER_ROWS_BUDGET_DRILLDOWN = 4
TOKENS_PANEL_HEIGHT = 7


def build_layout(
    snapshot: AggregatedSnapshot,
    *,
    width: int,
    height: int,
    focused_agent: str | None,
    paused: bool,
    show_warnings: bool = False,
    session_summary: str = '',
    daily_summary: str = '',
    warnings: str = '',
    history: HistoryStore | None = None,
    token_cap: int = 0,
    refresh_ms: int = 250,
    session_cost_usd: float = 0.0,
    daily_cost_usd: float = 0.0,
    shutdown_in_progress: bool = False,
) -> RenderableType:
    is_drilldown = (
        focused_agent is not None
        and widgets.find_view(snapshot, focused_agent) is not None
    )

    narrow = width < NARROW_WIDTH_THRESHOLD
    short = height < SHORT_HEIGHT_THRESHOLD

    workers_budget = (
        WORKER_ROWS_BUDGET_DRILLDOWN if is_drilldown else WORKER_ROWS_BUDGET_OVERVIEW
    )
    if short:
        workers_budget = max(2, workers_budget - 2)

    header = widgets.render_header(
        snapshot,
        session_summary=session_summary,
        daily_summary=daily_summary,
        warnings=warnings,
        paused=paused,
        refresh_ms=refresh_ms,
    )

    activity_map = (
        {agent_id: list(buf) for agent_id, buf in history.worker_activity.items()}
        if history is not None
        else None
    )

    workers_panel = widgets.render_workers(
        snapshot,
        focused_agent=focused_agent,
        max_rows=workers_budget,
        show_action=not is_drilldown and not narrow,
        animated=not paused,
        activity=activity_map,
    )

    footer_text = widgets.render_footer(focused_agent=focused_agent)
    if paused:
        footer_text = _append_text(footer_text, '   [paused]')

    if shutdown_in_progress:
        return widgets.render_shutdown_overlay()

    layout = Layout()
    sections: list[Layout] = [
        Layout(header, name='header', size=_header_size(warnings, session_summary)),
        Layout(workers_panel, name='workers', size=workers_budget + 2),
    ]

    if is_drilldown:
        view = widgets.find_view(snapshot, focused_agent)
        # find_view checked above, but keep narrowing explicit for type safety.
        assert view is not None
        sections.append(Layout(widgets.render_drilldown(view), name='detail'))
    else:
        if show_warnings:
            events_source = [
                ev
                for ev in snapshot.events
                if ev.level.lower() in {'warning', 'error', 'critical'}
            ]
        else:
            events_source = snapshot.events
        events_panel = widgets.render_events(
            events_source,
            max_rows=max(3, height // 4),
            title='warnings' if show_warnings else 'events',
            base_url=widgets.linear_base_url(snapshot),
        )
        claims_history = list(history.queue_claims) if history is not None else None
        done_history = list(history.queue_done) if history is not None else None
        rate_history = list(history.token_rate) if history is not None else None
        queue_panel = widgets.render_queue(
            snapshot.linear_queue,
            claims_history=claims_history,
            done_history=done_history,
        )
        daily_tokens = history.daily_tokens if history is not None else 0
        tokens_panel = widgets.render_tokens(
            daily_tokens=daily_tokens,
            cap_tokens=token_cap,
            input_tokens=_sum_field(snapshot, 'input_tokens'),
            output_tokens=_sum_field(snapshot, 'output_tokens'),
            cache_tokens=_sum_field(snapshot, 'cache_read_tokens')
            + _sum_field(snapshot, 'cache_creation_tokens'),
            rate_history=rate_history,
            session_cost_usd=session_cost_usd,
            daily_cost_usd=daily_cost_usd,
        )
        if narrow:
            sections.append(Layout(events_panel, name='events'))
            sections.append(Layout(queue_panel, name='queue'))
            sections.append(
                Layout(tokens_panel, name='tokens', size=TOKENS_PANEL_HEIGHT)
            )
        else:
            mid = Layout(name='mid')
            right = Layout(name='right')
            right.split_column(
                Layout(queue_panel, name='queue'),
                Layout(tokens_panel, name='tokens', size=TOKENS_PANEL_HEIGHT),
            )
            mid.split_row(
                Layout(events_panel, name='events', ratio=3),
                Layout(right, ratio=1, minimum_size=44),
            )
            sections.append(mid)

    sections.append(Layout(footer_text, name='footer', size=1))
    layout.split_column(*sections)
    return layout


def _header_size(warnings: str, session: str) -> int:
    # Title row + summary row (if any) + warnings (if any) + 2 lines panel chrome.
    rows = 1
    if session:
        rows += 1
    if warnings:
        rows += 1
    return rows + 2


def _sum_field(snapshot: AggregatedSnapshot, field: str) -> int:
    """Sum a StreamSummary integer field across all workers' current status."""
    total = 0
    for view in snapshot.workers:
        if view.status is None:
            continue
        total += int(getattr(view.status.stream, field, 0) or 0)
    return total


def _append_text(rendered: RenderableType, suffix: str) -> RenderableType:
    """Best-effort append for footer hint. Falls back to original on type miss."""
    from rich.text import Text

    if isinstance(rendered, Text):
        copy = rendered.copy()
        copy.append(suffix, style='bold yellow')
        return copy
    return rendered
