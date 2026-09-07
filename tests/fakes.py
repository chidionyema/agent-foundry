"""Fakes for the JetStream settlement surface so worker mechanics stay verifiable
without a live cluster (tests) — the real nats-py Msg implements the same methods
the worker drives (ack/nak/term/in_progress).

Modelling rule (matches real JetStream): a message that has been ack()'d or term()'d
is permanently removed from the stream and is never offered again; a message that
has been nak()'d *without* a delay deadline being met is slated for redelivery and
keeps coming back until the stream's delivery ceiling is reached.
"""

from __future__ import annotations

import json
from typing import Any


class FakeMsg:
    """Records the settlement calls a worker made on it."""

    def __init__(
        self, data: bytes, *, subject: str = "tasks.ten_x.price-alert", sid: int = 1
    ) -> None:
        self.data = data
        self.subject = subject
        self.sid = sid
        self.reply = subject + ".reply"
        self.calls: list[str] = []
        self.acked = False
        self.termed = False
        self.nak_count = 0
        self._nak_delays: list[int | float | None] = []

    def ack(self) -> None:
        self.calls.append("ack")
        self.acked = True

    def ack_sync(self) -> None:
        self.calls.append("ack_sync")
        self.acked = True

    def nak(self, delay: int | float | None = None) -> None:
        self.calls.append("nak")
        self.nak_count += 1
        self._nak_delays.append(delay)

    def term(self) -> None:
        self.calls.append("term")
        self.termed = True

    def in_progress(self) -> None:
        self.calls.append("in_progress")

    @property
    def nak_delays(self) -> list[int | float | None]:
        return self._nak_delays


class FakeSubscriber:
    """Offers physical messages modelling JetStream delivery rules:

    - an acked or terminated message is no longer offered;
    - a NAK'd message keeps being re-offered until it has been NAK'd ``max_deliver``
      times (the stream's total-delivery ceiling), after which redelivery stops —
      modelling what the operator configures as the durable dead-letter trigger.

    Tests that want to prove the worker's *in-process* ceiling instead use
    ``settle_once=False`` patterns; by default this fake reflects JetStream truth.
    """

    def __init__(self, msgs: list[FakeMsg], max_deliver: int = 20) -> None:
        self._physical: list[FakeMsg] = list(msgs)
        self._max_deliver = max_deliver

    def __aiter__(self):
        return self

    async def __anext__(self) -> FakeMsg:
        for m in self._physical:
            if m.acked or m.termed:
                continue
            if m.nak_count >= self._max_deliver:
                # stream stopped redelivering -> consumer sits empty
                break
            return m
        raise StopAsyncIteration


class RecordingDLQ:
    """Records dead-letter publications fired at a tenant-scoped DLQ subject."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, source_subject: str, record: dict[str, Any]) -> None:
        self.calls.append((source_subject, record))


def json_msg(payload: dict[str, Any]) -> FakeMsg:
    return FakeMsg(json.dumps(payload).encode("utf-8"))
