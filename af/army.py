"""Assembly and run wiring for armies — generic DAG composition (Legion 3).

A ``many`` order converges to a DAG (ORDER.md section 2 -> section 6). Two
transports share the SAME composition:

- :class:`InProcessRunner` walks the DAG inside one process — used by tests and by
  the headless milestone so a run provably converges without a live cluster.
- A NATS worker (see :mod:`af.cli`) binds each node to its tenant-scoped subject
  for the in-cluster path.

2026-11-06 Legion production pass:

- **Generic composition** — :class:`Army` takes any list of :class:`ArmyNodeSpec`
  (or a JSON-able spec + handler registry via :meth:`Army.from_spec`), so a legion
  can be assigned ANY goal, not only the price-alert chain. ``PriceAlertArmy`` is
  now one composition built from the same machinery; its ``EDGES`` class attribute
  is unchanged and remains the price-alert source of truth.
- **Multi-input threading bug fixed** — the old runner threaded only
  ``edges[slug][0]``, silently dropping every other upstream. Now ALL upstream
  outputs are merged in declared order (later inputs win key collisions) and are
  additionally exposed namespaced under ``payload["inputs"][<upstream_slug>]`` so
  diamond-shaped DAGs are addressable without guessing.
- **Per-node timeouts** — every node runs under ``asyncio.wait_for`` with a
  clamped ceiling (default 300s, hard max 3600s, matching the pi_bridge ceiling).
  Timeouts are metered distinctly (``detail.error_kind == "timeout"``).
- **Level concurrency** — independent nodes in the same topo level execute
  concurrently (``asyncio.gather``); a legion with N independent scouts no longer
  serializes them.
- **Fail-fast, fully metered** — spec errors (dupes, unknown inputs, cycles)
  raise at CONSTRUCTION, never mid-run; a node failure aborts the run after the
  in-flight level settles, and every node that never ran is metered as
  ``status="skipped"`` with its blocking upstreams named. Nothing is invisible.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .meter import Execution, Meter
from .nodes import (
    Scout,
    alert_core,
    dom_strip,
    extract_price,
    math_check,
)

JobHandler = Callable[[dict[str, Any]], Awaitable[Any]]

# Per-node execution ceiling. The default keeps a stuck node from stalling a run
# for hours; the max matches the pi_bridge MAX_TIMEOUT_S ceiling so policy is
# consistent across legions.
DEFAULT_NODE_TIMEOUT_S = 300.0
MAX_NODE_TIMEOUT_S = 3600.0


def _clamp_timeout(value: float | int | None) -> float:
    if value is None:
        return DEFAULT_NODE_TIMEOUT_S
    v = float(value)
    if v <= 0:
        raise ValueError(f"node timeout must be positive, got {value!r}")
    return min(v, MAX_NODE_TIMEOUT_S)


class ArmySpecError(ValueError):
    """An army composition is invalid (dupe slug, unknown upstream, cycle).

    Raised at CONSTRUCTION — a malformed DAG must never reach a runner.
    """


class ArmyRunError(RuntimeError):
    """A run failed: at least one node raised or timed out.

    Carries the full picture so a caller (or a ranking plane) can attribute the
    failure without re-reading meter rows: ``failures`` maps slug -> error text,
    ``skipped`` maps slug -> why it never ran.
    """

    def __init__(
        self, message: str, *, failures: dict[str, str], skipped: dict[str, str]
    ) -> None:
        super().__init__(message)
        self.failures = failures
        self.skipped = skipped


@dataclass(frozen=True)
class ArmyNodeSpec:
    """One node: a slug, its async job, its upstreams, its timeout."""

    slug: str
    job: JobHandler
    inputs: tuple[str, ...] = ()
    timeout_s: float = DEFAULT_NODE_TIMEOUT_S

    def __post_init__(self) -> None:
        if not self.slug or not isinstance(self.slug, str):
            raise ArmySpecError("node slug must be a non-empty string")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "timeout_s", _clamp_timeout(self.timeout_s))


# Backwards-compatible alias (the pre-Legion name for a node spec).
NodeSpec = ArmyNodeSpec


class Army:
    """A validated DAG of node jobs. Composition is data; validation is eager.

    Construction proves: slugs unique, every input names a real node, and the
    graph is acyclic (Kahn's algorithm) — after ``__init__`` returns, a runner can
    never livelock on a malformed DAG.
    """

    def __init__(self, nodes: list[ArmyNodeSpec]) -> None:
        if not nodes:
            raise ArmySpecError("army has no nodes")
        slugs = [n.slug for n in nodes]
        dupes = sorted({s for s in slugs if slugs.count(s) > 1})
        if dupes:
            raise ArmySpecError(f"duplicate node slugs: {dupes}")
        self._nodes: dict[str, ArmyNodeSpec] = {n.slug: n for n in nodes}
        known = set(self._nodes)
        for n in nodes:
            unknown = [u for u in n.inputs if u not in known]
            if unknown:
                raise ArmySpecError(
                    f"node {n.slug!r} depends on unknown nodes: {unknown}"
                )
        self._levels: list[list[str]] = self._topo_levels()

    def _topo_levels(self) -> list[list[str]]:
        """Kahn's algorithm by level: level 0 = source nodes, level k depends only
        on levels < k. Raises ArmySpecError on a cycle."""
        indegree = {s: len(n.inputs) for s, n in self._nodes.items()}
        remaining = dict(indegree)
        levels: list[list[str]] = []
        while remaining:
            level = sorted(s for s, d in remaining.items() if d == 0)
            if not level:
                raise ArmySpecError(
                    f"army DAG has a cycle; unresolvable nodes: {sorted(remaining)}"
                )
            levels.append(level)
            for s in level:
                del remaining[s]
            for s in list(remaining):
                remaining[s] = sum(
                    1 for u in self._nodes[s].inputs if u in remaining
                )
        return levels

    @property
    def levels(self) -> list[list[str]]:
        """Topo levels; nodes within one level are independent and may run
        concurrently."""
        return self._levels

    @property
    def edges(self) -> dict[str, tuple[str, ...]]:
        """slug -> upstream slugs (the machine-queryable shape of this DAG)."""
        return {s: n.inputs for s, n in self._nodes.items()}

    def node(self, slug: str) -> JobHandler | None:
        """Return the job handler for ``slug`` or None if it isn't in this army
        (so a tenant scoping a subject to a wrong slug fails to bind, close)."""
        spec = self._nodes.get(slug)
        return spec.job if spec else None

    def node_spec(self, slug: str) -> ArmyNodeSpec:
        return self._nodes[slug]

    def inputs_of(self, slug: str) -> tuple[str, ...]:
        return self._nodes[slug].inputs

    @classmethod
    def from_spec(
        cls, spec: dict[str, Any], registry: dict[str, JobHandler]
    ) -> "Army":
        """Build an army from a JSON-able spec + a handler registry.

        Spec shape::

            {"nodes": [{"slug": "a", "handler": "scout"},
                       {"slug": "b", "handler": "extract", "inputs": ["a"],
                        "timeout_s": 120}]}

        ``registry`` maps handler names to callables — names, not functions, so a
        spec stays serializable and auditable (an agent can READ what a legion
        will do before it runs).
        """
        raw_nodes = spec.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ArmySpecError("spec must contain a non-empty 'nodes' list")
        nodes: list[ArmyNodeSpec] = []
        for entry in raw_nodes:
            if not isinstance(entry, dict) or "slug" not in entry:
                raise ArmySpecError(f"malformed node entry: {entry!r}")
            handler_name = entry.get("handler")
            job = registry.get(handler_name) if handler_name else None
            if job is None:
                raise ArmySpecError(
                    f"node {entry.get('slug')!r}: unknown handler {handler_name!r} "
                    f"(registry has: {sorted(registry)})"
                )
            nodes.append(
                ArmyNodeSpec(
                    slug=entry["slug"],
                    job=job,
                    inputs=tuple(entry.get("inputs", ())),
                    timeout_s=entry.get("timeout_s", DEFAULT_NODE_TIMEOUT_S),
                )
            )
        return cls(nodes)


class PriceAlertArmy(Army):
    """Composition for one price-alert goal. Tenancy + our_price come from scope.

    Scout(keyword) -> dom-stripper -> extractor -> math-checker -> alerter,
    mirroring ORDER.md section 6's table exactly. ``EDGES`` is the single source
    of truth for this composition; the node specs are derived from it so the
    class attribute, the bus binding, and the in-process runner cannot drift.
    """

    # DAG edges (slug -> upstream slugs that must run first).
    EDGES = {
        "scout": (),
        "dom-stripper": ("scout",),
        "extractor": ("dom-stripper",),
        "math-checker": ("extractor",),
        "alerter": ("math-checker",),
    }

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
        handlers: dict[str, JobHandler] = {
            "scout": self._scout.handle,
            "dom-stripper": self._dom,
            "extractor": self._extract,
            "math-checker": self._check,
            "alerter": self._alert,
        }
        super().__init__(
            [
                ArmyNodeSpec(slug=slug, job=handlers[slug], inputs=self.EDGES[slug])
                for slug in self.EDGES
            ]
        )

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


class _NodeFailed(Exception):
    """Internal carrier: a node's failure, caught by the level gather."""


class InProcessRunner:
    """Walks an army DAG headlessly. For the milestone this is what makes a
    ``many`` order *converge* end to end with no cluster. Real in-cluster
    operation uses the same node handlers bound to NATS subjects in
    :mod:`af.cli`.

    Execution contract (Legion production pass):
      * nodes run level by level; within a level, concurrently
      * a node's input = run-level ``url`` context + the merge of ALL its
        upstreams' outputs (declared order; later inputs win collisions) +
        ``inputs`` = {upstream_slug: output} for explicit addressing
      * source nodes (no inputs) receive the full seed
      * every node is metered exactly once: ran / failed / skipped
      * any failure aborts after the in-flight level; dependents are metered
        skipped, then ArmyRunError raises with the full attribution
    """

    def __init__(
        self,
        army: Army,
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

    def _record(self, ex: Execution) -> None:
        if self._meter:
            self._meter.record(ex)

    def _record_skip(self, slug: str, blocked_by: list[str]) -> None:
        now = time.time()
        self._record(
            Execution(
                tenant_id=self._tenant_id,
                order_id=self._order_id,
                run_id=self._run_id,
                agent_slug=slug,
                status="skipped",
                started_at=now,
                finished_at=now,
                detail={"reason": f"upstream not completed: {blocked_by}"},
            )
        )

    async def _run_node(
        self, slug: str, seed: dict[str, Any], results: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        spec = self._army.node_spec(slug)
        node_input: dict[str, Any] = {}
        # The competitor url is run-level context every passed-along node should
        # carry, so the final alerter can name the source page even after earlier
        # nodes transformed its fields away.
        if seed.get("url"):
            node_input["url"] = seed["url"]
        if not spec.inputs:
            # Source nodes get the full seed (e.g. scout gets url + injected html).
            node_input.update(seed)
        else:
            # ALL upstreams thread through — declared order, later wins on key
            # collision. The pre-Legion runner used only inputs[0] and silently
            # dropped the rest of a diamond.
            for up in spec.inputs:
                node_input.update(results.get(up) or {})
            node_input["inputs"] = {up: results.get(up) or {} for up in spec.inputs}

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
            out = await asyncio.wait_for(spec.job(node_input), timeout=spec.timeout_s)
        except asyncio.TimeoutError:
            ex.status = "failed"
            ex.finished_at = time.time()
            ex.detail = {"error_kind": "timeout", "timeout_s": spec.timeout_s}
            self._record(ex)
            raise _NodeFailed(
                f"{slug}: timed out after {spec.timeout_s}s"
            ) from None
        except Exception as exc:  # noqa: BLE001 - a failing node marks the run failed
            ex.status = "failed"
            ex.finished_at = time.time()
            ex.detail = {"error_kind": "exception", "error": str(exc)}
            self._record(ex)
            raise _NodeFailed(f"{slug}: {exc}") from exc
        ex.finished_at = time.time()
        ex.detail = {
            "produced": bool(out),
            "duration_ms": round(ex.elapsed_ms(), 1),
            "inputs_from": list(spec.inputs),
        }
        self._record(ex)
        return out or {}

    async def run(self, seed: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Execute the DAG level by level and return slug -> output for every
        node that ran. Raises ArmyRunError if any node failed or timed out."""
        seed = dict(seed or {})
        results: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        skipped: dict[str, str] = {}
        levels = self._army.levels

        for i, level in enumerate(levels):
            runnable: list[str] = []
            for slug in level:
                blocked_by = [
                    u
                    for u in self._army.inputs_of(slug)
                    if u in failures or u in skipped
                ]
                if blocked_by:
                    self._record_skip(slug, blocked_by)
                    skipped[slug] = f"upstream not completed: {blocked_by}"
                else:
                    runnable.append(slug)

            outcomes = await asyncio.gather(
                *(self._run_node(s, seed, results) for s in runnable),
                return_exceptions=True,
            )
            for slug, outcome in zip(runnable, outcomes):
                if isinstance(outcome, BaseException):
                    failures[slug] = str(outcome)
                else:
                    results[slug] = outcome

            if failures:
                # Fail fast: nothing downstream of a failure runs, and every one
                # of those nodes is metered as skipped so the run is fully
                # accounted for — no silent omissions.
                for later in levels[i + 1 :]:
                    for slug in later:
                        if slug not in skipped:
                            self._record_skip(slug, sorted(failures))
                            skipped[slug] = "run aborted by earlier failure"
                raise ArmyRunError(
                    f"army run failed; failed={sorted(failures)} skipped={sorted(skipped)}",
                    failures=failures,
                    skipped=skipped,
                )
        return results
