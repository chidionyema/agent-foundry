"""Assembly and run wiring for the price-alert army.

A ``many`` order converges to a DAG (ORDER.md section 2 -> section 6). Here the
five nodes are wired as a directed chain of topics; each node's handler is the
function core above. Two transports share the SAME composition:

- :class:`InProcessRunner` walks the DAG inside one process — used by tests and by
  the headless milestone so a run provably converges without a live cluster.
- A NATS worker (see :mod:`af.cli`) binds each node to its tenant-scoped subject
  for the in-cluster path.

The composition (``ARMY``) is the single source of truth for which slugs exist and
their order, so the bus and the in-process runner cannot drift apart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .meter import Execution, Meter
from .nodes import (
    Scout,
    alert_core,
    dom_strip,
    extract_price,
    math_check,
)


@dataclass(frozen=True)
class NodeSpec:
    slug: str
    job: Callable[[dict[str, Any]], Any]  # async handler
    inputs: tuple[str, ...]  # upstream slugs whose output feeds this node
    output_field: str | None = (
        None  # which input key holds upstream output (None=default 'output')
    )


# The price-alert DAG, mirroring ORDER.md section 6's table exactly.
# Scout(keyword) -> dom-stripper -> extractor -> math-checker -> alerter
class PriceAlertArmy:
    """Composition for one price-alert goal. Tenancy + our_price come from scope."""

    def __init__(
        self,
        our_price: float,
        threshold_pct: float = 10.0,
        product_name: str = "product",
    ) -> None:
        self.our_price = our_price
        self.threshold_pct = threshold_pct
        self.product_name = product_name
        self._scout = Scout()

    def node(self, slug: str) -> Callable[[dict[str, Any]], Any] | None:
        """Return the job handler for ``slug`` or None if that slug isn't in this
        army (so a tenant scoping a subject to a wrong slug fails to bind, close)."""
        return {
            "scout": self._scout.handle,
            "dom-stripper": self._dom,
            "extractor": self._extract,
            "math-checker": self._check,
            "alerter": self._alert,
        }.get(slug)

    async def _dom(self, payload: dict[str, Any]) -> dict[str, Any]:
        html = payload.get("html", "")
        return dom_strip(html, base_url=payload.get("url"))

    async def _extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = payload.get("text", "")
        return extract_price(text, product_name=self.product_name)

    async def _check(self, payload: dict[str, Any]) -> dict[str, Any]:
        extracted = payload
        return math_check(extracted, self.our_price, threshold_pct=self.threshold_pct)

    async def _alert(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        comparison = payload
        return alert_core(
            comparison,
            product=self.product_name,
            competitor_url=comparison.get("url", "unknown"),
        )

    # DAG edges (slug -> upstream slugs that must run first).
    EDGES = {
        "scout": (),
        "dom-stripper": ("scout",),
        "extractor": ("dom-stripper",),
        "math-checker": ("extractor",),
        "alerter": ("math-checker",),
    }


class InProcessRunner:
    """Walks the army DAG headlessly. For the milestone this is what makes a
    ``many`` order *converge* end to end with no cluster. Real in-cluster operation
    uses the same node handlers bound to NATS subjects in :mod:`af.cli`."""

    def __init__(
        self,
        army: PriceAlertArmy,
        meter: Meter | None,
        *,
        tenant_id: str,
        order_id: str,
        run_id: str,
    ) -> None:
        self._army = army
        self._meter = meter
        self._tenant_id = tenant_id
        self._order_id = order_id
        self._run_id = run_id

    async def run(self, seed: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Execute each node in edge order, threading each node's output to its
        dependents as field 'output'. Meter every node that actually runs."""
        results: dict[str, dict[str, Any]] = {}
        edges = self._army.EDGES

        # Order by dependency (simple topo via repeated passes; 5 nodes).
        executed: set[str] = set()
        remaining = {slug for slug in edges if self._army.node(slug) is not None}

        while remaining:
            progressed = False
            for slug in list(remaining):
                if all(u in executed for u in edges[slug]):
                    handler = self._army.node(slug)
                    node_input: dict[str, Any] = {}
                    # The competitor url is run-level context every passed-along node
                    # should carry, so the final alerter can name the source page
                    # even after earlier nodes transformed its fields away.
                    if seed.get("url"):
                        node_input["url"] = seed["url"]
                    if slug == "scout":
                        node_input["html"] = seed.get("html", "")
                    else:
                        up = edges[slug][0]
                        node_input.update(results.get(up, {}) or {})

                    started = time.time()
                    ex = Execution(
                        tenant_id=self._tenant_id,
                        order_id=self._order_id,
                        run_id=self._run_id,
                        agent_slug=slug,
                        status="ran",
                        started_at=started,
                    )
                    try:
                        out = await handler(node_input)
                    except Exception as exc:  # noqa: BLE001 - a failing node marks the run failed
                        ex.status = "failed"
                        ex.detail = {"error": str(exc)}
                        ex.finished_at = time.time()
                        if self._meter:
                            self._meter.record(ex)
                        raise
                    ex.finished_at = time.time()
                    ex.detail = {"produced": bool(out)}
                    if self._meter:
                        self._meter.record(ex)
                    results[slug] = out or {}
                    executed.add(slug)
                    remaining.discard(slug)
                    progressed = True
            if not progressed:
                # cycle or missing upstream: treat as failure, don't loop forever
                raise RuntimeError(
                    f"army DAG could not progress; remaining={sorted(remaining)}"
                )
        return results
