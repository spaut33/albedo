"""Rendering helpers for the live TUI.

Each function takes a snapshot (or a slice of it) and returns a Rich
renderable. Pure: no I/O, no monkeypatching of global state. The Layout
in `layout.py` wires them together.

Color palette (semantic, not literal): role badges use distinct hues so
the eye finds the active CODER vs REVIEWER quickly. Phase tinting
distinguishes "running" vs "idle" rows. No emoji.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence

from rich import box
from rich.align import Align
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from albedo.status_writer import (
    PHASE_POLLING,
    PHASE_RUNNING_CLAUDE,
    PHASE_SHUTDOWN,
    PHASE_SPAWNING_CLAUDE,
    PHASE_THROTTLED,
    IssueRef,
    StreamSummary,
)
from albedo.tui.snapshot import (
    AggregatedSnapshot,
    LinearQueueSnapshot,
    LogEvent,
    WorkerView,
    humanize_phase,
)

ACTIVE_CLAUDE_PHASES: frozenset[str] = frozenset(
    {PHASE_SPAWNING_CLAUDE, PHASE_RUNNING_CLAUDE}
)

ROLE_COLORS: dict[str, str] = {
    'CODER': 'yellow',
    'REVIEWER': 'cyan',
    'ARCHITECT': 'magenta',
}

PHASE_COLORS: dict[str, str] = {
    PHASE_SPAWNING_CLAUDE: 'yellow',
    PHASE_RUNNING_CLAUDE: 'green',
    PHASE_POLLING: 'dim',
    PHASE_THROTTLED: 'yellow',
    PHASE_SHUTDOWN: 'red',
}


SPARK_BLOCKS: tuple[str, ...] = ('⣀', '⣄', '⣤', '⣦', '⣶', '⣷', '⣾', '⣿')
SPARK_BASELINE: str = '⠤'
GAUGE_FILLED = '█'
GAUGE_HALF = '▒'
GAUGE_EMPTY = '░'


def _grade_color(ratio: float) -> str:
    """Map 0..1 to a semantic color (green→yellow→red) for gauges/sparklines."""
    if ratio >= 0.85:
        return 'red'
    if ratio >= 0.6:
        return 'yellow'
    return 'green'


def _downsample(values: Sequence[float], target: int) -> list[float]:
    """Average-bucket downsample to `target` samples. Right-aligned (newest last)."""
    if target <= 0:
        return []
    n = len(values)
    if n == 0:
        return [0.0] * target
    if n <= target:
        return [float(v) for v in values]
    bucket = n / target
    out: list[float] = []
    for i in range(target):
        start = int(i * bucket)
        end = int((i + 1) * bucket)
        if end <= start:
            end = start + 1
        chunk = values[start:end]
        out.append(sum(chunk) / len(chunk))
    return out


def sparkline(
    values: Sequence[float],
    *,
    width: int,
    max_value: float | None = None,
    color: str | None = None,
) -> Text:
    """Render a Unicode-block sparkline of `values` into a `width`-wide Text.

    Output is always exactly `width` cells. Shorter inputs are left-padded
    with literal spaces so the newest sample stays anchored to the right
    edge — that pad is the only situation where a space appears in the
    output. `max_value` is the upper bound of the scale; if omitted, it
    is derived from `max(values)`.

    Empty / all-zero input renders entirely as spaces. Once any sample is
    non-zero, every data position renders a glyph: zero buckets pick a
    faint baseline (`SPARK_BASELINE`, styled `dim`) so a sparse series
    reads as a continuous trace rather than scattered specks across blank
    cells.

    By default cells are colored per-sample via `_grade_color(cell/max)` —
    a green→yellow→red gradient that signals proximity to a known cap.
    Pass `color` to force a single flat style instead; use this for series
    that convey shape over time without a meaningful ceiling (e.g. raw
    rate counts), where the gradient would otherwise paint the rolling
    peak red regardless of absolute magnitude. The baseline glyph stays
    `dim` regardless of `color` so it visually recedes against active
    samples.
    """
    if width <= 0:
        return Text('')
    samples = _downsample(values, width)
    pad = width - len(samples)
    text = Text(no_wrap=True)
    if pad > 0:
        text.append(' ' * pad)
    if not samples:
        return text
    peak = max_value if max_value is not None else max(samples)
    if peak <= 0:
        text.append(' ' * len(samples))
        return text
    for sample in samples:
        if sample <= 0:
            text.append(SPARK_BASELINE, style='dim')
            continue
        ratio = min(1.0, sample / peak)
        idx = min(len(SPARK_BLOCKS) - 1, max(0, int(ratio * (len(SPARK_BLOCKS) - 1))))
        style = color if color is not None else _grade_color(ratio)
        text.append(SPARK_BLOCKS[idx], style=style)
    return text


class AutoSparkline:
    """Self-sizing sparkline that fills the column it's rendered into.

    Reads `console.options.max_width` so the caller doesn't need to guess
    a width. Anything from 0 cells upwards is fine — `sparkline` already
    pads/downsamples to fit.
    """

    def __init__(
        self,
        values: Sequence[float],
        *,
        max_value: float | None = None,
        color: str | None = None,
    ) -> None:
        self._values = list(values)
        self._max_value = max_value
        self._color = color

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(0, options.max_width)
        yield sparkline(
            self._values,
            width=width,
            max_value=self._max_value,
            color=self._color,
        )


def bar_gauge(
    value: float,
    total: float,
    *,
    width: int,
    show_pct: bool = True,
    label: str = '',
) -> Text:
    """Horizontal bar gauge.

    Bar fills `width` cells; the percentage suffix and optional `label` are
    appended after two spaces. `total <= 0` renders as a flat empty bar
    (caller is expected to surface "no cap" / "n/a" via `label`). Set
    `show_pct=False` to suppress the percentage when the caller renders it
    in a sibling column.
    """
    width = max(1, width)
    text = Text(no_wrap=True)
    if total <= 0:
        text.append(GAUGE_EMPTY * width, style='dim')
        if label:
            text.append('  ' + label, style='dim')
        return text
    ratio = max(0.0, min(1.0, value / total))
    filled = int(ratio * width)
    half = 1 if (ratio * width - filled) >= 0.5 and filled < width else 0
    color = _grade_color(ratio)
    text.append(GAUGE_FILLED * filled, style=color)
    if half:
        text.append(GAUGE_HALF, style=color)
    remainder = width - filled - half
    if remainder > 0:
        text.append(GAUGE_EMPTY * remainder, style='dim')
    if show_pct:
        text.append(f'  {int(ratio * 100):3d}%', style=color)
    if label:
        text.append(f'  {label}', style='dim')
    return text


def boxed(
    body: RenderableType,
    *,
    title: str,
    border_style: str = 'dim',
) -> Panel:
    """Rounded-border panel with a left-aligned title."""
    return Panel(
        body,
        title=Text(title, style='bold'),
        title_align='left',
        border_style=border_style,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _format_elapsed(seconds: float) -> str:
    if seconds < 0:
        return '—'
    if seconds < 60:
        return f'{int(seconds)}s'
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f'{minutes}m{secs:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m'


def _format_clock(ts: float) -> str:
    if ts <= 0:
        return ''
    return time.strftime('%H:%M:%S', time.localtime(ts))


def _issue_cell(issue: IssueRef | None) -> Text:
    """Render the issue identifier, hyperlinked to Linear when a URL is known.

    The link/underline must NOT extend across the column's right-pad
    whitespace, so we apply the style as an inline span via `Text.assemble`
    (Rich's `Text.style` is the *base* style and bleeds onto padding).
    Rich emits OSC 8 escapes only when the output is a real terminal, and
    terminals that don't recognise OSC 8 strip the sequence — so callers
    don't need to detect terminal capability.
    """
    if issue is None:
        return Text('—', style='dim')
    style = f'white link {issue.url}' if issue.url else 'white'
    return Text.assemble((issue.identifier, style))


def _role_badge(role: str) -> Text:
    if not role:
        return Text('—', style='dim')
    color = ROLE_COLORS.get(role.upper(), 'white')
    return Text(role, style=f'bold {color}')


# Braille spinner frames, same set Rich uses for the 'dots' style. We don't
# rely on rich.spinner.Spinner because Live recreates the renderable each
# tick, so Spinner.start_time resets every render and the animation freezes
# on frame 0. Computing the glyph from wall-clock time is stateless and
# stays in sync no matter how often the layout is rebuilt.
SPINNER_FRAMES: tuple[str, ...] = (
    '⠋',
    '⠙',
    '⠹',
    '⠸',
    '⠼',
    '⠴',
    '⠦',
    '⠧',
    '⠇',
    '⠏',
)
SPINNER_FPS = 10


def _spinner_glyph() -> str:
    return SPINNER_FRAMES[int(time.time() * SPINNER_FPS) % len(SPINNER_FRAMES)]


def _phase_cell(phase: str, *, animated: bool) -> RenderableType:
    label = humanize_phase(phase)
    color = PHASE_COLORS.get(phase, 'white')
    if animated and phase in ACTIVE_CLAUDE_PHASES:
        return Text.assemble(
            (_spinner_glyph() + ' ', color),
            (label, color),
        )
    return Text(label, style=color)


def _action_cell(view: WorkerView, *, animated: bool) -> RenderableType:
    status = view.status
    if status is None or status.issue is None:
        return Text('—', style='dim')
    stream = status.stream
    if stream.last_tool_name:
        target = stream.last_tool_target or ''
        label = (
            f'{stream.last_tool_name}: {target}' if target else stream.last_tool_name
        )
        running = status.phase in ACTIVE_CLAUDE_PHASES
        if animated and running:
            return Text.assemble(
                (_spinner_glyph() + ' ', 'green'),
                (label, 'white'),
            )
        return Text(label, style='white')
    return Text('—', style='dim')


def render_header(
    snapshot: AggregatedSnapshot,
    *,
    session_summary: str = '',
    daily_summary: str = '',
    warnings: str = '',
    paused: bool = False,
    refresh_ms: int = 250,
) -> RenderableType:
    clock = time.strftime('%H:%M:%S', time.localtime(snapshot.now))
    duration_part, counters_part = _split_session_summary(session_summary)

    left = Text()
    left.append('albedo', style='bold')
    left.append(f'  ·  {snapshot.project_name}', style='dim')

    right = Text()
    if paused:
        right.append('paused', style='bold yellow')
        right.append('  ·  ', style='dim')
    if duration_part:
        right.append(duration_part, style='white')
        right.append('  ·  ', style='dim')
    right.append(f'{refresh_ms}ms', style='dim')
    right.append('  ·  ', style='dim')
    right.append(clock, style='dim')

    title_row = Table.grid(expand=True)
    title_row.add_column(ratio=1)
    title_row.add_column(no_wrap=True, justify='right')
    title_row.add_row(left, right)

    summary = Text()
    if counters_part:
        summary.append(counters_part, style='white')
    if daily_summary:
        if counters_part:
            summary.append('   │   ', style='dim')
        summary.append(daily_summary, style='dim')

    children: list[RenderableType] = [title_row]
    if summary.plain:
        children.append(summary)
    if warnings:
        warn_line = Text(warnings, style='bold red')
        children.append(warn_line)
    return Panel(
        Group(*children),
        border_style='dim',
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _split_session_summary(line: str) -> tuple[str, str]:
    """Split `session XmYs · done N · fail M · cost $Z` into duration + rest.

    Empty input → ('', ''). When the leading token doesn't start with
    `session `, treat the whole line as counters so callers that pass a
    non-standard summary still get rendered without losing data.
    """
    if not line:
        return '', ''
    parts = line.split(' · ', 1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ''
    if head.startswith('session '):
        return head, rest
    return '', line


WORKER_NAME_WIDTH = 12


ACT_COLUMN_WIDTH = 12


def render_workers(
    snapshot: AggregatedSnapshot,
    *,
    focused_agent: str | None = None,
    max_rows: int | None = None,
    show_action: bool = True,
    animated: bool = True,
    activity: Mapping[str, Sequence[float]] | None = None,
) -> RenderableType:
    show_activity = activity is not None
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column('marker', no_wrap=True, width=1)
    table.add_column('#', no_wrap=True, width=3, justify='right')
    table.add_column('name', no_wrap=True, width=WORKER_NAME_WIDTH)
    table.add_column('role', no_wrap=True, width=8)
    table.add_column('issue', no_wrap=True, width=10)
    table.add_column('phase', no_wrap=True, width=14)
    if show_activity:
        table.add_column('act', no_wrap=True, width=ACT_COLUMN_WIDTH)
    if show_action:
        table.add_column('action', ratio=1)
    table.add_column('elapsed', no_wrap=True, width=7, justify='right')

    workers = snapshot.workers
    visible = workers if max_rows is None else workers[:max_rows]
    overflow = max(0, len(workers) - len(visible))

    for view in visible:
        focused = focused_agent is not None and view.agent_id == focused_agent
        marker = Text('▌', style='bold yellow') if focused else Text(' ')
        agent_cell = Text(view.agent_id, style='bold' if focused else 'white')
        status = view.status
        role = status.role if status is not None else ''
        issue_ref = status.issue if status is not None else None
        phase = status.phase if status is not None else 'unknown'
        elapsed = _elapsed_for(view)
        name = (
            status.display_name if status is not None else ''
        ) or f'agent-{view.agent_id}'
        name_cell = Text(_truncate(name, WORKER_NAME_WIDTH), style='cyan')
        row: list[RenderableType] = [
            marker,
            agent_cell,
            name_cell,
            _role_badge(role),
            _issue_cell(issue_ref),
            _phase_cell(phase, animated=animated),
        ]
        if show_activity:
            assert activity is not None  # show_activity ↔ activity is not None
            samples = activity.get(view.agent_id, ())
            row.append(
                sparkline(
                    samples,
                    width=ACT_COLUMN_WIDTH,
                    max_value=1.0,
                    color='grey50',
                )
            )
        if show_action:
            row.append(_action_cell(view, animated=animated))
        row.append(Text(_format_elapsed(elapsed), style='dim'))
        if view.is_stale:
            # Tint the whole row red-on-default by overriding cell styles.
            row = [_with_style(cell, 'red') for cell in row]
        table.add_row(*row)
    if overflow:
        empty_extra: list[RenderableType] = []
        if show_activity:
            empty_extra.append(Text(''))
        if show_action:
            empty_extra.append(Text(''))
        table.add_row(
            Text(' '),
            Text('+' + str(overflow), style='dim'),
            Text(''),
            Text('more', style='dim'),
            Text(''),
            Text(''),
            *empty_extra,
            Text(''),
        )
    return boxed(table, title='workers')


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + '…'


def _elapsed_for(view: WorkerView) -> float:
    if view.status is None:
        return -1.0
    if view.status.phase == PHASE_POLLING:
        return -1.0
    if view.status.phase_started_at <= 0:
        return -1.0
    return max(0.0, time.time() - view.status.phase_started_at)


def _with_style(renderable: RenderableType, style: str) -> RenderableType:
    if isinstance(renderable, Text):
        copy = renderable.copy()
        copy.stylize(style)
        return copy
    return renderable


_LINEAR_IDENT_RE = re.compile(r'\b[A-Z][A-Z0-9]+-\d+\b')


def _linkify_message(message: str, base_url: str, base_style: str) -> Text:
    """Wrap any Linear-shaped identifier in `message` as an OSC 8 hyperlink.

    The identifier match (`[A-Z][A-Z0-9]+-\\d+`, e.g. `AI-48`) becomes a
    span styled with `link <base_url>/issue/<ident>`. Surrounding text
    keeps `base_style` only — the link does not bleed onto neighbouring
    characters or column padding. When `base_url` is empty (we haven't
    seen any worker pick up an issue yet, so we can't derive the
    workspace slug), the message renders as plain text.
    """
    if not base_url:
        return Text(message, style=base_style, overflow='ellipsis', no_wrap=True)
    text = Text(overflow='ellipsis', no_wrap=True)
    cursor = 0
    for match in _LINEAR_IDENT_RE.finditer(message):
        if match.start() > cursor:
            text.append(message[cursor : match.start()], style=base_style)
        ident = match.group(0)
        text.append(ident, style=f'{base_style} link {base_url}/issue/{ident}')
        cursor = match.end()
    if cursor < len(message):
        text.append(message[cursor:], style=base_style)
    return text


def linear_base_url(snapshot: AggregatedSnapshot, fallback: str = '') -> str:
    """Derive `https://linear.app/<workspace>` from any known issue URL.

    Linear issue URLs look like `https://linear.app/<workspace>/issue/<key>/<slug>`,
    so any worker that has picked up an issue gives us the workspace
    prefix to template links for identifiers we only see as plain text
    (e.g. inside event log lines). Returns `fallback` when no URL is
    found in the current snapshot — callers cache the last-known value
    so links survive a transient "all workers idle" state.
    """
    for view in snapshot.workers:
        status = view.status
        if status is None or status.issue is None or not status.issue.url:
            continue
        prefix, sep, _ = status.issue.url.partition('/issue/')
        if sep:
            return prefix
    return fallback


def render_events(
    events: list[LogEvent],
    *,
    title: str = 'events',
    max_rows: int | None = None,
    base_url: str = '',
) -> RenderableType:
    body: RenderableType
    if not events:
        body = Align.center(Text('no events yet', style='dim'), vertical='middle')
    else:
        rows = events[-max_rows:] if max_rows is not None else events
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column('time', no_wrap=True, width=8, style='dim')
        table.add_column('agent', no_wrap=True, width=3, style='cyan')
        table.add_column('msg', ratio=1)
        for ev in rows:
            msg_style = _level_style(ev.level)
            msg = _linkify_message(ev.message, base_url, msg_style)
            table.add_row(ev.hms, ev.agent, msg)
        body = table
    return boxed(body, title=title)


def _level_style(level: str) -> str:
    lower = level.lower()
    if lower in {'error', 'critical'}:
        return 'red'
    if lower in {'warning', 'warn'}:
        return 'yellow'
    return 'white'


PREFERRED_STATE_ORDER: tuple[str, ...] = (
    'Triage',
    'Backlog',
    'In Progress',
    'Review',
    'Awaiting approval',
    'Done',
)


def sort_states(counts: dict[str, int]) -> list[tuple[str, int]]:
    """Order workflow states top-to-bottom along the pipeline.

    Known states (PREFERRED_STATE_ORDER) come first in fixed order; any
    custom states fall to the bottom alphabetically.
    """
    order = {name: i for i, name in enumerate(PREFERRED_STATE_ORDER)}
    return sorted(
        counts.items(),
        key=lambda kv: (order.get(kv[0], len(order)), kv[0]),
    )


def render_queue(
    queue: LinearQueueSnapshot,
    *,
    claims_history: Sequence[int] | None = None,
    done_history: Sequence[int] | None = None,
) -> RenderableType:
    body: RenderableType
    if not queue.counts:
        body = Align.center(Text('no data yet', style='dim'), vertical='middle')
    else:
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column('column', ratio=1)
        table.add_column('count', justify='right', no_wrap=True, width=4)
        for name, count in sort_states(queue.counts):
            table.add_row(Text(name, style='white'), Text(str(count), style='cyan'))
        children: list[RenderableType] = [table]
        if claims_history is not None and any(claims_history):
            children.append(_rate_row('claims/min', claims_history))
        if done_history is not None and any(done_history):
            children.append(_rate_row('done/min', done_history))
        body = Group(*children)
    if queue.updated_at <= 0:
        age = ''
    else:
        delta = max(0.0, time.time() - queue.updated_at)
        age = f' (synced {_format_elapsed(delta)} ago)'
    return boxed(body, title=f'linear{age}')


def _rate_row(label: str, values: Sequence[int]) -> RenderableType:
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column('label', width=12, style='dim')
    grid.add_column('spark', no_wrap=True, ratio=1)
    grid.add_column('val', no_wrap=True, justify='right', width=8)
    last = values[-1] if values else 0
    grid.add_row(
        Text(label),
        AutoSparkline(values, color='cyan'),
        Text(str(last), style='cyan'),
    )
    return grid


def render_tokens(
    *,
    daily_tokens: int,
    cap_tokens: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_tokens: int = 0,
    rate_history: Sequence[int] | None = None,
    bar_width: int = 10,
    spark_width: int = 12,
    session_cost_usd: float = 0.0,
    daily_cost_usd: float = 0.0,
) -> RenderableType:
    """btop MEM analog: rolling 24h burn, breakdown, and rate sparkline."""
    burn_row = Table.grid(padding=(0, 1), expand=True)
    burn_row.add_column(width=10, style='dim', no_wrap=True)
    burn_row.add_column(no_wrap=True, ratio=1)
    burn_row.add_column(no_wrap=True, width=14, justify='right')
    if cap_tokens > 0:
        pct = int(min(100, daily_tokens * 100 / cap_tokens))
        burn_label = f'{pct}% {_humanize_tokens(daily_tokens)}'
        burn_bar = bar_gauge(
            float(daily_tokens),
            float(cap_tokens),
            width=bar_width,
            show_pct=False,
        )
    else:
        burn_label = f'{_humanize_tokens(daily_tokens)} · no cap'
        burn_bar = bar_gauge(0.0, 0.0, width=bar_width, show_pct=False)
    burn_row.add_row(Text('daily burn'), burn_bar, Text(burn_label, style='cyan'))

    breakdown = Text()
    breakdown.append('in ', style='dim')
    breakdown.append(_humanize_tokens(input_tokens), style='white')
    breakdown.append('  ·  out ', style='dim')
    breakdown.append(_humanize_tokens(output_tokens), style='white')
    breakdown.append('  ·  cache ', style='dim')
    breakdown.append(_humanize_tokens(cache_tokens), style='white')

    children: list[RenderableType] = [burn_row, breakdown]
    if rate_history is not None and any(rate_history):
        last = rate_history[-1]
        rate_row = Table.grid(padding=(0, 1), expand=True)
        rate_row.add_column(width=10, style='dim', no_wrap=True)
        rate_row.add_column(no_wrap=True, ratio=1)
        rate_row.add_column(no_wrap=True, width=14, justify='right')
        rate_row.add_row(
            Text('rate'),
            sparkline(list(rate_history), width=spark_width, color='cyan'),
            Text(f'{_humanize_tokens(last)}/min', style='cyan'),
        )
        children.append(rate_row)
    if session_cost_usd or daily_cost_usd:
        cost_line = Text()
        cost_line.append('cost ', style='dim')
        cost_line.append(f'${session_cost_usd:.2f}', style='white')
        cost_line.append(' session', style='dim')
        cost_line.append('  ·  ', style='dim')
        cost_line.append(f'${daily_cost_usd:.2f}', style='white')
        cost_line.append(' 24h', style='dim')
        children.append(cost_line)

    return boxed(Group(*children), title='tokens')


def _humanize_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f'{n / 1000:.1f}k'
    return f'{n / 1_000_000:.1f}M'


def render_drilldown(view: WorkerView) -> RenderableType:
    status = view.status
    if status is None or status.issue is None:
        return Panel(
            Align.center(
                Text(f'agent-{view.agent_id} is idle', style='dim'),
                vertical='middle',
            ),
            title=f'agent-{view.agent_id}',
            border_style='cyan',
        )
    issue = status.issue
    role = status.role
    title = Text()
    title.append(f'agent-{view.agent_id}', style='bold')
    title.append('  ·  ')
    title.append(role or 'no role', style=ROLE_COLORS.get(role.upper(), 'white'))
    title.append('  ·  ')
    title.append(
        issue.identifier,
        style=f'bold link {issue.url}' if issue.url else 'bold',
    )
    if issue.title:
        title.append(f'  "{issue.title}"', style='italic dim')

    rows: list[RenderableType] = [title]
    rows.append(
        Text.assemble(
            ('phase: ', 'dim'),
            (status.phase, PHASE_COLORS.get(status.phase, 'white')),
            ('  (', 'dim'),
            (_format_elapsed(_elapsed_for(view)), 'dim'),
            (' since ', 'dim'),
            (_format_clock(status.phase_started_at), 'dim'),
            (')', 'dim'),
        )
    )
    if status.worktree:
        rows.append(Text.assemble(('worktree: ', 'dim'), (status.worktree, 'white')))
    if status.branch:
        rows.append(
            Text.assemble(
                ('branch: ', 'dim'),
                (status.branch, 'white'),
                ('  base: ', 'dim'),
            )
        )
    rows.append(_render_stream_block(status.stream))
    rows.append(_render_recent_tools(status.stream))
    if view.is_stale:
        rows.append(Text('⚠ heartbeat stale', style='bold red'))
    return Panel(Group(*rows), title='', border_style='cyan', padding=(0, 1))


def _render_stream_block(stream: StreamSummary) -> RenderableType:
    counters = Text.assemble(
        ('turns: ', 'dim'),
        (str(stream.turns), 'white'),
        ('   tool_use: ', 'dim'),
        (str(stream.tool_use_count), 'white'),
        ('   tokens: ', 'dim'),
        (_format_tokens(stream), 'white'),
    )
    rows: list[RenderableType] = [counters]
    if stream.last_tool_name:
        last = Text.assemble(
            ('last action:  ', 'dim'),
            (
                f'{stream.last_tool_name}'
                + (f': {stream.last_tool_target}' if stream.last_tool_target else ''),
                'green',
            ),
        )
        rows.append(last)
    if stream.thinking_preview:
        rows.append(
            Text.assemble(
                ('thinking:     ', 'dim'),
                (f'"{stream.thinking_preview}"', 'italic'),
            )
        )
    return Group(*rows)


def _format_tokens(stream: StreamSummary) -> str:
    total = stream.input_tokens + stream.output_tokens
    if total >= 1000:
        return f'{total / 1000:.1f}k'
    return str(total)


def _render_recent_tools(stream: StreamSummary) -> RenderableType:
    if not stream.recent_tools:
        return Text('')
    table = Table.grid(padding=(0, 1))
    table.add_column('time', no_wrap=True, width=8, style='dim')
    table.add_column('name', no_wrap=True, width=10)
    table.add_column('target', ratio=1)
    for entry in reversed(stream.recent_tools):
        ts = float(entry.get('ts', 0.0))
        name = str(entry.get('name', ''))
        target = str(entry.get('target', ''))
        table.add_row(_format_clock(ts), name, target)
    return Group(Text('recent tool calls:', style='dim'), table)


def render_shutdown_overlay() -> RenderableType:
    """Centered modal shown between SIGINT receipt and worker exit.

    The supervisor's signal handler terminates worker processes and waits
    for them to drain. Until they exit (1-2s typically) the TUI keeps
    rendering — this overlay tells the user "we heard you, we're cleaning
    up" so the lag doesn't look like a hang.
    """
    body = Group(
        Text(_spinner_glyph() + '  shutting down', style='bold'),
        Text(''),
        Text('terminating workers, syncing Linear, releasing worktrees.', style='dim'),
        Text('press Ctrl+C again to force-quit.', style='dim'),
    )
    panel = Panel(
        body,
        border_style='dim',
        box=box.ROUNDED,
        padding=(1, 4),
    )
    # `Padding(top=4)` nudges the modal a few rows below the top edge so it
    # doesn't feel pinned to the terminal chrome. `Align.center(vertical=...)`
    # alone collapses leading newlines, hence the explicit padding.
    return Padding(Align.center(panel), (4, 0, 0, 0))


def render_footer(*, focused_agent: str | None) -> RenderableType:
    bits: list[tuple[str, str]] = [
        ('[1-9]', 'bold'),
        ('agent  ', 'dim'),
    ]
    if focused_agent is not None:
        bits.extend(
            [
                ('[Esc]', 'bold'),
                ('back  ', 'dim'),
                ('[Ctrl+K]', 'bold'),
                (' kill  ', 'dim'),
            ]
        )
    bits.extend(
        [
            ('[/]', 'bold'),
            ('search  ', 'dim'),
            ('[p]', 'bold'),
            ('ause  ', 'dim'),
            ('[w]', 'bold'),
            ('arnings  ', 'dim'),
            ('Ctrl+C', 'bold'),
            (' quit', 'dim'),
        ]
    )
    text = Text()
    for value, style in bits:
        text.append(value, style=style)
    return text


def find_view(snapshot: AggregatedSnapshot, agent_id: str | None) -> WorkerView | None:
    if agent_id is None:
        return None
    for v in snapshot.workers:
        if v.agent_id == agent_id:
            return v
    return None


__all__ = [
    'AutoSparkline',
    'bar_gauge',
    'boxed',
    'find_view',
    'render_drilldown',
    'render_events',
    'render_footer',
    'render_header',
    'render_queue',
    'render_shutdown_overlay',
    'render_tokens',
    'render_workers',
    'sparkline',
]
