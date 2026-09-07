"""The five price-alert army nodes (ORDER.md section 6), each one narrow job.

Every node is a pure function over a validated payload dict -> output dict, so it
is unit-testable offline and deployable as a function. Transport concerns (real
HTTP, real NATS) live at the edges — never inside these cores.

The army, end to end (Scout -> DOM-Stripper -> Extractor -> Math-Checker ->
Alerter):

    Scout        fetch a competitor page (or accept injected HTML) -> {"url","html"}
    DOM-Stripper HTML -> readable text (bs4, the estate's trusted HTML lib)
    Extractor    price table / text -> strict JSON {price, currency, updated}
    Math-Checker compare extracted price vs our price -> {"cheaper": bool, delta_pct}
    Alerter      emit a structured alert when cheaper (side-effect node)

Filter-first: every mode is "function". No unit here imports or triggers training.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from bs4 import BeautifulSoup

from .worker import PermanentlyRejected, TransientError


# --------------------------------------------------------------------------- #
# Helpers shared only within this army module
# --------------------------------------------------------------------------- #


def _norm_number(s: str) -> float | None:
    """'$1,299.00' / '1 299,00' -> float; None when no number present."""
    cleaned = re.sub(r"[^\d.,]", "", s)
    cleaned = cleaned.replace(",", "").replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Node 1 — Scout : fetch a page (transport kept out so tests stay offline)
# --------------------------------------------------------------------------- #
_FETCH_TIMEOUT_S = 20
_ALLOWED_FETCH_SCHEMES = ("http", "https")


def fetch_page(url: str) -> str:
    """Real HTTP fetch. urllib.request, no new transport dependency. Raises a
    PermanentlyRejected on a disallowed scheme and a TransientError on network-level
    failure so the worker may NAK + retry."""
    from urllib.parse import urlsplit

    if urlsplit(url).scheme not in _ALLOWED_FETCH_SCHEMES:
        raise PermanentlyRejected(f"fetch: scheme of {url!r} is not http(s)")
    # S310 audited: only http/https reach here — any other scheme already raised
    req = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": "agent-foundry-scout/0.1"}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=_FETCH_TIMEOUT_S
        ) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception as exc:
        raise TransientError(f"fetch {url} failed: {exc}") from exc


class Scout:
    """The Scout bot. In the deployed image its handler calls :func:`fetch_page`;
    in tests or an offline harness the bus worker passes injected HTML via the
    order scope."""

    def __init__(self, fetch: Any = fetch_page) -> None:
        self._fetch = fetch

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise PermanentlyRejected(f"scout: bad or missing url {url!r}")
        injected = payload.get("html")
        if isinstance(injected, str) and injected.strip():
            html = injected  # offline / deterministic harness content
        elif injected is not None and not isinstance(injected, str):
            html = json.dumps(injected)
        else:
            try:
                html = await _to_async(self._fetch, url)
            except TransientError:
                raise
            except (
                Exception
            ) as exc:  # transport failure -> transient, worker NAKs + retries
                raise TransientError(f"scout fetch {url} failed: {exc}") from exc
        return {"url": url, "html": html}


async def _to_async(fn: Any, *args: Any) -> Any:
    """Run a transport callable without blocking the event loop. Handles both a
    sync blocking fetch and an already-async callable. Called exactly once."""
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)


# --------------------------------------------------------------------------- #
# Node 2 — DOM-Stripper : html -> readable text (bs4)
# --------------------------------------------------------------------------- #
def dom_strip(html: str, *, base_url: str | None = None) -> dict[str, Any]:
    """Strip HTML to a linear readable text (the estate's bs4), dropping script,
    style, nav, footer, and comment noise. Returns dict with a 'text' field."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # collapse 3+ blank lines to one to keep payloads tidy for the extractor
    text = "\n".join(line for line in text.splitlines() if line.strip())
    return {"url": base_url, "text": text, "length": len(text)}


# --------------------------------------------------------------------------- #
# Node 3 — Extractor : price from text/table -> strict JSON
# --------------------------------------------------------------------------- #
def extract_price(text: str, *, product_name: str | None = None) -> dict[str, Any]:
    """Pulls the first plausible product price out of stripped page text.

    This is intentionally a *deterministic heuristic* — filter-first, no frontier
    call needed for a parser that must not silently drift. It returns strict JSON
    the Math-Checker consumes.
    """
    if not text or not text.strip():
        raise PermanentlyRejected("extract-price: empty text, nothing to parse")

    # Collect every currency-prefixed number in first-seen order, then prefer the
    # line matching the product name when one is supplied.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    order_of_appearance: list[tuple[float, str, str]] = []
    for ln in lines:
        for m in re.finditer(r"([£$€¥]|(?:USD|EUR|GBP))\s*(\d[\d.,]{1,12})", ln):
            cur = m.group(1)
            price = _norm_number(m.group(2))
            if price is not None:
                order_of_appearance.append((price, cur, ln))

    if not order_of_appearance:
        raise TransientError(
            "extract-price: no currency-prefixed number found (page may not be loaded yet)"
        )

    # If a product keyword is given, prefer the line containing it; else first.
    price, cur, matched_line = order_of_appearance[0]
    if product_name:
        probe = product_name.lower()
        for p, c, ln in order_of_appearance:
            if probe in ln.lower():
                price, cur, matched_line = p, c, ln
                break

    norm_cur = {"£": "GBP", "$": "USD", "€": "EUR", "¥": "JPY"}.get(cur, cur)
    return {
        "price": price,
        "currency": norm_cur,
        "updated": None,
        "matched_line": matched_line[:200],
    }


# ours is quoted in one currency for this army; leave overridable per tenant scope.
_CURRENCY_OUR = "USD"


# --------------------------------------------------------------------------- #
# Node 4 — Math-Checker : compare against our price
# --------------------------------------------------------------------------- #
def math_check(
    extracted: dict[str, Any], our_price: float, *, threshold_pct: float = 10.0
) -> dict[str, Any]:
    """True when the competitor is at least ``threshold_pct`` cheaper than ours.

    Returns an alert-ready comparison. Currency mismatch between "us" and "them"
    is treated as UNKNOWN pricedelta (does not alert) rather than silently alerting
    on incomparable numbers — a guardrail, not a guess.
    """
    their = extracted.get("price")
    cur = extracted.get("currency")
    baseline_currency = _CURRENCY_OUR
    if not isinstance(their, (int, float)):
        return {
            "comparable": False,
            "reason": "no_price",
            "delta_pct": None,
            "cheaper": False,
        }
    if cur and baseline_currency and cur != baseline_currency:
        return {
            "comparable": False,
            "reason": f"currency_mismatch ours={baseline_currency} theirs={cur}",
            "delta_pct": None,
            "cheaper": False,
        }
    if our_price <= 0:
        return {
            "comparable": False,
            "reason": "our_price_invalid",
            "delta_pct": None,
            "cheaper": False,
        }
    delta_pct = (our_price - their) / our_price * 100.0  # positive => theirs is cheaper
    cheaper = delta_pct >= threshold_pct
    return {
        "comparable": True,
        "reason": "below_threshold" if cheaper else "at_or_above_threshold",
        "delta_pct": round(delta_pct, 2),
        "their_currency": cur,
        "cheaper": cheaper,
    }


# ours is quoted in one currency for this army; leave overridable per tenant scope.
_CURRENCY_OUR = "USD"


# --------------------------------------------------------------------------- #
# Node 5 — Alerter : emit alert row (pure shape; transport injected at edge)
# --------------------------------------------------------------------------- #
def alert_core(
    comparison: dict[str, Any], *, product: str, competitor_url: str
) -> dict[str, Any] | None:
    """Returns an alert payload ONLY when the comparison says cheaper is true.
    None when the competitor is not under threshold -> no message sent at all."""
    if not comparison.get("cheaper"):
        return None
    return {
        "title": f"[price-alert] {product} dropped below threshold",
        "product": product,
        "competitor_url": competitor_url,
        "delta_pct": comparison.get("delta_pct"),
        "currency": comparison.get("their_currency"),
    }
