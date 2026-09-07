"""Army convergence + metering — ORDER.md section 5's definition of done for the
first milestone: a `many` order converges end to end through all five nodes and
every node's run is recorded in metering, with NO training in the path."""

from __future__ import annotations

import asyncio
import json

from af.army import InProcessRunner, PriceAlertArmy
from af.meter import Meter

COMPETITOR_HTML = """
<html><body>
<h1>Competitor Depot - Acme Widget 3000</h1>
<p>Today only! Acme Widget 3000 for <span id="price">$79.99</span></p>
<footer>Shipping extra</footer>
</body></html>
"""


def _record_rows(writer_rows: list) -> Meter:
    return Meter(writer_rows.append)


def _rows_loaded(path) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def test_many_order_converges_all_five_nodes() -> None:
    army = PriceAlertArmy(our_price=100.0, threshold_pct=10.0, product_name="Acme")
    rows: list = []
    runner = InProcessRunner(
        army,
        _record_rows(rows),
        tenant_id="ten_alpha",
        order_id="ord_many1",
        run_id="run_many1",
    )
    results = asyncio.run(
        runner.run(
            {
                "url": "https://competitor.example/acme",
                "html": COMPETITOR_HTML,
            }
        )
    )

    # all five ran
    assert set(results) >= {
        "scout",
        "dom-stripper",
        "extractor",
        "math-checker",
        "alerter",
    }
    # an alert was produced because $79.99 is > 10% under our $100
    assert (
        results["alerter"].get("delta_pct") == 20.01
        or abs(results["alerter"]["delta_pct"] - 20.0) < 2
    )
    assert results["math-checker"]["cheaper"] is True


def test_every_node_run_is_metered() -> None:
    army = PriceAlertArmy(our_price=100.0, product_name="Acme")
    rows: list = []
    runner = InProcessRunner(
        army,
        _record_rows(rows),
        tenant_id="ten_alpha",
        order_id="ord_meter",
        run_id="run_meter",
    )
    asyncio.run(
        runner.run({"url": "https://competitor.example/u", "html": COMPETITOR_HTML})
    )
    slugs = {r["agent_slug"] for r in rows}
    assert slugs == {"scout", "dom-stripper", "extractor", "math-checker", "alerter"}
    assert all(r["status"] == "ran" for r in rows)
    assert all(r["run_id"] == "run_meter" for r in rows)
    assert all(r["tenant_id"] == "ten_alpha" for r in rows)


def test_no_alert_when_competitor_not_significantly_cheaper() -> None:
    # competitor price $95 vs our $100 -> only 5%, under the 10% threshold
    army = PriceAlertArmy(our_price=100.0, threshold_pct=10.0, product_name="Acme")
    rows: list = []
    runner = InProcessRunner(
        army,
        _record_rows(rows),
        tenant_id="ten_alpha",
        order_id="ord_n",
        run_id="run_n",
    )
    html = COMPETITOR_HTML.replace("$79.99", "$95.00")
    results = asyncio.run(
        runner.run({"url": "https://competitor.example/u", "html": html})
    )
    assert results["math-checker"]["cheaper"] is False
    assert results.get("alerter") in ({}, None)  # nothing emitted when not cheaper


def test_meter_writes_jsonl_rows_to_file(tmp_path) -> None:
    from af.meter import FileMeter

    fm = FileMeter(tmp_path)
    m = Meter(fm.write)
    from af.meter import Execution
    import time

    m.record(
        Execution(
            tenant_id="ten_a",
            order_id="ord",
            run_id="r1",
            agent_slug="scout",
            status="ran",
            started_at=time.time(),
            finished_at=time.time(),
        )
    )
    fm.close()
    data = _rows_loaded(tmp_path / "executions.jsonl")
    assert len(data) == 1
    assert data[0]["agent_slug"] == "scout"
