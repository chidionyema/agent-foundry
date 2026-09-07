"""Worker mechanics — the guardrails of ORDER.md section 4, proven behaviorally.

Fake delivery models JetStream truth: acked/terminated messages never return;
NAK'd messages are redelivered until the stream/delivery ceiling. See fakes.py.
"""

from __future__ import annotations

import asyncio

from af.bus import Subject
from af.worker import PermanentlyRejected, TransientError, Worker, DeadLetterWaiter
from tests.fakes import FakeMsg, FakeSubscriber, RecordingDLQ, json_msg

SUBJECT = "tasks.ten_a.math-checker"


async def _ok(_payload: dict) -> dict:
    return {"done": True}


# --------------------------------------------------------------------------- #
# poison pill: term on first sight, never via the NAK/retry path
# --------------------------------------------------------------------------- #
def test_undecodable_payload_is_terminated_not_retried() -> None:
    msg = FakeMsg(b"{not json", subject=SUBJECT)
    w = Worker(SUBJECT, handle=_ok)
    asyncio.run(w.run(FakeSubscriber([msg])))
    # term'd once; never NAK'd (no retry path was used). A term()d message is gone
    # from the stream, so the fake offers it exactly once.
    assert msg.calls.count("term") == 1
    assert "nak" not in msg.calls
    assert msg.acked is False
    assert msg.termed is True


def test_non_object_json_root_is_terminated() -> None:
    msg = FakeMsg(b"[1,2,3]", subject=SUBJECT)
    w = Worker(SUBJECT, handle=_ok)
    asyncio.run(w.run(FakeSubscriber([msg])))
    assert msg.termed is True
    assert "ack" not in msg.calls
    assert "nak" not in msg.calls


# --------------------------------------------------------------------------- #
# success: ack + redispatch to the downstream topic
# --------------------------------------------------------------------------- #
def test_success_acks() -> None:
    msg = json_msg({"task_id": "1", "url": "https://x"})
    w = Worker(SUBJECT, handle=_ok)
    asyncio.run(w.run(FakeSubscriber([msg])))
    assert msg.acked is True
    assert "nak" not in msg.calls


def test_success_output_is_redispatched_when_wired() -> None:
    pushed: list[tuple[str, dict]] = []

    async def push(env: dict) -> None:
        pushed.append((env["topic"], env["output"]))

    msg = json_msg({"task_id": "1"})
    w = Worker(
        SUBJECT, handle=_ok, redispatcher=push, topic_out="tasks.ten_a.extractor"
    )
    asyncio.run(w.run(FakeSubscriber([msg])))
    assert msg.acked is True
    assert len(pushed) == 1
    assert pushed[0][0] == "tasks.ten_a.extractor"
    assert pushed[0][1] == {"done": True}


# --------------------------------------------------------------------------- #
# heartbeat keeps a long job alive (in_progress) so AckWait never fires mid-run
# --------------------------------------------------------------------------- #
def test_long_job_is_kept_alive_by_in_progress_heartbeat() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_payload: dict) -> dict:
        started.set()
        await release.wait()
        return {"done": True}

    async def scenario() -> None:
        msg = json_msg({"task_id": "1"})
        w = Worker(SUBJECT, handle=slow, heartbeat_interval_s=0.02)
        task = asyncio.create_task(w.run(FakeSubscriber([msg])))
        await started.wait()
        # several heartbeat intervals elapse while the job is still running
        await asyncio.sleep(0.07)
        release.set()
        await task
        assert msg.acked is True
        # the heartbeat MUST have pulsed at least once before completion (proves a
        # >interval job is kept alive rather than letting AckWait fire mid-run)
        assert msg.calls.count("in_progress") >= 1, msg.calls

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# transient failure: NAK (redeliver) then dead-letter after the attempt ceiling
# --------------------------------------------------------------------------- #
def test_transient_failure_naks_then_dead_letters_after_ceiling() -> None:
    async def always_transient(_payload: dict) -> dict:
        raise TransientError("upstream 503")

    dlq = RecordingDLQ()
    waiter = DeadLetterWaiter(dlq)
    msg = json_msg({"task_id": "9"})
    # max_attempts=3 => msg attempted 3 times: NAK on the 1st two, dead-letter+term
    # on the 3rd. The fake re-offers a NAK'd message; a term'd one never returns.
    w = Worker(
        SUBJECT, handle=always_transient, max_attempts=3, heartbeat_interval_s=0.5
    )
    asyncio.run(w.run(FakeSubscriber([msg]), waiter=waiter))
    assert msg.calls.count("nak") == 2, msg.calls
    assert msg.calls.count("term") == 1, msg.calls
    assert len(dlq.calls) == 1
    source, record = dlq.calls[0]
    assert source == SUBJECT
    assert record["reason"] == "upstream 503"
    assert record["dlq_source"] == SUBJECT
    assert record["payload"]["task_id"] == "9"


def test_transient_below_ceiling_is_never_terminated_or_dead_lettered() -> None:
    async def flaky(_payload: dict) -> dict:
        raise TransientError("rate limit")

    dlq = RecordingDLQ()
    waiter = DeadLetterWaiter(dlq)
    msg = json_msg({"task_id": "4"})
    w = Worker(SUBJECT, handle=flaky, max_attempts=5)
    asyncio.run(w.run(FakeSubscriber([msg], max_deliver=3), waiter=waiter))
    assert msg.calls.count("nak") == 3
    assert msg.termed is False
    assert msg.acked is False
    assert dlq.calls == []


# --------------------------------------------------------------------------- #
# permanently rejected (semantically poison) terminates without the NAK path
# --------------------------------------------------------------------------- #
def test_permanently_rejected_is_terminated_and_dead_lettered() -> None:
    async def reject(_payload: dict) -> dict:
        raise PermanentlyRejected("bad url scheme")

    dlq = RecordingDLQ()
    waiter = DeadLetterWaiter(dlq)
    msg = json_msg({"task_id": "7"})
    w = Worker(SUBJECT, handle=reject)
    asyncio.run(w.run(FakeSubscriber([msg]), waiter=waiter))
    assert msg.termed is True
    assert "nak" not in msg.calls
    assert dlq.calls[0][1]["reason"] == "rejected: bad url scheme"


# --------------------------------------------------------------------------- #
# tenant scoping — cross-tenant isolation (ORDER.md section 4)
# --------------------------------------------------------------------------- #
def test_subjects_are_strictly_tenant_scoped() -> None:
    assert Subject.input("ten_a", "alert") == "tasks.ten_a.alert"
    assert Subject.dlq("ten_a", "alert") == "tasks.ten_a.alert.dlq"
    # a tenant's DLQ stays inside that tenant's namespace even when the slug
    # resembles another tenant — no cross-tenant subject is ever constructible.
    assert ".ten_b." not in Subject.dlq("ten_a", "alert")


def test_invalid_slug_or_tenant_is_refused() -> None:
    from af.bus import Subject
    import pytest

    for bad in ("ten a", "ten_a/../ten_b", "", ".", "x..y"):
        with pytest.raises(ValueError):
            Subject.input("ten_a", bad)
    with pytest.raises(ValueError):
        Subject.input("ten a", "alert")
