# agent-foundry — product spec (founder sign-off draft)

Status: DRAFT for founder sign-off. Nothing is built. This file is the contract for the
first commit; code and tests begin only after the founder approves it.

## 1. What this product is — in one line

A factory (and its runtime) that lets someone **order an army of highly-specialised
micro-agents — "agents" and "bots" — that assemble themselves to hit a stated goal**, on the
estate's existing single platform layer, for near-zero marginal cost.

## 2. What this product is NOT (the load-bearing corrections)

The founder's earlier messages leaned on a "train every micro-agent as a fine-tuned 1B LoRA
for $0.20 each." That headline does not survive contact with reality, and this spec refuses to
repeat it:

1. **Most of the manufactured "specialties" are already deterministic.** DOM-stripping,
   diff-review, JSON-repair, route-switching ("BILLING/TECH_SUPPORT/SALES"), payload-building,
   SERP extraction — every one of these is a **pure function or a one-shot frontier call**, not
   a task that needs a trained 1B model. Training a micro-model to do what a hand-written parser
   does flawlessly and ~5,000x cheaper is reinventing the wheel and doing it worse (a 1% error on
   a routing switch silently destroys 1% of inbound).
2. **$0.20/agent is training cost on free-then-finite GPUs and omits the runtime and the
   factory's real engines** (evaluation, retrain-on-fail, versioning). A factory is not free; its
   true per-adapter all-in is $0.30-$0.60 and its real cost is the orchestration, not the GPU.
3. **A router trained by a router is a contradiction.** If the System-1 routing is the
   latency/cost-critical front, you want it small and deterministic/frontier-single-shot, and you
   only *distil* it into a 1B adapter once live traffic proves the shape is stable and hot
   enough to pay for the distillation.

### The architecture this spec commits to: HYBRID, filter-first

Every ordered micro-agent is produced by a **2-stage pipeline that defaults to cheap and only
pays for training when a number justifies it**:

- **Stage 1 — Function or single frontier call (default).** If the micro-task is a stable
  input→output transform (parse, route, repair, extract, alert), it ships as a hand-written
  function or one frontier call on the existing model router. Zero training, zero GPU, zero
  new infrastructure.
- **Stage 2 — Distil a 1B LoRA only when two gates both pass:** (a) the task is high-frequency
  (thousands of invocations/day) AND latency/cost-critical enough that a frontier call hurts,
  AND (b) live telemetry shows the Stage-1 input→output shape is stable over a full cycle (no
  perpetual drift). Stage 2 uses frontier-generated synthetic data + Unsloth QLoRA on free
  Kaggle T4 + GGUF export — the founder's stack — but only as the *finishing* engine, never as
  the default.

The result is the founder's intended economics (**one army for ~$0-1 one-time, ~$0-20/month to
run**) delivered by not paying for training that was never needed, and paying only where the
traffic justifies it.

## 3. The two train-worthy adapters on day one (evidence-gated, not speculative)

From the estate's own Otto shape (ingress-router / gateway / guardrail), the two micro-tasks
that *survive* the Stage-2 filter and are worth a real 1B LoRA once volume proves it:

1. **The Guardrail / human-gate verifier.** Distinguishing "chat" vs "database query" vs
   "destructive command" at the front. High-frequency, latency-critical, and the cost of a wrong
   answer is bounded only if the model is small and fast AND the large model still double-checks
   the destructive branch. Distil only after a week of logged routing decisions prove the shape.
2. **The Ingress Router.** Telegram/whatsapp/webhook message → intent tag, for the hot path.
   Same gates.

These two are PROPOSED, not shipped. They become real work items only when this spec is signed
and live traffic justifies Stage 2.

## 4. Where it lives — and the one-platform rule

`agent-foundry` is a **product**, sibling to `hermes-v2`. It does NOT carry its own copy of any
platform layer:

- The **message bus** is the estate's existing row (the config already names NATS JetStream /
  Redis Streams on the same compute the founder prefers) — agent-foundry subscribes/publishes to
  that, never a second broker.
- **Model routing / traces / identity / secrets** come from the estate's one-of-each rows. No
  second router, no second secret store.
- The factory's output (an army) is described by **catalog entities** in the estate Backstage
  catalog, like any onboarded workload.

A buyer's engineer doing diligence reads this file and sees: one platform, and a product that is
a thin, well-designed client of it.

## 5. Ordered-army lifecycle (the product's core loop)

1. **Order** — a goal in natural language ("scrape competitor pricing daily, alert me if mine is
   10% more expensive").
2. **Decompose** — one smart frontier call breaks the goal into 4-6 micro-tasks, each tagged
   as function / frontier-call / train-worthy (never auto-trained).
3. **Manifest** — each micro-task becomes a catalog entity + a config on the bus (topic,
   input/output schema). An army is a DAG of these, not free-talking agents.
4. **Run** — each node pulls its input topic, does its one job, pushes the next topic. Critic
   bot catches failures and re-queues (bounded retries, dead-letter after N).
5. **Evolve** — telemetry decides Stage-2 promotion. Nothing trains without a number.

## 6. Example first army (the price-alerting goal, end to end)

| micro-agent | job | production mode |
|---|---|---|
| Scout | fetch competitor pages on schedule | function (cron + fetch) |
| DOM-Stripper | HTML → Markdown | function (readability) |
| Extractor | price table → strict JSON | one frontier call |
| Math-Checker | compare prices; true/false alert | function |
| Alerter | emit alert to the estate's alert bus | function |

Cost to produce this army: near-$0 (all mode-1). Runtime: the estate's existing compute. This is
the FIRST deliverable once signed — it proves the factory shape with no training bill.

## 7. Total cost — the straight answer to "what's this new work cost"

| Tier | scope | one-time | monthly run |
|---|---|---|---|
| A | prove factory + one army (mode-1 functions + single frontier calls) | $0 | $0 on the estate's free/CPU node; ~$15-30/mo only if sub-100ms GPU needed |
| B | the two train-worthy adapters, IF traffic gates pass | ~$0.30-0.60 each all-in (data-gen + eval + retrains) | covered by A's node |
| C | full parallel army factory at scale | orchestration dev-hours (not $) | ~$15-30/mo for one T4/4090 spot if phone-off-desktop concurrency demands |

Deliberately NOT repeated: the "$0.20/agent, one dollar for a 5-squad army" framing. The honest
Tier-A number is ~$0 and the honest Tier-B per-adapter all-in is ~$0.30-0.60 once evaluation and
retrains are counted.

## 8. Founder decisions needed to proceed past this draft

1. Confirm the **one-platform rows the factory rides on** (bus = which existing broker name;
   router/config from the estate rows) — named here so the first code talks to the right one.
2. Confirm the **first army to build for the buyer demo** is the price-alerting goal in §6 (or
   name a different goal).
3. Confirm the product repo is **public-facing / buy-buildable now** (README/licence/catalog
   entity) or internal-until-demo (private).

None of these blocks writing the spec into the repo as the first commit; 1 blocks code.
