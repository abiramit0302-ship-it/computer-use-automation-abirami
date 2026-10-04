"""Evidence: a structured, redacted record of every run.

Each run gets its own folder, runs/<run_id>/, containing:
  events.jsonl   one JSON object per line, in order: what happened and why
  screenshots/   images captured at failures and escalations (sensitive cells masked)
  summary.json   the final result
Every event passes through the Redactor before it's written. There is no way to log
around it: redaction happens at the sink.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.redaction import Redactor

RUNS_DIR = Path("runs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, kind: str, redactor: Redactor, root: Path = RUNS_DIR):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{kind}-{stamp}-{uuid.uuid4().hex[:6]}"
        self.dir = Path(root) / self.run_id
        (self.dir / "screenshots").mkdir(parents=True)
        self.redactor = redactor
        self._seq = 0
        self._file = (self.dir / "events.jsonl").open("w", encoding="utf-8")

    def event(self, kind: str, message: str, *, step_id: str | None = None, **data: Any) -> None:
        """Record one thing that happened. Redacted before it touches the disk."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": _now(),
            "kind": kind,
            "step_id": step_id,
            "message": message,
            "data": data,
        }
        self._file.write(json.dumps(self.redactor.redact_data(record)) + "\n")
        self._file.flush()  # written immediately, so a crash never loses the last events

    def screenshot_path(self, label: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return self.dir / "screenshots" / f"{self._seq:03d}-{safe}.png"

    def write_summary(self, summary: dict[str, Any]) -> Path:
        path = self.dir / "summary.json"
        path.write_text(json.dumps(self.redactor.redact_data(summary), indent=2) + "\n")
        return path

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()