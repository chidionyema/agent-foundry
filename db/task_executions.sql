-- agent-foundry task-execution metering — datastore-side (ORDER.md §3 "rides ahead").
--
-- One estate Postgres database (one Database object on the estate CNPG cluster,
-- owner role agent_foundry, mirroring otto_gateway / research in
-- idp/platform/estate-db). No second datastore. Every discrete agent task run is
-- one row here, tenant-scoped, so per-tenant queries never cross a tenant
-- (cross-tenant isolation is a hard estate rule, and the writer's tenant_id is
-- the same gateway-validated value that names the bus subject).
--
-- Idempotent by design: every statement is IF NOT EXISTS, so applying this file
-- against a fresh database creates the schema and against an existing one is a
-- no-op — the estate precedent (otto/memory/migrations, research-engine db/ddl.sql).
-- Applied on connect by af/db.py (mirrors research-engine/engine/store.py) before
-- any writer is used, so no run can leave a row un-addressable.

BEGIN;

-- The one metering table: discrete runs of a single agent node under an order.
-- One row per discrete task run; the run_id groups the army's nodes of one order
-- into one unit for a tenant.
CREATE TABLE IF NOT EXISTS task_executions (
    id           BIGSERIAL PRIMARY KEY,

    tenant_id    TEXT        NOT NULL,
    order_id     TEXT        NOT NULL,
    run_id       TEXT        NOT NULL,

    -- One of the army node names (scout, dom-stripper, extractor, math-checker,
    -- alerter) or any later node slug. NOT NULL — the fail-closed point every
    -- writer merges on; there is no run without a node name.
    agent_slug   TEXT        NOT NULL,

    -- ran | failed(transient) | poisoned | dead-lettered — the worker's status
    -- vocabulary from af/worker.py, kept as text so the set can grow without a
    -- migration.
    status       TEXT        NOT NULL,

    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,

    -- The node's structured output / error detail as sent by the meter. Kept as
    -- JSONB; a writer stores the exact dict the Execution carries, so the row and
    -- the bus event never drift.
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb,

    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Time-in-state is computed at read time, never stored: started_at /
    -- finished_at are what they are.
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

-- Per-tenant, per-run reads are the only reads this table is built for (a tenant
-- sees its own runs; the per-run view is the LLM-facing "what did this order do").
CREATE INDEX IF NOT EXISTS task_executions_tenant_run_idx
    ON task_executions (tenant_id, run_id, started_at DESC);

-- An agent slug is looked up by status within a tenant (dead-letter dashboard).
CREATE INDEX IF NOT EXISTS task_executions_tenant_status_idx
    ON task_executions (tenant_id, status, started_at DESC)
    WHERE status <> 'ran';

COMMIT;
