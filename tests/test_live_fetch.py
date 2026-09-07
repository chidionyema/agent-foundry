"""Live-transport proof: the Scout node fetches over a REAL local HTTP socket (no
html injection) so fetch_page + strip + extract are proven on the actual wire path,
not just against in-memory strings."""

from __future__ import annotations

import asyncio
import http.server
import threading

from pathlib import Path

from af.nodes import Scout, dom_strip, extract_price

SEED = (
    Path(__file__)
    .resolve()
    .parent.parent.joinpath("examples/seed.competitor.html")
    .read_text()
)


def test_scout_fetches_over_real_http_then_strips_and_extracts() -> None:
    # serve the seed html body on any ephemeral port via a tiny handler
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = SEED.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    async def scenario() -> dict:
        scout = Scout()  # fetch_page real urllib transport, NOT injected html
        raw = await scout.handle({"url": f"http://127.0.0.1:{port}/p"})
        stripped = dom_strip(raw["html"])
        return extract_price(stripped["text"], product_name="Acme Widget 3000")

    try:
        extracted = asyncio.run(scenario())
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)

    assert extracted["price"] == 79.99, extracted
    assert extracted["currency"] == "USD"
