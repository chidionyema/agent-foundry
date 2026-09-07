"""Production NATS/JetStream bind for the worker.

Turns the estate's one nats-py JetStream (``OTTO_NATS_URL``, mirroring
otto-gateway's own connection) into the :class:`Subscriber` + DLQ publisher the
worker consumes, using the same settlement surface the tests fake.

Connection URL resolution order: ``OTTO_NATS_URL`` env, else the estate default
``nats://nats.event-bus.svc:4222`` (the single event-bus row). No hardcoded machine
or checkout path lives here.
"""

from __future__ import annotations

import os
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext

ESTATE_NATS_URL = os.environ.get("OTTO_NATS_URL", "nats://nats.event-bus.svc:4222")


class NatsBus:
    """Owns one nats-py connection + JetStream context and yields subscriptions.
    A product is a *client* of this one bus, never a second broker (SPEC section 4).
    """

    def __init__(self, url: str = ESTATE_NATS_URL) -> None:
        self.url = url
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None

    async def connect(self) -> "NatsBus":
        self._nc = await nats.connect(self.url)
        self._js = self._nc.jetstream()
        return self

    @property
    def js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("NatsBus not connected; await connect() first")
        return self._js

    async def consume(self, subject: str, **subscribe_kwargs: Any):
        """Subscribe with JetStream durability. Returns an object exposing the
        message-decode surface; worker pulls via async iteration."""
        # ordered consumer => per-message ack/hb/term all honoured, redeliveries
        # trackable; this matches ORDER.md's worker mechanic exactly.
        return await self.js.subscribe(
            subject, ordered_consumer=True, **subscribe_kwargs
        )

    async def publish_json(self, subject: str, payload: dict[str, Any]) -> None:
        import json as _json

        await self.js.publish(
            subject, _json.dumps(payload, default=str).encode("utf-8")
        )

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._js = None
