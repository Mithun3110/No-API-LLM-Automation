"""Run settings, loaded from config/settings.json. LLM settings are added in step 6."""

import json
from pathlib import Path

from pydantic import Field

from .common import StrictModel

DEFAULT_SETTINGS_PATH = Path(__file__).parent.parent.parent / "config" / "settings.json"


class Settings(StrictModel):
    bank_base_url: str
    step_limit: int = Field(default=20, ge=1)             # discovery stops after this many actions
    wait_timeout_s: float = Field(default=10, gt=0)       # replay waits this long for wait_for
    recoverable_retries: int = Field(default=3, ge=0)     # max fixes per recoverable condition
    recovery_max_actions: int = Field(default=3, ge=1)    # bounded LLM recovery budget
    headless: bool = False                                # visible, so a human can take over


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    return Settings.model_validate(json.loads(path.read_text()))
