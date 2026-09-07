"""Serve wiring: ArmyBusServe must bind one worker per army node to its tenant-
scoped subject and forward each node's output to its downstream subjects. Relled
on a FakeBus standing in for the estate NATS row, so wiring is proven without the
cluster but exercises the real construction logic (serve.py is otherwise untested)."""

from __future__ import annotations

import asyncio

from af.serve import ArmyBusServe


class FakeBus:
    def __init__(self) -> None:
        self.consumed: list[str] = []
        self.published: list[tuple[str, dict]] = []

    async def consume(self, subject: str, **kw):  # noqa: ANN003
        self.consumed.append(subject)
        return []  # empty subscription, no msgs

    async def publish_json(self, subject: str, payload: dict) -> None:
        self.published.append((subject, payload))


def test_serve_binds_five_nodes_to_tenant_scoped_subjects() -> None:
    bus = FakeBus()
    serve = ArmyBusServe(bus, "ten_x", our_price=100.0)
    asyncio.run(serve.start())

    expected = [
        "tasks.ten_x.scout",
        "tasks.ten_x.dom-stripper",
        "tasks.ten_x.extractor",
        "tasks.ten_x.math-checker",
        "tasks.ten_x.alerter",
    ]
    assert bus.consumed == expected, bus.consumed
    assert len(serve._workers) == 5
    # every worker's subject is tenant-scoped to ten_x
    for w in serve._workers:
        assert w.subject.startswith("tasks.ten_x."), w.subject


def test_subject_naming_never_crosses_tenant() -> None:
    # a serve for tenant B constructs only tenant B subjects even though the army
    # slugs are identical to tenant A's — no .ten_a. token can leak in.
    bus = FakeBus()
    serve = ArmyBusServe(bus, "ten_b", our_price=100.0)
    asyncio.run(serve.start())
    for subject in bus.consumed:
        assert subject.startswith("tasks.ten_b.")
        assert ".ten_a." not in subject
