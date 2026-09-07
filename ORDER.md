# agent-foundry — order & assembly contract (dual-mode: make-to-order + make-to-stock)

Status: FIRST IMPLEMENTATION ARTIFACT; supersedes SPEC.md §2 as the build authorisation for the
order path.

**Dual-Mode flip (founder decision 2026-09-07, overrides the earlier filter-first sign-off):** the
factory serves BOTH flows on one platform — Make-to-Order (a tenant orders 1..N bots assembled
into a coordinated army on the estate bus) AND Make-to-Stock (the factory invents a reusable agent,
trains a cheap 1B LoRA once, and lists it on a storefront sellable to many tenants). Products are
"Swarm Templates": a strong frontier brain for research/reasoning + a distilled cheap LoRA for
mechanical output, routed over the estate NATS bus. A store-bought template is the same composite as
an ordered one — the buyer just gets it through the storefront row, not a fresh frontier call (see
§7 Marketplace).

## 0. The confirmed product core

- **Order.** A tenant (the customer) submits a goal plus a count/scope: order ONE bot, or order a
  PACK of N bots — **N is unbounded and arbitrary (1, 2, 9, 9000 — whatever the customer asks).**
  The count axis is NOT "one vs many": it is simply `N >= 1`, and the SAME assembly path serves
  every N. What varies with N is bus fan-out and worker replication, never the ordering model.
  Examples:
  - "bot_count": 1  — "a DOM-stripper that turns any product page into Markdown."
  - "bot_count": 9000 — "an army of 9000 SKU-watchers that watch every one of a catalog's rows."

### What "one bot" IS (resolved) — a capability-node, not a hosted persona

A counted bot is **one named job with a schema'd input->output** (a DOM-stripper, a JSON-repair,
an extractor, a watcher). It is NOT a bespoke "character" deployment. 9000 bots stays sane because
9000 nodes run on a **fixed set of engines** (function / frontier / LoRA) over the bus — a scaling
question (bus fan-out, worker replicas), never 9000 bespoke services. A user-facing agent (the
founder talks to it via portal/telegram) is a **thin delivery shell over one or more nodes** — a
channel, not an extra counted bot.
- **Assembly.** Every ordered bot is produced filter-first: a **function** or **single frontier
  call** by default; a **distilled 1B LoRA** only when two traffic gates pass (see SPEC.md §2).
- **Execution.** The assembled bots are nodes on the estate's NATS JetStream bus — the one bus
  row (`#event-bus`, `nats://nats.event-bus.svc:4222`). They talk in structured JSON over
  subjects, never conversational natural language.

## 1. The order object (the product's input contract)

A customer order is a JSON document. It is the one schema every downstream step consumes:

```jsonc
{
  "order_id": "ord_<ulid>",
  "tenant_id": "ten_<...>",
  "goal": "scrape 5 competitor sites daily, alert if mine is 10% dearer", // the single objective
  "bot_count": 9000,             // ANY integer >= 1. Unbounded. 1, 2, 9, 9000 are one code path.
  "scope": { "urls": ["...5 sites..."], "comparison_basis": "price", "threshold_pct": 10 },
  "assembly": { "mode": "firstile", "train_gate": "deferred" }, // filter-first; no auto-train
  "created_at": "<iso8601>"
}
```

Invariants every step must hold:
- `order_id` and `tenant_id` are tenant-assigned; `tenant_id` is validated against the API key by
  the gateway (Phase 3). A tenant can only act on its own order. For `N >= 9000`-scale orders the
  subject names the tenant and the bot slug, never a count the bus must parse.
- `bot_count` is an arbitrary positive integer `>= 1`, never a two-valued "one/many" enum.
  `bot_count > 1` is handled by the same assembly as `bot_count == 1`, repeated across the bus
  fan-out. Treating any specific N as a distinct code path is a defect.
- `assembly.mode` is ALWAYS `firstile` today; `train_gate` makes concrete one "which LoRA we would
  distil, and the gating metric" so the factory does not silently train.

## 2. Assembly pipeline (order -> running army)

```
order (1 or many)
   -> [decomposer]    frontier call: goal -> 1..N bot specs (each: slug, job, input_schema,
                      output_schema, mode[function|frontier|train_candidate])
   -> [manifest]      1 order -> N catalog entities; each bot -> array of bus subjects + schemas;
                      a pack is an explicit DAG (node reads topic_i, writes topic_j)
   -> [provisioner]   materialises each bot: function = import/pod; frontier = router entry;
                      train_candidate = held, gated, NOT run
   -> [run]           each node pulls its input topic, does its one job, pushes the next topic;
                      critic bot catches failure, re-queues (bounded), dead-letter after N
```

- **one** order: skip the decomposer or accept its single-node result; the manifest is one node.
- **many** order: the decomposer EXECUTES and MUST emit a DAG with ≥2 nodes that share topics.

## 3. What ships in this first build (headless, no GPU)

The founder approved swarm-runtime-first. So this repo first delivers, in order:

1. `order/<schema>.json` — the order object above as a validated JSON Schema (the input gate).
2. `assemble/decomposer.md` + a thin stub — the prompt + tool framing for the ONE frontier call
   that turns a `many` order into an N-spec DAG; deterministic, no training.
3. `bus/` — the worker/dead-letter/critic mechanics bound to `nats://nats.event-bus.svc:4222`
   (mirrors otto-gateway's own `OTTO_NATS_URL`), the subjects the pack uses, and the schema-
   validation repair loop from the spec's Phase 4 worker.
4. A `many` example pack = the SPEC §6 price-alert army wired end to end.

Deliberately deferred UNTIL the HF execution environment is proven (see below): the LIVE push of a
trained QLoRA to the Hugging Face hub and the storefront commerce rows. The training spine itself
generator.py + foundry_trainer.py building a small QLoRA on HF free GPU is now IN scope (founder
Dual-Mode flip 2026-09-07) — no longer "NEVER built". Still deferred: vLLM multi-LoRA serving,
Unsloth foundry beyond the HF PoC, S3 adapter store, commercial billing. One thing rides ahead
unchanged: the `tenants` + `task_executions` metering rows, because metering is platform, and the
estate's DB is the estate's DB (one DB row), not a second store.

## 4. Guardrails for order fulfilment

- Poison-pill handling and `msg.term()` on schema failure, ack-heartbeat for long jobs, NAK+delay
  on transient error, DLQ after 3 — exact mechanic from the spec's Phase 4 worker.
- Cross-tenant isolation via gateway-validated `tenant_id`; every subject names the tenant:
  `tasks.<tenant_id>.<agent_slug>`.
- No bot is ever auto-trained withOUT an explicit make-to-stock Publish decision; the headless
  first milestone and a Make-to-Order run never trigger a GPU. Only the foundry training spine
  (founder Approved on a Swarm Template) exercises HF free GPU, and only after the §6 seam is set.

## 5. Definition of done for the first milestone

A `many` order for the price-alert army, submitted over the bus, converges to all five bots
**running as functions** with a completed end-to-end run (a page fetched, cleaned, price parsed,
compared, alert emitted) recorded in metering — with NO vLLM and NO Unsloth in the path.

## 6. HF execution environment (policy-clean, estate-secret seam)

Training runs on Hugging Face free GPU, NOT this laptop (no GPU here). The HF credential never
touches this repo or a shell environment: it is read from a mounted secret file the operator
declares, refusing to run when absent. Mirrors otto-gateway exactly — Kyverno forbids
env.valueFrom.secretKeyRef / envFrom.secretRef, so the token arrives as a mounted file and is read
via a `*_FILE` env, never as a pod env var or console paste (LAW 46/52/54). Rotation via
`bin/idp-vault-put --merge`, never a chat, manifest or log. agent-foundry holds no HF key.

## 7. Marketplace (make-to-stock) — resolved schema, builds on the train-capability node

A Swarm Template is the sellable composite of a strong-frontier-brain prompt + a distilled cheap
LoRA for the mechanical output + the NATS routing/swarm wiring. Selling an agent to the Nth
customer costs ~zero new infra because the LoRA is trained once and hot-loaded; the marginal sale
is a storefront row, not a new train.

Private vs public rides the ONE `agents` registry (a capability node is real whether it was ordered
by a tenant, or invented by the factory and listed):

- A **private** agent (Make-to-Order): trained/generated on a tenant's behalf, and only that
  `tenant_id` may reach its LoRA. `tenant_id` NOT NULL, `is_public` FALSE.
- A **marketplace** agent (Make-to-Stock): built by the factory on a Publish decision (founder
  approved), owned by the system not a tenant. `tenant_id` IS NULL (system-owned), `is_public`
  TRUE, plus `price_tier`.
- Buying does NOT train. A storefront purchase inserts a row into `tenant_subscriptions`
  (tenant_id → the pre-trained adapter), which vLLM hot-loads. `tenant_subscriptions` and the sale
  are real but only reachable after §6's HF environment is live; until then the schema is declared
  and the spine (generator.py + foundry_trainer.py) is written and unit-verifiable, not yet pushing
  to a hub.
