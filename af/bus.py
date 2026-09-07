"""Bus subject naming and the NATS/JetStream settlement seam.

Cross-tenant isolation rule (ORDER.md section 4): *every subject names the tenant*
and never a count the bus must parse. All traffic for a bot lives under
``tasks.<tenant_id>.<agent_slug>`` and its dead letter under
``tasks.<tenant_id>.<agent_slug>.dlq``. A tenant can only ever bind to its own
subjects; the class that builds subject strings therefore takes nothing but a
tenant id and a slug and validates both so a tenant id can never leak into another
tenant's namespace.

The ``Settler`` protocol is the seam that keeps the worker mechanics testable
without a live cluster: the production adapter hands the real nats-py JetStream
``Msg`` setter methods to the worker, while tests hand a recorder.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

# Slug charset kept strict: it appears inside subjects and is validated by the
# gateway (Phase 3) against tenant + agent_slug, so no characters the NATS subject
# grammar or an injection could surprise. Mirrors {tenant_id}--{agent_slug} shape
# used elsewhere for model identifiers but routed per-tenant here.
_SUBJECT_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_TASKS_PREFIX = "tasks"


def _check_part(part: str, kind: str) -> str:
    if not _SUBJECT_PART.match(part):
        raise ValueError(f"Invalid {kind} {part!r}: only [A-Za-z0-9_-] allowed")
    return part


class Subject:
    """Builds tenant-scoped NATS subjects. Stateless; every method pure."""

    @staticmethod
    def input(tenant_id: str, agent_slug: str) -> str:
        """The topic a bot pulls its work from: tasks.<tenant>.<slug>."""
        t = _check_part(tenant_id, "tenant_id")
        s = _check_part(agent_slug, "agent_slug")
        return f"{_TASKS_PREFIX}.{t}.{s}"

    @staticmethod
    def dlq(tenant_id: str, agent_slug: str) -> str:
        """Dead-letter subject for a bot, named in the SAME tenant namespace."""
        return f"{Subject.input(tenant_id, agent_slug)}.dlq"


@runtime_checkable
class MsgBody(Protocol):
    """The minimal settlement surface the worker needs from an inbound message.

    Mirrors the nats.py JetStream Msg methods that ORDER.md section 4 names:
    poison-pill term(), NAK+delay on transient error, in_progress() heartbeat for
    long jobs. ``data`` is the raw payload bytes.
    """

    subject: str
    data: bytes
    reply: str

    def ack(self) -> None: ...
    def ack_sync(self) -> None: ...
    def nak(self, delay: int | float | None = None) -> None: ...
    def term(self) -> None: ...
    def in_progress(self) -> None: ...


@runtime_checkable
class Subscriber(Protocol):
    """Whatever yields inbound messages with a settlement handle."""

    def __aiter__(self): ...

    async def __anext__(self) -> MsgBody: ...
