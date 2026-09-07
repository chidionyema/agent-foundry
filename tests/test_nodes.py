"""Node function tests — every bot in the army behaves per its one job. Grading
parsed structure/value, never prose (R76)."""

from __future__ import annotations

import asyncio

import pytest

from af.nodes import (
    Scout,
    alert_core,
    dom_strip,
    extract_price,
    math_check,
)


# --------------------------------------------------------------------------- #
# Scout / Dom-Stripper
# --------------------------------------------------------------------------- #
def test_dom_strip_removes_script_and_noise() -> None:
    html = "<html><head><title>t</title></head><body>"
    html += "<script>var x=1;</script><style>.a{}</style><nav>menu</nav>"
    html += "<h1>Acme Widget</h1><p>Price 19.99</p>"
    html += "<footer>© Acme</footer></body></html>"
    out = dom_strip(html, base_url="https://x")
    assert "script" not in out["text"]
    assert "Price 19.99" in out["text"]
    assert out["url"] == "https://x"
    assert out["length"] > 0


def test_dom_strip_empty_html_has_zero_length() -> None:
    out = dom_strip("", base_url=None)
    assert out["text"] == ""
    assert out["length"] == 0


def test_scout_accepts_injected_html_offline() -> None:
    scout = Scout(
        fetch=lambda url: (_ for _ in ()).throw(RuntimeError("must not fetch"))
    )
    out = asyncio.run(scout.handle({"url": "https://c.example", "html": "<b>hi</b>"}))
    assert out["html"] == "<b>hi</b>"
    assert out["url"] == "https://c.example"


def test_scout_rejects_non_http_url() -> None:
    from af.worker import PermanentlyRejected

    scout = Scout()
    with pytest.raises(PermanentlyRejected):
        asyncio.run(scout.handle({"url": "file:///etc/passwd"}))


def test_scout_fetch_network_failure_is_transient() -> None:
    from af.worker import TransientError

    def boom(_url: str) -> str:
        raise OSError("connection refused")

    scout = Scout(fetch=boom)
    with pytest.raises(TransientError):
        asyncio.run(scout.handle({"url": "https://c.example"}))


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #
def test_extract_price_pulls_first_currency_number() -> None:
    text = "Acme Widget\nOur low low price: $1,299.00 free shipping"
    out = extract_price(text)
    assert out["price"] == 1299.0
    assert out["currency"] == "USD"


def test_extract_price_prefers_line_with_product_name() -> None:
    text = "Sale $5.00\nAcme Widget Special Edition $149.99"
    out = extract_price(text, product_name="Acme Widget")
    assert out["price"] == 149.99


# --------------------------------------------------------------------------- #
# Math-Checker
# --------------------------------------------------------------------------- #
def test_math_check_detects_cheaper_below_threshold() -> None:
    comp = math_check(
        {"price": 80.0, "currency": "USD"}, our_price=100.0, threshold_pct=10.0
    )
    assert comp["cheaper"] is True  # 20% cheaper
    assert comp["delta_pct"] == 20.0
    assert comp["comparable"] is True


def test_math_check_no_alert_within_threshold() -> None:
    comp = math_check(
        {"price": 95.0, "currency": "USD"}, our_price=100.0, threshold_pct=10.0
    )
    assert comp["cheaper"] is False  # only 5% cheaper
    assert comp["delta_pct"] == 5.0


def test_math_check_currency_mismatch_is_not_comparable() -> None:
    comp = math_check(
        {"price": 50.0, "currency": "EUR"}, our_price=100.0, threshold_pct=10.0
    )
    assert comp["comparable"] is False
    assert comp["cheaper"] is False
    assert "currency_mismatch" in comp["reason"]


def test_math_check_missing_price_is_not_comparable() -> None:
    comp = math_check({"price": None, "currency": None}, our_price=100.0)
    assert comp["comparable"] is False
    assert comp["cheaper"] is False


# --------------------------------------------------------------------------- #
# Alerter
# --------------------------------------------------------------------------- #
def test_alerter_emits_payload_when_cheaper() -> None:
    alert = alert_core(
        {"cheaper": True, "delta_pct": 20.0, "their_currency": "USD"},
        product="Acme Widget",
        competitor_url="https://c.example",
    )
    assert alert is not None
    assert alert["delta_pct"] == 20.0
    assert alert["product"] == "Acme Widget"


def test_alerter_emits_nothing_when_not_cheaper() -> None:
    alert = alert_core(
        {"cheaper": False, "delta_pct": 2.0},
        product="Acme Widget",
        competitor_url="https://c.example",
    )
    assert alert is None
