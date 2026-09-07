# Decomposer — the ONE frontier call that turns a `many` order into an N-spec DAG

Status: deterministic prompt + framing (ORDER.md section 2 and 3.2). No code beyond
the thin stub in `af/army.py`. Filter-first: the decomposer only *names* nodes and
their production mode; it never trains and never provisions a GPU.

## Role

For a `many` order (`bot_count >= 2`) the factory does **not** hand-build each bot.
Instead one single frontier call breaks the customer goal into 1..N bot specs, then
a deterministic manifest step turns those specs into the DAG the bus runs. This file
is the contract that single call is graded against.

A `one` order (`bot_count == 1`) skips decomposition entirely and uses the manifest
of a single node (ORDER.md section 2: "one order: skip the decomposer or accept its
single-node result; the manifest is one node").

## Input (what we hand the frontier call)

The validated order object (af/order.Order): `goal`, `bot_count`, `scope`. Nothing
else. The tenant id and order id are context the frontier should not invent into the
spec.

## Output contract (must parse, or the call is retried)

The frontier caller returns a single JSON array of **bot specs**, exactly
`bot_count` entries:

```jsonc
[
  {
    "slug": "skl-watcher-1",            // [A-Za-z0-9_-], unique within the order
    "job": "watch one catalog SKU and alert when price drops below X",
    "input": { "topic": "tasks.alerts.<sku_id>" },       // what it reads
    "output_mode": "function" | "frontier" | "train_candidate",
    "depends_on": []                     // slugs that must finish first (DAG edges)
  }
]
```

**Hard rules (a violation fails the call, no repair):**
- Exactly `bot_count` specs. Fewer or more => the call is NAK'd and re-run bounded.
- `output_mode` is `function` or `frontier` by default. `train_candidate` is only
  produced when the scope's traffic gates are already evidenced; a `train_candidate`
  spec is **held, never run** by this build (ORDER.md: no bot is auto-trained).
- `depends_on` must not be cyclic; the manifest step enforces a strict topological
  order and fails a cyclic spec back to the decomposer.
- Slugs are the {agent_slug} half of the subject `tasks.<tenant>.<slug>` and obey the
  same charset; a spec that breaks the charset is refused, not sanitised.

## Manifest step (deterministic, no model)

Each spec becomes, in order:

1. one catalog entity (estate Backstage) describing the node's job + I/O schema;
2. a bus subject per node (`tasks.<tenant>.<slug>`);
3. a worker binding: node → handler (function) | router entry (frontier) | held
   (train_candidate).

A pack is the DAG of these nodes: node reads `depends_on` upstream topics and after
its job pushes to its outbound topic, exactly as the price-alert army in
`af/army.py` hard-wires today.

## Why filter-first here

The decomposer is the one place a "clever" LLM writes structure. Keeping its output
to a constrained JSON spec (never free prose) means the resulting army is inspectable,
catalogued and dead-letterable — a buyer's engineer can read the manifest, not a
transcript. Training is never a spec the factory auto-runs.

## Definition of done for the decomposer

A `many` order, run through the decomposer stub + manifest on the real bus, yields
`bot_count` catalogued worker bindings that together form an acyclic DAG, and the
bound workers run headless — with no path that triggers training.
