"""The bus worker: durable job mechanics binding one node job to inbound messages.

Implements, exactly, ORDER.md section 4:

- **Poison-pill handling** — a message whose payload cannot be validated against the
  order / node schema is TERMINATED via ``msg.term()`` on first sight, bypassing the
  retry logic. A corrupt message will never become valid by redelivery, so retrying
  would only produce an infinite redelivery loop.
- **Ack heartbeat for long jobs** — while a node job runs, a background task calls
  ``msg.in_progress()`` on an interval (default 15s), so the JetStream consumer's
  AckWait never fires mid-run for a legitimate generation/processing-heavy job.
- **NAK + delay on transient error** — a transient failure negatively-acknowledges
  with a backoff delay so the stream redelivers later rather than dropping the work.
- **Dead-letter after N** — after ``max_attempts`` consecutive transient failures a
  message is moved (raw payload + failure record) to the tenant-scoped DLQ subject
  and the original is terminated. No infinite redelivery.

Every subject is tenant-scoped (see :mod:`af.bus`); the worker never constructs a
subject that could cross a tenant boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .bus import MsgBody, Subscriber

log = logging.getLogger("agent-foundry.worker")

# Heartbeat interval (ORDER.md §4: pulse msg.in_progress() every 15 seconds).
HEARTBEAT_INTERVAL_S = 15.0
# Default consecutive-transient-failure ceiling before a message is dead-lettered.
DEFAULT_MAX_ATTEMPTS = 3

# A node job's handler: takes the validated payload dict and returns the output
# dict the next stage consumes (or None for terminal/side-effect-only jobs).
NodeJob = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


# What "transient" means to a caller, named for intent at the call site.
class TransientError(Exception):
    """A failure that redelivery may resolve (upstream hiccup, rate limit, 5xx).

    Raised inside a node job, the worker NAKs with delay and, after
    ``max_attempts``, dead-letters.
    """

    def __init__(self, message: str, retry_delay_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_delay_s = retry_delay_s


class PermanentlyRejected(Exception):
    """A failure that no redelivery fixes (poison in the *payload's meaning* even
    though it parsed). The worker terminates the message immediately."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class _Heartbeat:
    """Calls ``msg.in_progress()`` every interval until stopped, so long node jobs
    never trip the consumer's AckWait. Also the mechanism ORDER.md calls
    extend_ack_heartbeat() / msg.in_progress()."""

    def __init__(self, target: MsgBody, interval_s: float) -> None:
        self._target = target
        self._interval_s = interval_s
        self._stop = asyncio.Event()

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self._interval_s)
                if self._stop.is_set():
                    break
                self._target.in_progress()
        except Exception:  # pragma: no cover - heartbeat failure is non-fatal
            log.exception("heartbeat failed; acknowledging runs unaffected")

    async def __aenter__(self) -> "_Heartbeat":
        self._task = asyncio.create_task(self.run())
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


class AttemptTracker:
    """Dead-letter ceiling per message. Keyed by (subject, message_id) so a NAK'd
    redelivery of the same message accumulates attempts while a genuinely new
    message starts fresh. The worker looks up attempts by the msg's subject and an
    id taken from the payload (task_id), not by process state that resets on pod
    restart for a message that is mid-redelivery — streams survive the worker, so
    this tracker is a best-effort within one process; the durable ceiling is the
    stream's max_deliver, which the caller should keep consistent."""

    def __init__(self, max_attempts: int) -> None:
        self._max_attempts = max_attempts
        self._counts: dict[tuple[str, str], int] = {}

    def is_over(self, subject: str, message_id: str) -> bool:
        return self._counts.get((subject, message_id), 0) >= self._max_attempts

    def record_failure(self, subject: str, message_id: str) -> None:
        key = (subject, message_id)
        self._counts[key] = self._counts.get(key, 0) + 1


class Worker:
    """Consumes from a tenant-scoped subject and drives one NodeJob per message.

    ``decode`` returns a payload dict plus an id used for attempt tracking.
    ``handle`` is the node job. Both are injected so this one class is used by
    every bot in the army without subclass spam.
    """

    def __init__(
        self,
        subject: str,
        *,
        handle: NodeJob,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        redispatcher: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        topic_out: str | None = None,
        decode_id: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.subject = subject
        self._handle = handle
        self._max_attempts = max_attempts
        self._heartbeat_interval_s = heartbeat_interval_s
        # redispatcher = what to call with the output to push the next stage. When
        # None the output is logged (node was terminal / side-effect only).
        self._redispatcher = redispatcher
        self.topic_out = topic_out
        self._id = decode_id or (lambda payload: str(payload.get("task_id", "?")))
        self._attempts = AttemptTracker(max_attempts)

    async def _dead_letter(
        self,
        msg: MsgBody,
        payload: dict[str, Any],
        reason: str,
        waiter: "DeadLetterWaiter | None",
    ) -> None:
        if waiter is not None:
            await waiter.dead_letter(self.subject, payload, reason)
        # Original term'd — never redelivered again.
        msg.term()

    async def _handle_one(
        self, msg: MsgBody, waiter: "DeadLetterWaiter | None"
    ) -> None:
        # --- decode + validate: poison-pill path, NEVER retried -----------------
        try:
            raw = json.loads(msg.data.decode("utf-8"))
            if not isinstance(raw, dict):
                # a JSON non-object at the root can never become a valid order/node
                raise ValueError("payload root must be a JSON object")
        except Exception as exc:
            log.warning(
                "poison pill on %s (undecodable): %s; terminating", msg.subject, exc
            )
            msg.term()
            return

        mid = self._id(raw)
        # Validation of the semantic contract happens in handle(); undecodable was
        # handled above. Now run the node job with a live heartbeat.
        try:
            async with _Heartbeat(msg, self._heartbeat_interval_s):
                output = await self._handle(raw)
            msg.ack()
            if output is not None:
                if self._redispatcher and self.topic_out:
                    await self._redispatcher(
                        {"input": raw, "output": output, "topic": self.topic_out}
                    )
                else:
                    log.info(
                        "node produced output but no downstream topic; discarding output"
                    )
        except PermanentlyRejected as exc:
            # Semantically poison even though it parsed: terminate, do not retry.
            log.warning(
                "permanently rejecting message id=%s on %s: %s", mid, self.subject, exc
            )
            if waiter is not None:
                await waiter.dead_letter(self.subject, raw, f"rejected: {exc}")
            msg.term()
        except TransientError as exc:
            self._attempts.record_failure(msg.subject, mid)
            if self._attempts.is_over(msg.subject, mid):
                log.warning(
                    "dead-lettering message id=%s on %s after %d failures: %s",
                    mid,
                    self.subject,
                    self._max_attempts,
                    exc,
                )
                await self._dead_letter(msg, raw, str(exc), waiter)
            else:
                log.info(
                    "transient error id=%s on %s (attempt %d): %s",
                    mid,
                    msg.subject,
                    self._attempts._counts.get((msg.subject, mid)),
                    exc,
                )
                # NAK with delay; the tracker accumulates across in-process redeliveries.
                msg.nak(delay=exc.retry_delay_s)
        except Exception as exc:  # noqa: BLE001 - our own PermanentlyRejected/Transient are above
            log.exception("unexpected failure id=%s on %s: %s", mid, msg.subject, exc)
            msg.nak()

    async def run(
        self, subscriber: Subscriber, waiter: "DeadLetterWaiter | None" = None
    ) -> None:
        """Consume forever (or until the subscription is cancelled upstream)."""
        async for msg in subscriber:
            try:
                await self._handle_one(msg, waiter)
            except Exception:  # noqa: BLE001 - never let one bad message kill the loop
                log.exception("worker loop error on %s", msg.subject)
                try:
                    msg.nak()
                except Exception:  # noqa: BLE001 - best-effort NAK after a fatal msg error
                    log.exception(
                        "failed to NAK message after loop error on %s", msg.subject
                    )


class DeadLetterWaiter:
    """Publishes dead letters to the tenant-scoped DLQ subject. Injection seam so
    the worker's DLQ behavior is test-verifiable without a cluster; the production
    bind hands a real publisher."""

    def __init__(
        self, publisher: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        self._publisher = publisher

    async def dead_letter(
        self, source_subject: str, payload: dict[str, Any], reason: str
    ) -> None:
        record = {
            "dlq_source": source_subject,
            "reason": reason,
            "dead_at": __import__("datetime")
            .datetime.now(tz=__import__("datetime").timezone.utc)
            .isoformat(),
            "payload": payload,
        }
        await self._publisher(source_subject, record)
