"""Mock LLM: plays back scripted tool calls from config/mock_scripts/<name>.json.

It sits behind the same decide() interface as a real model, so the whole discovery loop,
the safety gate and the recorder run unchanged without an API key.

A script is chosen when the goal contains all its goal_keywords and matches its goal_pattern.
Named groups in the pattern fill {{placeholders}} in the calls, so the same script works for
member 12345 or 12346.
"""

import json
import re
from pathlib import Path

from .llm import LLMError, ToolCall

MOCK_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "config" / "mock_scripts"


class MockLLM:
    model = "mock"

    def __init__(self, calls: list[dict], name: str = "inline"):
        self.calls = calls
        self.name = name
        self._next = 0

    def decide(self, system: str, prompt: str, tools: list[dict]) -> ToolCall:
        if self._next >= len(self.calls):
            # A real model would keep going; the script ran out, which is a scripted ask_human.
            return ToolCall("ask_human", {"reason": f"mock script '{self.name}' has no more steps"})
        call = self.calls[self._next]
        self._next += 1
        return ToolCall(call["tool"], call["args"])


def _fill(value, values: dict[str, str]):
    if isinstance(value, str):
        return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], value)
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    return value


def load_mock(goal: str, scripts_dir: Path = MOCK_SCRIPTS_DIR) -> MockLLM:
    """Find the script that fits the goal, and fill in its values."""
    for path in sorted(scripts_dir.glob("*.json")):
        script = json.loads(path.read_text())
        if not all(k.lower() in goal.lower() for k in script.get("goal_keywords", [])):
            continue
        match = re.search(script["goal_pattern"], goal, re.IGNORECASE)
        if match:
            return MockLLM(_fill(script["calls"], match.groupdict()), name=path.stem)
    names = ", ".join(p.stem for p in sorted(scripts_dir.glob("*.json"))) or "none"
    raise LLMError(f"no mock script matches this goal (available: {names})")
