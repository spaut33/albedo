"""SQLite-backed usage ledger and rolling-window rate-limit guard.

After every `claude -p` spawn the worker calls `record_usage` with the
token counts pulled from the final JSON event. Before the next spawn the
worker calls `should_throttle`, which sums input+output tokens in the
configured rolling window (default 5h) and reports whether the cap from
`config.usage.rolling_window_token_cap` has been reached.

This is a soft guard against accidentally burning through a Claude
subscription's bucket — the API itself enforces the real limit and
returns 429s; the ledger is here so we hit the wall less often.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_FILENAME = 'usage.db'
SCHEMA_VERSION = 2
SECONDS_PER_DAY = 24 * 60 * 60

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    ts_unix: int
    agent_id: str
    issue_id: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    model: str


@dataclass(frozen=True, slots=True)
class WindowStats:
    spawns: int
    tokens: int
    cost_usd: float
    wallclock_sec: int
    done: int
    failed: int


class UsageLedger:
    """Thread- and process-safe SQLite ledger.

    Each instance owns the path; concurrent processes may open the same
    file. We rely on SQLite's filesystem locking and short transactions
    to keep contention rare. A small in-process Lock keeps stdout-style
    races between supervisor housekeeping and a worker minimal too.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            self._init_schema(con)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        # `timeout` parameter avoids "database is locked" on contention.
        con = sqlite3.connect(str(self._db_path), timeout=10.0)
        try:
            con.execute('PRAGMA journal_mode = WAL')
            con.execute('PRAGMA foreign_keys = ON')
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self, con: sqlite3.Connection) -> None:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_records (
                ts_unix INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                issue_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            'CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_records (ts_unix)'
        )
        # v2 migration is gated by BEGIN EXCLUSIVE so concurrent inits
        # (supervisor + worker processes share one usage.db) serialize and only
        # the first process runs ALTER TABLE. Without the lock all readers see
        # `existing` without the new columns, then race on ADD COLUMN.
        con.commit()
        prev_isolation = con.isolation_level
        con.isolation_level = None
        try:
            con.execute('BEGIN EXCLUSIVE')
            try:
                existing = {
                    row[1]
                    for row in con.execute(
                        'PRAGMA table_info(usage_records)'
                    ).fetchall()
                }
                if 'outcome' not in existing:
                    con.execute(
                        'ALTER TABLE usage_records '
                        "ADD COLUMN outcome TEXT NOT NULL DEFAULT ''"
                    )
                if 'wallclock_sec' not in existing:
                    con.execute(
                        'ALTER TABLE usage_records '
                        'ADD COLUMN wallclock_sec INTEGER NOT NULL DEFAULT 0'
                    )
                if 'cost_usd' not in existing:
                    con.execute(
                        'ALTER TABLE usage_records '
                        'ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0.0'
                    )
                if 'role' not in existing:
                    con.execute(
                        'ALTER TABLE usage_records '
                        "ADD COLUMN role TEXT NOT NULL DEFAULT ''"
                    )
                con.execute('COMMIT')
            except:
                con.execute('ROLLBACK')
                raise
        finally:
            con.isolation_level = prev_isolation
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        con.execute(
            'INSERT OR REPLACE INTO usage_meta (key, value) VALUES (?, ?)',
            ('schema_version', str(SCHEMA_VERSION)),
        )

    def record_usage(
        self,
        *,
        agent_id: str,
        issue_id: str,
        usage: Mapping[str, int],
        model: str = '',
        ts_unix: int | None = None,
        outcome: str = '',
        wallclock_sec: int = 0,
        cost_usd: float = 0.0,
        role: str = '',
    ) -> None:
        """Insert one row reflecting the spawn's token usage and outcome."""
        ts = int(ts_unix if ts_unix is not None else time.time())
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO usage_records (
                    ts_unix, agent_id, issue_id,
                    input_tokens, output_tokens,
                    cache_creation_tokens, cache_read_tokens, model,
                    outcome, wallclock_sec, cost_usd, role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    agent_id,
                    issue_id,
                    int(usage.get('input_tokens', 0)),
                    int(usage.get('output_tokens', 0)),
                    int(usage.get('cache_creation_input_tokens', 0)),
                    int(usage.get('cache_read_input_tokens', 0)),
                    model,
                    outcome,
                    int(wallclock_sec),
                    float(cost_usd),
                    role,
                ),
            )

    def tokens_in_window(
        self,
        *,
        window_seconds: int,
        now_unix: int | None = None,
    ) -> int:
        """Sum input+output tokens recorded in the last `window_seconds`.

        Cache tokens are NOT counted: they're effectively free against the
        usage budget on Claude subscriptions, and including them would
        cause the guard to over-throttle.
        """
        now = int(now_unix if now_unix is not None else time.time())
        cutoff = now - max(0, window_seconds)
        with self._lock, self._connect() as con:
            row = con.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0)
                     + COALESCE(SUM(output_tokens), 0)
                FROM usage_records
                WHERE ts_unix >= ? AND ts_unix <= ?
                """,
                (cutoff, now),
            ).fetchone()
        return int(row[0] or 0)

    def should_throttle(
        self,
        *,
        token_cap: int,
        window_seconds: int,
        now_unix: int | None = None,
    ) -> bool:
        """True when the rolling window's tokens have reached the cap."""
        if token_cap <= 0:
            return False
        return (
            self.tokens_in_window(window_seconds=window_seconds, now_unix=now_unix)
            >= token_cap
        )

    def stats_window(
        self,
        *,
        window_seconds: int = SECONDS_PER_DAY,
        now_unix: int | None = None,
    ) -> WindowStats:
        """Aggregate spawns in the last `window_seconds` for the TUI footer."""
        now = int(now_unix if now_unix is not None else time.time())
        cutoff = now - max(0, window_seconds)
        with self._lock, self._connect() as con:
            row = con.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(input_tokens) + SUM(output_tokens), 0),
                    COALESCE(SUM(cost_usd), 0.0),
                    COALESCE(SUM(wallclock_sec), 0),
                    COALESCE(
                        SUM(CASE WHEN outcome = 'done' THEN 1 ELSE 0 END), 0
                    ),
                    COALESCE(
                        SUM(CASE WHEN outcome IN ('fail', 'timeout') THEN 1 ELSE 0 END),
                        0
                    )
                FROM usage_records
                WHERE ts_unix >= ? AND ts_unix <= ?
                """,
                (cutoff, now),
            ).fetchone()
        return WindowStats(
            spawns=int(row[0] or 0),
            tokens=int(row[1] or 0),
            cost_usd=float(row[2] or 0.0),
            wallclock_sec=int(row[3] or 0),
            done=int(row[4] or 0),
            failed=int(row[5] or 0),
        )


def default_db_path(state_dir: Path) -> Path:
    """`<state_dir>/usage.db` — convention used across the orchestrator."""
    return state_dir / DEFAULT_DB_FILENAME
