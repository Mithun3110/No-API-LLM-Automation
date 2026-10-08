"""Each run gets its own folder: runs/<run_id>/ with log, result, screenshots, traces."""

import secrets
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RUNS_DIR = Path(__file__).parent.parent.parent / "runs"


def new_run_id() -> str:
    # e.g. 20261008T203012Z-a3f9: UTC like the log timestamps; sorts by time; the random
    # suffix avoids collisions.
    # No long bare digit runs, so the masker's account-number pattern never hits a run id.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


class RunFolder:
    def __init__(self, run_id: str | None = None, runs_dir: Path = DEFAULT_RUNS_DIR):
        self.run_id = run_id or new_run_id()
        self.path = runs_dir / self.run_id
        self.path.mkdir(parents=True, exist_ok=False)  # never write into another run's folder
        self._shots = 0

    @property
    def log_path(self) -> Path:
        return self.path / "log.jsonl"

    @property
    def result_path(self) -> Path:
        return self.path / "result.json"

    def screenshot_path(self, label: str) -> Path:
        # Numbered so screenshots sort in the order they were taken.
        self._shots += 1
        return self.path / f"{self._shots:02d}_{label}.png"

    def trace_path(self) -> Path:
        return self.path / "trace.zip"

    def snapshot_path(self, label: str) -> Path:
        return self.path / f"{label}_accessibility.yaml"

    def intervention_path(self, number: int) -> Path:
        return self.path / f"intervention_{number}.json"
