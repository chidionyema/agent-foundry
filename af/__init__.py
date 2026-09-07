"""agent-foundry — a factory (and runtime) that turns an ordered goal into a
running army of specialised micro-node jobs over the estate's single NATS bus.

This package is filter-first: the default production mode for every bot is a plain
function or a single frontier call. Nothing here imports vLLM, Unsloth, or any
training path. A train_candidate on an order is held and gated, never executed by
this code.
"""

__version__ = "0.1.0"
