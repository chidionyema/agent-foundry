"""agent-foundry command line.

Subcommands:
  validate  path    Validate an order JSON against order/schema.json (exit 0/1).
  plan      order   Print the army node plan a `many` order maps to (no execution).
  run       order   Run the price-alert army in-process for a given order and seed.

These call the same library the NATS worker uses, so what runs headless here is the
identical node code that runs in-cluster. No training path is reachable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .army import InProcessRunner, PriceAlertArmy
from .meter import FileMeter, Meter
from .order import Order, OrderValidationError


def _load_raw(path: str) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def cmd_validate(args: argparse.Namespace) -> int:
    raw = _load_raw(args.order)
    try:
        Order.from_dict(raw)
    except OrderValidationError as exc:
        print(f"INVALID: {exc.summary()}", file=sys.stderr)
        return 1
    print("VALID")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    raw = _load_raw(args.order)
    order = Order.from_dict(raw)
    slugs = ["scout", "dom-stripper", "extractor", "math-checker", "alerter"]
    print(
        json.dumps(
            {
                "order_id": order.order_id,
                "tenant_id": order.tenant_id,
                "bot_count": order.bot_count,
                "assembly_mode": order.assembly_mode,
                "train_gate": order.train_gate,
                "nodes": [{"slug": s} for s in slugs],
                "edges": {s: list(u) for s, u in PriceAlertArmy.EDGES.items()},
            },
            indent=2,
        )
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    raw = _load_raw(args.order)
    order = Order.from_dict(raw)
    if order.assembly_mode != "firstile":
        # train_candidate / frontier orders never execute here; no auto-train.
        print(
            f"REFUSED: order assembly.mode is {order.assembly_mode!r}; only 'firstile' runs headless.",
            file=sys.stderr,
        )
        return 2

    scope = order.scope or {}
    our_price = float(scope.get("our_price", 100.0))
    threshold = float(scope.get("threshold_pct", 10.0))
    product = scope.get("product_name", "product")
    army = PriceAlertArmy(
        our_price=our_price, threshold_pct=threshold, product_name=product
    )

    seed = {"url": scope.get("url", "https://competitor.example")}
    # html comes from --seed-html file (offline harness) or scope.html; never from
    # a hardcoded checkout path.
    if args.seed_html:
        seed["html"] = _read_text(args.seed_html)
    elif "html" in scope:
        seed["html"] = scope["html"]
    else:
        seed["html"] = ""

    meter: Meter | None = None
    fm: FileMeter | None = None
    if args.meter_dir:
        fm = FileMeter(args.meter_dir)
        meter = Meter(fm.write)

    run_id = order.run_id or f"run_{order.order_id}"
    runner = InProcessRunner(
        army, meter, tenant_id=order.tenant_id, order_id=order.order_id, run_id=run_id
    )
    results = asyncio.run(runner.run(seed))
    print(
        json.dumps(
            {
                "converged": True,
                "run_id": run_id,
                "results": {k: v for k, v in results.items()},
                "alert": results.get("alerter") or None,
            },
            indent=2,
        )
    )
    if fm is not None:
        fm.close()
    return 0


def _read_text(path: str) -> str:
    with Path(path).open("r", encoding="utf-8") as fh:
        return fh.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-foundry")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser(
        "validate", help="validate an order JSON against order/schema.json"
    )
    pv.add_argument("order", help="path to an order JSON file")
    pv.set_defaults(fn=cmd_validate)

    pp = sub.add_parser(
        "plan", help="print the army node plan for an order (no execution)"
    )
    pp.add_argument("order", help="path to an order JSON file")
    pp.set_defaults(fn=cmd_plan)

    pr = sub.add_parser(
        "run", help="run the price-alert army in-process (headless convergence proof)"
    )
    pr.add_argument("order", help="path to an order JSON file")
    pr.add_argument(
        "--seed-html",
        help="file containing competitor HTML for the Scout node (offline harness)",
    )
    pr.add_argument(
        "--meter-dir",
        help="directory to write execution metering JSONL (default: temp dir)",
    )
    pr.set_defaults(fn=cmd_run)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
