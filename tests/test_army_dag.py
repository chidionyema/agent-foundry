"""Legion 3 production pins: generic DAG composition, multi-input threading,
per-node timeouts, fail-fast with metered skips, cycle detection, level
concurrency. These pin the guarantees the Legion program makes about the Army
legion; the pre-existing test_army.py pins the price-alert composition itself."""

from __future__ import annotations

import asyncio
import time

import pytest

from af.army import (
    Army,
    ArmyNodeSpec,
    ArmyRunError,
    ArmySpecError,
    InProcessRunner,
    MAX_NODE_TIMEOUT_S,
)
from af.meter import Meter


def _job(output: dict, delay: float = 0.0):
    async def run(payload: dict) -> dict:
        if delay:
            await asyncio.sleep(delay)
        return dict(output)

    return run


def _runner(army: Army, rows: list) -> InProcessRunner:
    return InProcessRunner(
        army, Meter(rows.append), tenant_id="t", order_id="o", run_id="r"
    )


# --------------------------------------------------------------------------- #
# The multi-input bug: pre-Legion runner threaded only edges[slug][0]
# --------------------------------------------------------------------------- #
def test_multi_input_node_receives_all_upstreams() -> None:
    seen: dict = {}

    async def join(payload: dict) -> dict:
        seen.update(payload)
        return {"joined": True}

    army = Army(
        [
            ArmyNodeSpec("a", _job({"x": 1})),
            ArmyNodeSpec("b", _job({"y": 2})),
            ArmyNodeSpec("c", join, inputs=("a", "b")),
        ]
    )
    results = asyncio.run(_runner(army, []).run({}))
    assert results["c"] == {"joined": True}
    # flat merge: both upstreams visible
    assert seen["x"] == 1 and seen["y"] == 2
    # namespaced view: each upstream addressable explicitly
    assert seen["inputs"]["a"] == {"x": 1}
    assert seen["inputs"]["b"] == {"y": 2}


def test_multi_input_collision_last_declared_wins() -> None:
    seen: dict = {}

    async def join(payload: dict) -> dict:
        seen.update(payload)
        return {}

    army = Army(
        [
            ArmyNodeSpec("a", _job({"k": "from-a"})),
            ArmyNodeSpec("b", _job({"k": "from-b"})),
            ArmyNodeSpec("c", join, inputs=("a", "b")),
        ]
    )
    asyncio.run(_runner(army, []).run({}))
    assert seen["k"] == "from-b"  # declared order is the precedence rule
    assert seen["inputs"]["a"]["k"] == "from-a"  # both still reachable namespaced


# --------------------------------------------------------------------------- #
# Timeouts
# --------------------------------------------------------------------------- #
def test_node_timeout_is_enforced_and_metered() -> None:
    rows: list = []
    army = Army(
        [ArmyNodeSpec("slow", _job({"never": True}, delay=5.0), timeout_s=0.05)]
    )
    with pytest.raises(ArmyRunError) as err:
        asyncio.run(_runner(army, rows).run({}))
    assert "slow" in err.value.failures
    assert rows[0]["status"] == "failed"
    assert rows[0]["detail"]["error_kind"] == "timeout"
    assert rows[0]["detail"]["timeout_s"] == 0.05


def test_timeout_is_clamped_to_ceiling() -> None:
    assert ArmyNodeSpec("n", _job({}), timeout_s=999999).timeout_s == MAX_NODE_TIMEOUT_S


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ArmyNodeSpec("n", _job({}), timeout_s=0)


# --------------------------------------------------------------------------- #
# Spec validation — eager, at construction, never mid-run
# --------------------------------------------------------------------------- #
def test_cycle_rejected_at_construction() -> None:
    with pytest.raises(ArmySpecError, match="cycle"):
        Army(
            [
                ArmyNodeSpec("a", _job({}), inputs=("b",)),
                ArmyNodeSpec("b", _job({}), inputs=("a",)),
            ]
        )


def test_duplicate_slug_rejected() -> None:
    with pytest.raises(ArmySpecError, match="duplicate"):
        Army([ArmyNodeSpec("a", _job({})), ArmyNodeSpec("a", _job({}))])


def test_unknown_upstream_rejected() -> None:
    with pytest.raises(ArmySpecError, match="unknown"):
        Army([ArmyNodeSpec("a", _job({}), inputs=("ghost",))])


def test_from_spec_unknown_handler_rejected() -> None:
    with pytest.raises(ArmySpecError, match="unknown handler"):
        Army.from_spec({"nodes": [{"slug": "x", "handler": "nope"}]}, {})


def test_from_spec_builds_and_runs() -> None:
    async def double(payload: dict) -> dict:
        return {"n": payload.get("n", 1) * 2}

    army = Army.from_spec(
        {
            "nodes": [
                {"slug": "seed", "handler": "seed"},
                {"slug": "d1", "handler": "double", "inputs": ["seed"]},
                {"slug": "d2", "handler": "double", "inputs": ["d1"]},
            ]
        },
        {"seed": _job({"n": 3}), "double": double},
    )
    results = asyncio.run(_runner(army, []).run({}))
    assert results["d2"]["n"] == 12


# --------------------------------------------------------------------------- #
# Fail-fast with full accounting
# --------------------------------------------------------------------------- #
def test_failed_upstream_marks_dependents_skipped_and_metered() -> None:
    async def boom(payload: dict) -> dict:
        raise RuntimeError("kaboom")

    rows: list = []
    army = Army(
        [
            ArmyNodeSpec("bad", boom),
            ArmyNodeSpec("ok", _job({"fine": True})),
            ArmyNodeSpec("child", _job({}), inputs=("bad",)),
            ArmyNodeSpec("grandchild", _job({}), inputs=("ok", "child")),
        ]
    )
    with pytest.raises(ArmyRunError) as err:
        asyncio.run(_runner(army, rows).run({}))
    assert set(err.value.failures) == {"bad"}
    assert "child" in err.value.skipped and "grandchild" in err.value.skipped

    by_slug = {r["agent_slug"]: r for r in rows}
    assert by_slug["bad"]["status"] == "failed"
    assert by_slug["bad"]["detail"]["error_kind"] == "exception"
    assert by_slug["ok"]["status"] == "ran"  # independent branch still ran
    assert by_slug["child"]["status"] == "skipped"
    assert "bad" in by_slug["child"]["detail"]["reason"]
    assert by_slug["grandchild"]["status"] == "skipped"


# --------------------------------------------------------------------------- #
# Level concurrency — independent nodes must not serialize
# --------------------------------------------------------------------------- #
def test_independent_nodes_run_concurrently() -> None:
    army = Army(
        [
            ArmyNodeSpec("a", _job({"a": 1}, delay=0.20)),
            ArmyNodeSpec("b", _job({"b": 1}, delay=0.20)),
            ArmyNodeSpec("c", _job({"c": 1}), inputs=("a", "b")),
        ]
    )
    started = time.monotonic()
    results = asyncio.run(_runner(army, []).run({}))
    elapsed = time.monotonic() - started
    assert set(results) == {"a", "b", "c"}
    assert elapsed < 0.35, f"levels serialized: {elapsed:.2f}s for two 0.2s nodes"


# --------------------------------------------------------------------------- #
# Runner contract details
# --------------------------------------------------------------------------- #
def test_source_nodes_receive_full_seed() -> None:
    seen: dict = {}

    async def probe(payload: dict) -> dict:
        seen.update(payload)
        return {}

    army = Army([ArmyNodeSpec("s", probe)])
    asyncio.run(_runner(army, []).run({"url": "https://x.example", "html": "<p/>"}))
    assert seen["url"] == "https://x.example"
    assert seen["html"] == "<p/>"


def test_url_context_propagates_downstream() -> None:
    seen: dict = {}

    async def probe(payload: dict) -> dict:
        seen["url"] = payload.get("url")
        return {}

    army = Army(
        [
            ArmyNodeSpec("s", _job({"text": "hi"})),
            ArmyNodeSpec("d", probe, inputs=("s",)),
        ]
    )
    asyncio.run(_runner(army, []).run({"url": "https://x.example"}))
    assert seen["url"] == "https://x.example"


def test_every_meter_row_carries_duration_and_run_identity() -> None:
    rows: list = []
    army = Army(
        [ArmyNodeSpec("a", _job({"x": 1})), ArmyNodeSpec("b", _job({}), inputs=("a",))]
    )
    asyncio.run(_runner(army, rows).run({}))
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "ran"
        assert row["run_id"] == "r" and row["tenant_id"] == "t"
        assert isinstance(row["detail"]["duration_ms"], float)
    assert rows[1]["detail"]["inputs_from"] == ["a"]
