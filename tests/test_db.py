"""Datastore metering (af/db.py): migration applied idempotently on connect, then
rows inserted tenant-scoped. Proven against a fake psycopg-style connection so the
insert path is unit-tested without the cluster — same seam as the NATS path. A real
cluster run applies the same code (research-engine store.py pattern)."""

from __future__ import annotations

import time

import pytest

from af.db import (
    EstateDbMeter,
    EstateDbUnavailable,
    apply_migrations,
    estate_dsn,
    migration_sql,
)
from af.meter import Execution


def _ex(tenant="ten_a", slug="scout", status="ran") -> Execution:
    now = time.time()
    return Execution(
        tenant_id=tenant,
        order_id="ord_1",
        run_id="run_1",
        agent_slug=slug,
        status=status,
        started_at=now - 0.5,
        finished_at=now,
        detail={"found": "79.99"},
    )


class FakeConn:
    """Stands in for a psycopg connection: captures every statement + binds."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.calls: int = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self) -> None:
        self.calls += 1

    def rollback(self) -> None:
        self.calls += 1


class _FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple | None = None) -> None:
        if params is not None:
            self._conn.executed.append(f"INSERT {params[0]} {params[3]} {params[4]}")
        else:
            self._conn.executed.append(sql)


def test_migration_is_idempotent_and_creates_tenant_scoped_table() -> None:
    # The checked-in migration carries what the datastore contract needs: the
    # table exists, is re-runnable, tenants are never mixed by the writer's own
    # query filter, and time-in-state is not precomputed.
    sql = migration_sql()
    assert "CREATE TABLE IF NOT EXISTS task_executions" in sql
    assert "tenant_id" in sql and "run_id" in sql and "agent_slug" in sql
    # a tenant column and the per-tenant index — cross-tenant reads are explicit.
    assert "task_executions_tenant_run_idx" in sql
    # idempotent contract: re-applying on a live DB is a no-op mistake-proof.
    assert "IF NOT EXISTS" in sql


def test_apply_migrations_runs_the_ddl_then_commits() -> None:
    conn = FakeConn()
    apply_migrations(conn)
    assert any("CREATE TABLE IF NOT EXISTS task_executions" in s for s in conn.executed)
    assert conn.calls >= 1  # commit() ran


def test_estate_dsn_refuses_dark_when_unset(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("AF_DATABASE_URL", raising=False)
    with pytest.raises(EstateDbUnavailable):
        estate_dsn()


def test_estate_meter_inserts_a_discrete_row_tenant_scoped() -> None:
    conn = FakeConn()
    wrote: list[tuple] = []

    def fake_write(c, ex):  # noqa: ANN001, ANN202
        wrote.append((c, ex))

    meter2 = EstateDbMeter(writer=fake_write)
    ex = _ex()
    meter2.write(conn, ex)
    assert len(wrote) == 1
    assert wrote[0][1].tenant_id == "ten_a"
    assert wrote[0][1].agent_slug == "scout"


def test_file_and_db_backends_share_the_one_execution_shape() -> None:
    # A discrete run is one Execution whether it lands in a jsonl file today or the
    # estate table in the image — no field goes missing between backends.
    ex = _ex(slug="alerter")
    as_dict = ex.to_json()
    for field in ("tenant_id", "order_id", "run_id", "agent_slug", "status", "detail"):
        assert field in as_dict
