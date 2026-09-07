"""Order loading and schema validation — the input gate of the factory.

Every downstream step consumes a validated ``Order`` object (ORDER.md section 1).
Nothing downstream should re-parse raw JSON; if it cannot get a validated Order it
is a caller bug, and nothing about a schema violation reaches the bus.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

# The order JSON Schema rides in the repo at order/schema.json and is the single
# source of truth for what a valid order is. Loading it from the checkout makes
# the code never re-declare the shape from memory (no drift between gate and doc).
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "order" / "schema.json"


def load_schema(path: Path = _SCHEMA_PATH) -> dict[str, Any]:
    """Load the order JSON Schema. Raises if the file is absent or not JSON."""
    with path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    # Validate the schema itself up front so a typo fails loudly here, not later.
    Draft202012Validator.check_schema(schema)
    return schema


@dataclass(frozen=True)
class Order:
    """A validated order. Immutable once constructed through :func:`from_dict`."""

    order_id: str
    tenant_id: str
    goal: str
    bot_count: int
    scope: dict[str, Any]
    assembly_mode: str
    train_gate: str
    created_at: str
    run_id: str | None = None

    @property
    def is_many(self) -> bool:
        """True when the order asks for more than one bot (ORDER.md: any N>=1; a
        many order MUST emit a DAG with >= 2 nodes that share topics)."""
        return self.bot_count >= 2

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], schema: dict[str, Any] | None = None
    ) -> "Order":
        schema = schema or load_schema()
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
        if errors:
            raise OrderValidationError(errors)
        assembly = raw["assembly"]
        return cls(
            order_id=raw["order_id"],
            tenant_id=raw["tenant_id"],
            goal=raw["goal"],
            bot_count=raw["bot_count"],
            scope=raw["scope"],
            assembly_mode=assembly["mode"],
            train_gate=assembly["train_gate"],
            created_at=raw["created_at"],
            run_id=raw.get("run_id"),
        )


class OrderValidationError(ValueError):
    """Raised when an order document fails the order/schema.json gate.

    A worker that decodes raw JSON into an Order and catches this error treats the
    inbound message as a poison pill and TERMINATES it (msg.term()) rather than
    retrying — an undecodable order will never become decodable by redelivery.
    """

    def __init__(self, errors: list[Any]) -> None:
        self.errors = errors
        lines = []
        for err in errors:
            where = ".".join(str(p) for p in err.path) or "<root>"
            lines.append(f"{where}: {err.message}")
        super().__init__("; ".join(lines))

    def summary(self) -> str:
        return str(self)
