"""Structured run log: one JSON object per line in runs/<run_id>/log.jsonl.

Everything passes through the Masker BEFORE it is written. There is no unmasked write path.
"""

import json
from pathlib import Path
from typing import Any

from src.models import ControlState, LogEntry, RunResult
from src.models.common import LogMode
from src.safety import Masker

from .run_folder import RunFolder


class RunLogger:
    def __init__(self, folder: RunFolder, masker: Masker, echo: bool = True):
        self.folder = folder
        self.masker = masker
        self.echo = echo  # also print a short line to the terminal

    @property
    def run_id(self) -> str:
        return self.folder.run_id

    def log(
        self,
        mode: LogMode,
        outcome: str,
        controller: ControlState,
        step: int | None = None,
        action: str | None = None,
        target: str | None = None,
        reason: str | None = None,
        warnings: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> LogEntry:
        m = self.masker
        entry = LogEntry(
            run_id=self.run_id, mode=mode, outcome=outcome, controller=controller, step=step, action=action,
            target=m.mask_text(target) if target else None,
            reason=m.mask_text(reason) if reason else None,
            warnings=[m.mask_text(w) for w in warnings or []],
            data=m.mask(data or {}),
        )
        with self.folder.log_path.open("a") as f:
            f.write(entry.model_dump_json() + "\n")
        if self.echo:
            where = f"step {step} " if step is not None else ""
            what = " ".join(x for x in (action, entry.target) if x)
            print(f"[{mode}] {where}{what} -> {outcome}" + (f" ({entry.reason})" if entry.reason else ""))
        return entry

    def write_result(self, result: RunResult) -> Path:
        """Save result.json with sensitive outputs masked. The caller still gets the real values."""
        raw = result.model_dump(mode="json")
        data = self.masker.mask(raw)
        # Structural fields are never sensitive and must stay exact (paths are used to open evidence).
        for key in ("run_id", "recipe_id", "recipe_version", "status", "mode", "log_file", "outcome_code"):
            data[key] = raw[key]
        if result.failure:
            data["failure"]["evidence"] = raw["failure"]["evidence"]
        self.folder.result_path.write_text(json.dumps(data, indent=2))
        return self.folder.result_path


def read_log(path: Path) -> list[LogEntry]:
    """Read a log back into typed entries (used by tests and evidence checks)."""
    return [LogEntry.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
