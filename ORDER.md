# agent-foundry — order & assembly contract (filter-first)

Status: FIRST IMPLEMENTATION ARTIFACT; supersedes SPEC.md §2 as the build authorisation for the
order path. The founder approved "swarm runtime first, filter-first" and confirmed the product
core: **a customer orders 1 or many bots; the factory assembles them into a running coordinated
army on the estate bus.**

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

Deliberately deferred (NEVER built until traffic gates pass): vLLM multi-LoRA, Unsloth foundry,
S3 adapter store, the Postgres tenant/billing schema's `agents` training table. One thing rides
ahead: the `tenants` + `task_executions` metering rows, because metering is platform, and the
estate's DB is the estate's DB (one DB row), not a second store.

## 4. Guardrails for order fulfilment

- Poison-pill handling and `msg.term()` on schema failure, ack-heartbeat for long jobs, NAK+delay
  on transient error, DLQ after 3 — exact mechanic from the spec's Phase 4 worker.
- Cross-tenant isolation via gateway-validated `tenant_id`; every subject names the tenant:
  `tasks.<tenant_id>.<agent_slug>`.
- No bot is ever auto-trained; nothing in `order` or `assemble` triggers a GPU.

## 5. Definition of done for the first milestone

A `many` order for the price-alert army, submitted over the bus, converges to all five bots
**running as functions** with a completed end-to-end run (a page fetched, cleaned, price parsed,
compared, alert emitted) recorded in metering — with NO vLLM and NO Unsloth in the path.
