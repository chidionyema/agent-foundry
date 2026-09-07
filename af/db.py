"""Datastore-side metering for agent-founderly task runs (ORDER.md §3 rides-ahead).

ORDER.md: *metering is platform, and the estate's DB is the estate's DB — one DB
row, not a second store.* So agent-foundry holds no private datastore. This module
writes each discrete task execution into a single table, ``task_executions``, in
the DB the operator provisions for this consumer on the estate CNPG cluster (a
``Database`` object owned by role ``agent_foundry``, mirrors otto_gateway/research
in idp/platform/estate-db). The one migration that guarantees the table, applied
idempotently on connect, mirrors research-engine/engine/store.py.

Refuses to boot when the estate DSN or the migration is missing (the agent-workforce
``estate.load`` contract): a run that cannot be recorded must not pretend to run
dark. In tests the writer is given a fake connection/driver, so the insert path is
proved without the cluster — the same honest seam as the bus path (af/nats_bus.py
vs af/bus.py).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .meter import Execution

# The migration lives in this repo (db/task_executions.sql) — resolved from this
# file, never from the checkout cwd, so an installed copy still finds it
# (hardcode-fence; mirror order.py loading order/schema.json).
_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "db" / "task_executions.sql"


class EstateDbUnavailable(RuntimeError):
    """Raised when a required estate DB value is missing; the run must not go dark."""


def _need(name: str, why: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EstateDbUnavailable(
            f"{name} is not set; it is {why}. Refuse to run dark."
        )
    return value


def estate_dsn() -> str:
    """The estate DB address. ``AF_DATABASE_URL`` is the one knob; never a literal.

    In the deployed image the manifest injects the real estate Postgres DSN
    (credentials through the estate's secret store); in tests the caller points it
    at whatever the fake driver binds.
    """
    return _need(
        "AF_DATABASE_URL",
        "the DSN of the estate DB that owns this consumer's task_executions table",
    )


def migration_sql() -> str:
    path = _MIGRATION_PATH
    if not path.is_file():
        raise EstateDbUnavailable(
            f"migration missing at {path}; cannot guarantee the table"
        )
    return path.read_text(encoding="utf-8")


def apply_migrations(conn: Any) -> None:
    """Apply the idempotent DDL on connect, then the writer is safe to use.

    ``conn`` mirrors the psycopg connection research uses; a test passes a fake
    conn that records the executed SQL so the migration is proven applied before
    any insert (never a silent no-op).
    """
    sql = migration_sql()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class EstateDbMeter:
    """Writes each discrete ``Execution`` as one row in ``task_executions``.

    Tenanted at rest: the row's ``tenant_id`` is the gateway-validated value that
    also names the bus subject, so a read for one tenant can never surface another
    tenant's runs.
    """

    def __init__(
        self,
        writer: Callable[[Any, Execution], None] | None = None,
    ) -> None:
        # writer(conn, execution). The default binds the real psycopg connect;
        # a test injects a fake conn + writer so the insert SQL is proven without
        # the cluster. The conn itself is opened lazily by the caller / runner so
        # headless (jsonl) runs never touch a driver.
        self._writer = writer if writer is not None else _write_row

    def write(self, conn: Any, ex: Execution) -> None:
        self._writer(conn, ex)


def _write_row(conn: Any, ex: Execution) -> None:
    """Insert one execution row. Timestamps epoch-float (what Execution carries)
    are converted to timestamptz by postgres; detail JSON is the exact dict the
    meter recorded."""
    import datetime as _dt

    def _ts(secs: float) -> str:
        return _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc).isoformat()

    finished = _ts(ex.finished_at) if ex.finished_at is not None else None
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO task_executions
                (tenant_id, order_id, run_id, agent_slug, status,
                 started_at, finished_at, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                ex.tenant_id,
                ex.order_id,
                ex.run_id,
                ex.agent_slug,
                ex.status,
                _ts(ex.started_at),
                finished,
                __import__("json").dumps(ex.detail, sort_keys=True)
                if ex.detail
                else "{}",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
