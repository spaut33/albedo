"""Smoke tests for TUI rendering — focus on data → string projection.

We render to a Rich Console with capture and assert visible substrings to
keep the tests robust against minor Rich layout changes (line breaks,
spacing).
"""

from __future__ import annotations

import re
import time

from rich.console import Console

from albedo.status_writer import (
    PHASE_POLLING,
    PHASE_SPAWNING_CLAUDE,
    IssueRef,
    StreamSummary,
    WorkerStatus,
)
from albedo.tui import widgets
from albedo.tui.layout import build_layout
from albedo.tui.snapshot import (
    AggregatedSnapshot,
    LinearQueueSnapshot,
    LogEvent,
    WorkerView,
)


def _capture(renderable: object, width: int = 120) -> str:
    console = Console(width=width, force_terminal=True, color_system=None)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _capture_with_links(renderable: object, width: int = 120) -> str:
    """Capture output with full ANSI/OSC 8 escapes preserved.

    Rich emits link escapes only when the console is a real terminal; the
    default `_capture` helper mirrors a colour-stripped pipe and is too
    lossy for hyperlink assertions.
    """
    console = Console(
        width=width,
        force_terminal=True,
        color_system='truecolor',
        legacy_windows=False,
    )
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _make_snapshot(
    workers: list[WorkerView],
    events: list[LogEvent] | None = None,
) -> AggregatedSnapshot:
    return AggregatedSnapshot(
        workers=workers,
        events=events or [],
        linear_queue=LinearQueueSnapshot(counts={'Backlog': 2, 'Review': 1}),
        project_name='sample',
        now=time.time(),
    )


def _make_worker(
    agent_id: str,
    *,
    role: str = 'CODER',
    issue: str | None = 'LIN-1',
    phase: str = PHASE_SPAWNING_CLAUDE,
    last_tool: tuple[str, str] | None = ('Edit', 'src/foo.py'),
    stale: bool = False,
    issue_url: str = '',
) -> WorkerView:
    if issue is None:
        return WorkerView(
            agent_id=agent_id,
            status=WorkerStatus(
                agent_id=agent_id,
                updated_at=time.time(),
                phase=PHASE_POLLING,
                phase_started_at=time.time() - 5,
            ),
            heartbeat_age_seconds=2.0,
            is_stale=False,
        )
    stream = StreamSummary()
    if last_tool is not None:
        stream.last_tool_name = last_tool[0]
        stream.last_tool_target = last_tool[1]
        stream.last_tool_ts = time.time()
        stream.tool_use_count = 5
        stream.turns = 3
    status = WorkerStatus(
        agent_id=agent_id,
        updated_at=time.time(),
        phase=phase,
        phase_started_at=time.time() - 60,
        issue=IssueRef(id='id', identifier=issue, title='do thing', url=issue_url),
        role=role,
        stream=stream,
    )
    return WorkerView(
        agent_id=agent_id,
        status=status,
        heartbeat_age_seconds=3.0,
        is_stale=stale,
    )


def test_workers_panel_lists_each_worker_with_role_and_issue() -> None:
    snap = _make_snapshot(
        [_make_worker('1'), _make_worker('2', role='REVIEWER', issue='LIN-2')]
    )
    text = _capture(widgets.render_workers(snap))
    assert 'CODER' in text
    assert 'REVIEWER' in text
    assert 'LIN-1' in text
    assert 'LIN-2' in text
    assert 'src/foo.py' in text


def test_workers_panel_shows_display_name_when_set() -> None:
    view = _make_worker('1')
    assert view.status is not None
    view.status.display_name = 'alice'
    snap = _make_snapshot([view])
    text = _capture(widgets.render_workers(snap))
    assert 'alice' in text


def test_workers_panel_falls_back_to_agent_dash_id_without_display_name() -> None:
    view = _make_worker('7')
    assert view.status is not None
    view.status.display_name = ''
    snap = _make_snapshot([view])
    text = _capture(widgets.render_workers(snap))
    assert 'agent-7' in text


def test_workers_panel_idle_row_shows_dash() -> None:
    snap = _make_snapshot([_make_worker('1', issue=None)])
    text = _capture(widgets.render_workers(snap))
    assert '—' in text


def test_workers_panel_overflow_indicator() -> None:
    workers = [_make_worker(str(n), issue=f'LIN-{n}') for n in range(1, 6)]
    snap = _make_snapshot(workers)
    text = _capture(widgets.render_workers(snap, max_rows=3))
    assert '+2' in text
    assert 'more' in text


def test_events_panel_renders_recent_messages() -> None:
    events = [
        LogEvent(ts=time.time(), agent='1', level='info', message='claimed LIN-1'),
        LogEvent(ts=time.time(), agent='2', level='warning', message='backoff'),
    ]
    snap = _make_snapshot([_make_worker('1')], events=events)
    text = _capture(widgets.render_events(snap.events))
    assert 'claimed LIN-1' in text
    assert 'backoff' in text


def test_queue_panel_lists_columns() -> None:
    text = _capture(widgets.render_queue(LinearQueueSnapshot(counts={'Backlog': 7})))
    assert 'Backlog' in text
    assert '7' in text


def test_sort_states_orders_known_states_first() -> None:
    counts = {
        'Done': 4,
        'Backlog': 1,
        'Triage': 2,
        'In Progress': 3,
        'Awaiting approval': 0,
        'Review': 5,
    }
    ordered = widgets._sort_states(counts)
    assert [name for name, _ in ordered] == [
        'Triage',
        'Backlog',
        'In Progress',
        'Review',
        'Awaiting approval',
        'Done',
    ]


def test_sort_states_pushes_unknown_states_to_bottom_alphabetically() -> None:
    counts = {'Custom Z': 1, 'Custom A': 2, 'Backlog': 3, 'Done': 4}
    ordered = widgets._sort_states(counts)
    assert [name for name, _ in ordered] == [
        'Backlog',
        'Done',
        'Custom A',
        'Custom Z',
    ]


def test_queue_panel_renders_full_workflow_in_order() -> None:
    counts = {'Done': 1, 'Triage': 2, 'In Progress': 3, 'Backlog': 4}
    text = _capture(widgets.render_queue(LinearQueueSnapshot(counts=counts)))
    triage_idx = text.index('Triage')
    backlog_idx = text.index('Backlog')
    inprog_idx = text.index('In Progress')
    done_idx = text.index('Done')
    assert triage_idx < backlog_idx < inprog_idx < done_idx


def test_drilldown_shows_issue_role_and_recent_tools() -> None:
    view = _make_worker('1', role='CODER', issue='LIN-42')
    assert view.status is not None
    view.status.stream.recent_tools = [
        {'name': 'Read', 'target': 'a.py', 'ts': time.time() - 10},
        {'name': 'Edit', 'target': 'b.py', 'ts': time.time() - 5},
    ]
    text = _capture(widgets.render_drilldown(view))
    assert 'agent-1' in text
    assert 'LIN-42' in text
    assert 'CODER' in text
    assert 'recent tool calls' in text
    assert 'a.py' in text and 'b.py' in text


def test_layout_overview_includes_header_workers_and_footer() -> None:
    snap = _make_snapshot([_make_worker('1')])
    layout = build_layout(snap, width=120, height=40, focused_agent=None, paused=False)
    text = _capture(layout)
    assert 'albedo' in text
    assert 'workers' in text
    assert 'agent' in text or '1' in text


def test_layout_drilldown_replaces_events_with_detail() -> None:
    snap = _make_snapshot([_make_worker('1', role='CODER', issue='LIN-100')])
    layout = build_layout(snap, width=120, height=40, focused_agent='1', paused=False)
    text = _capture(layout)
    assert 'LIN-100' in text
    assert 'recent tool' in text or 'phase' in text


def test_layout_narrow_terminal_drops_queue() -> None:
    snap = _make_snapshot(
        [_make_worker('1')],
        events=[LogEvent(ts=time.time(), agent='1', level='info', message='claimed')],
    )
    text = _capture(
        build_layout(snap, width=80, height=20, focused_agent=None, paused=False),
        width=80,
    )
    # Queue counts ('Backlog 2') should not appear in narrow mode.
    assert 'Backlog' not in text or 'linear queue' not in text


def test_workers_panel_overflow_with_many_workers() -> None:
    workers = [_make_worker(str(n), issue=f'LIN-{n}') for n in range(1, 11)]
    snap = _make_snapshot(workers)
    text = _capture(widgets.render_workers(snap, max_rows=6))
    assert '+4' in text
    assert 'more' in text


def test_layout_handles_single_worker() -> None:
    snap = _make_snapshot([_make_worker('1')])
    text = _capture(
        build_layout(snap, width=120, height=40, focused_agent=None, paused=False)
    )
    # No "+N more" indicator when everything fits.
    assert 'more' not in text or text.count('more') <= 1


def test_paused_appended_to_footer() -> None:
    snap = _make_snapshot([_make_worker('1')])
    text = _capture(
        build_layout(snap, width=120, height=40, focused_agent=None, paused=True)
    )
    assert 'paused' in text


def test_warnings_banner_appears_when_provided() -> None:
    snap = _make_snapshot([_make_worker('1')])
    text = _capture(
        build_layout(
            snap,
            width=120,
            height=40,
            focused_agent=None,
            paused=False,
            warnings='⚠ 1 stale worker',
        )
    )
    assert 'stale worker' in text


def test_workers_panel_wraps_issue_in_osc8_when_url_present() -> None:
    url = 'https://linear.app/acme/issue/LIN-9/do-thing'
    snap = _make_snapshot([_make_worker('1', issue='LIN-9', issue_url=url)])
    raw = _capture_with_links(widgets.render_workers(snap))
    # Rich emits OSC 8 as `ESC ] 8 ; <params> ; <url> ESC \`. The params
    # segment is implementation detail (Rich injects an `id=NNN`); we just
    # require the URL to appear inside the OSC 8 envelope before the label.
    assert '\x1b]8;' in raw
    assert url in raw
    assert raw.index(url) < raw.index('LIN-9')


def test_workers_panel_omits_osc8_when_url_missing() -> None:
    snap = _make_snapshot([_make_worker('1', issue='LIN-9', issue_url='')])
    raw = _capture_with_links(widgets.render_workers(snap))
    assert '\x1b]8;' not in raw
    assert 'LIN-9' in raw


def test_workers_panel_link_does_not_extend_to_column_padding() -> None:
    """Underline/link must stop at the identifier — not bleed into right-pad
    whitespace as Rich would do if we set the link as the Text base style."""
    url = 'https://linear.app/acme/issue/LIN-9/do-thing'
    snap = _make_snapshot([_make_worker('1', issue='LIN-9', issue_url=url)])
    raw = _capture_with_links(widgets.render_workers(snap))
    # Inside the OSC 8 envelope (between `\x1b]8;...;URL\x1b\\` and the
    # closing `\x1b]8;;\x1b\\`) we want only the identifier itself, no
    # trailing spaces.
    open_marker = '\x1b\\'
    close_marker = '\x1b]8;;\x1b\\'
    open_idx = raw.index(url) + len(url) + len(open_marker)
    close_idx = raw.index(close_marker, open_idx)
    linked_text = raw[open_idx:close_idx]
    # Strip ANSI SGR codes so we can inspect just the visible payload.
    visible = re.sub(r'\x1b\[[0-9;]*m', '', linked_text)
    assert visible == 'LIN-9'


def test_drilldown_wraps_issue_in_osc8_when_url_present() -> None:
    url = 'https://linear.app/acme/issue/LIN-42/do-thing'
    view = _make_worker('1', role='CODER', issue='LIN-42', issue_url=url)
    raw = _capture_with_links(widgets.render_drilldown(view))
    assert '\x1b]8;' in raw
    assert url in raw
    assert raw.index(url) < raw.index('LIN-42')


def test_events_panel_linkifies_identifiers_when_base_url_known() -> None:
    events = [
        LogEvent(ts=time.time(), agent='1', level='info', message='claimed AI-48'),
        LogEvent(
            ts=time.time(), agent='1', level='info', message='AI-48 -> In Progress'
        ),
    ]
    raw = _capture_with_links(
        widgets.render_events(events, base_url='https://linear.app/acme')
    )
    assert 'https://linear.app/acme/issue/AI-48' in raw
    assert raw.count('\x1b]8;') >= 2  # two OSC 8 opens, one per occurrence


def test_events_panel_renders_plainly_without_base_url() -> None:
    events = [
        LogEvent(ts=time.time(), agent='1', level='info', message='claimed AI-48'),
    ]
    raw = _capture_with_links(widgets.render_events(events, base_url=''))
    assert '\x1b]8;' not in raw
    assert 'AI-48' in raw


def test_linear_base_url_is_derived_from_first_known_issue_url() -> None:
    snap = _make_snapshot(
        [
            _make_worker('1', issue='AI-1', issue_url=''),
            _make_worker(
                '2', issue='AI-48', issue_url='https://linear.app/acme/issue/AI-48/foo'
            ),
        ]
    )
    assert widgets.linear_base_url(snap) == 'https://linear.app/acme'


def test_linear_base_url_empty_when_no_worker_has_url() -> None:
    snap = _make_snapshot([_make_worker('1', issue='AI-1', issue_url='')])
    assert widgets.linear_base_url(snap) == ''


def test_show_warnings_filters_event_panel_to_errors() -> None:
    events = [
        LogEvent(ts=time.time(), agent='1', level='info', message='polling'),
        LogEvent(ts=time.time(), agent='2', level='error', message='boom'),
    ]
    snap = _make_snapshot([_make_worker('1')], events=events)
    plain = _capture(
        build_layout(
            snap,
            width=120,
            height=40,
            focused_agent=None,
            paused=False,
            show_warnings=False,
        )
    )
    filtered = _capture(
        build_layout(
            snap,
            width=120,
            height=40,
            focused_agent=None,
            paused=False,
            show_warnings=True,
        )
    )
    assert 'polling' in plain
    assert 'polling' not in filtered
    assert 'boom' in filtered
    assert 'warnings' in filtered
