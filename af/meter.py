"""Metering — records every task execution for a run.

ORDER.md section 3: *metering is platform, and the estate's DB is the estate's DB
(one DB row), not a second store.* So this module does NOT open its own database.

The seam: :class:`Meter` is given a ``writer`` callable. In the deployed estate
image the writer is bound to the estate's single Postgres row layer via the
platform's identity/secrets path (the ``tenants`` + ``task_executions`` rows the
spec names). In the headless milestone, :class:`FileMeter` / console writer record
the same event shape to a JSONL file so a completed end-to-end run is observably,
verifiably recorded without standing up the estate DB inside this build.

The one event shape all writers receive is fixed here so a later step can connect
the file writer's rows to the estate DB with no format drift.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable


# The exact event the platform's task_executions row is expected to hold.
@dataclass
class Execution:
    tenant_id: str
    order_id: str
    run_id: str
    agent_slug: str  # one of the army node names: scout, dom-stripper...
    status: str  # ran | failed(transient) | poisoned | dead-lettered
    started_at: float
    finished_at: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def elapsed_ms(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at) * 1000.0


class Meter:
    """Records executions through an injected writer (test / file / estate DB)."""

    def __init__(self, writer: Callable[[dict[str, Any]], None]) -> None:
        self._writer = writer

    def record(self, ex: Execution) -> None:
        self._writer(ex.to_json())


def _console_writer(row: dict[str, Any]) -> None:
    import logging

    logging.getLogger("agent-foundry.meter").info(
        "meter %s", json.dumps(row, sort_keys=True)
    )


DEFAULT_LOG_WRITER = _console_writer


class FileMeter:
    """Records executions to JSONL under a directory the caller supplies (tests)
    or ``AF_METER_DIR`` (real image mounts a volume). No checkout path is baked in
    (hardcode-fence); when neither is given it falls back to a fresh temp dir."""

    def __init__(self, directory: Path | str | None = None) -> None:
        import tempfile

        if directory is None:
            directory = Path(
                os.environ.get("AF_METER_DIR") or tempfile.mkdtemp(prefix="af-meter-")
            )
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = (self.dir / "executions.jsonl").open("a", encoding="utf-8")

    def write(self, row: dict[str, Any]) -> None:
        self._fh.write(json.dumps(row, sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
