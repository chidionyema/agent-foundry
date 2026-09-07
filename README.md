# agent-foundry

A factory (and its runtime) that lets someone **order an army of highly-specialised
micro-agents — agents and bots — that assemble themselves to hit a stated goal**, on the
estate's single platform layer, for near-zero marginal cost.

Product repo — sibling to `hermes-v2`. A **client** of the estate's platform rows (one bus, one
router, one catalog), never a second copy of a platform layer.

## Status

FIRST BUILD DONE (filter-first headless milestone, ORDER.md section 3 + 5). Docs that
lock scope: `SPEC.md` + `ORDER.md`; code under `af/`. Verdict: converges a `many` price-alert
order end to end over five function nodes and meters every execution — no vLLM, no Unsloth,
no training path.

## Run it

```bash
./verify.sh                     # unit tests + headless convergence + metering proof (exit 0 = done)
./.venv/bin/python -m af.cli validate examples/order.price-alert.json
./.venv/bin/python -m af.cli plan examples/order.price-alert.json
./.venv/bin/python -m af.cli run examples/order.price-alert.json --seed-html examples/seed.competitor.html --meter-dir /tmp/af-meter
```

The price-alert army (`af/army.py`, `af/nodes.py`): Scout -> DOM-Stripper -> Extractor ->
Math-Checker -> Alerter. Worker mechanics (`af/worker.py`): poison-pill `term()`, NAK+delay
for transient errors, dead-letter after the ceiling, Ack-heartbeat via `in_progress()` for
long jobs, tenant-scoped subjects (`tasks.<tenant>.<slug>`, `af/bus.py`). The in-cluster bus
seam is `af/serve.py` against the estate NATS row (`OTTO_NATS_URL`, default
`nats://nats.event-bus.svc:4222`).

## What is deliberately NOT built here

vLLM multi-LoRA, Unsloth foundry, S3 adapter store, and the `agents` training row are deferred
to WHEN live traffic gates pass (ORDER.md section 3). No bot is ever auto-trained; nothing in
here triggers a GPU.
