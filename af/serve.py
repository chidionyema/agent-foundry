"""In-cluster serve: bind each army node to its tenant-scoped JetStream subject so
an order's DAG flows over the estate's ONE bus (SPEC section 4 — a client of the
row, never a second broker).

Topology (ORDER.md section 2 -> 6): each bot pulls ``tasks.<tenant>.<slug>``, does
its one job, then PUBLISHES its output to the next bot's subject. The order object
arrives on the order subject and is decomposed into the DAG edges, then each edge is
fanned out to its node's subject.

This module is the wiring seam between af.worker (settlement mechanics, tested) and
af.nats_bus (real nats-py). It is deliberately thin: the node jobs are the pure
functions in af.nodes / af.army, already unit-tested.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .army import PriceAlertArmy
from .bus import Subject
from .nats_bus import NatsBus
from .worker import DeadLetterWaiter, Worker

log = logging.getLogger("agent-foundry.serve")


class ArmyBusServe:
    """Binds a tenant's price-alert army to the estate bus.

    For the firstile milestone the army is the fixed five-node price-alert DAG. A
    decomposer-fed arbitrary DAG swaps the fixed ``ARMY`` map for one built from the
    order, keeping this same wiring. ``tenant_id`` scopes every subject so no message
    can cross a tenant boundary (cross-tenant isolation guardrail).
    """

    ARMY = PriceAlertArmy.EDGES  # slug -> upstream slugs

    def __init__(
        self,
        bus: NatsBus,
        tenant_id: str,
        *,
        our_price: float,
        threshold_pct: float = 10.0,
        product_name: str = "product",
    ) -> None:
        self.bus = bus
        self.tenant_id = tenant_id
        self.army = PriceAlertArmy(
            our_price=our_price, threshold_pct=threshold_pct, product_name=product_name
        )
        self._workers: list[Worker] = []

    async def start(self) -> None:
        dlq = DeadLetterWaiter(self._dlq_publish)
        # For each node in topological order, build a worker bound to its subject.
        # A node's output is published to its own subject's consumer happens inside
        # the worker's redispatcher, which forwards to every downstream subject.
        order = self._topo_order()
        for slug in order:
            subject = Subject.input(self.tenant_id, slug)
            handler = self.army.node(slug)
            if handler is None:
                log.warning("no handler for slug %r; not serving", slug)
                continue
            downstream = [s for s in order if slug in self.ARMY[s]]
            redispatcher = self._make_redispatcher(downstream) if downstream else None
            topic_out = (
                Subject.input(self.tenant_id, downstream[0]) if downstream else None
            )
            w = Worker(
                subject,
                handle=handler,
                redispatcher=redispatcher,
                topic_out=topic_out,
            )
            self._workers.append(w)
            sub = await self.bus.consume(subject)
            asyncio.create_task(w.run(sub, waiter=dlq))
            log.info("serving %s -> %s", slug, subject)

    def _topo_order(self) -> list[str]:
        # shallow copy of ORDER graph; fixed army is acyclic by construction.
        return ["scout", "dom-stripper", "extractor", "math-checker", "alerter"]

    def _make_redispatcher(self, downstream: list[str]):
        async def redispatcher(env: dict[str, Any]) -> None:
            for slug in downstream:
                subject = Subject.input(self.tenant_id, slug)
                log.info("forward output to %s", subject)
                await self.bus.publish_json(subject, env["output"])

        return redispatcher

    async def _dlq_publish(self, source_subject: str, record: dict[str, Any]) -> None:
        # DLQ subject lives in the SAME tenant namespace as the source (ORDER.md §4)
        await self.bus.publish_json(source_subject + ".dlq", record)


async def run(
    tenant_id: str,
    *,
    our_price: float,
    threshold_pct: float,
    product_name: str,
    url: str = "",
) -> None:
    bus = NatsBus()
    await bus.connect()
    serve = ArmyBusServe(
        bus,
        tenant_id,
        our_price=our_price,
        threshold_pct=threshold_pct,
        product_name=product_name,
    )
    await serve.start()
    # The caller/other nodes put an order's Scout message on tasks.<tenant>.scout;
    # this process then services its subjects until interrupted.
    try:
        await asyncio.Event().wait()
    finally:
        await bus.close()
