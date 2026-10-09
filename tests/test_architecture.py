"""The rules in CLAUDE.md, checked on the code itself, so they cannot quietly erode."""

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "src"
BROWSER_ACTIONS = {"click", "type", "select", "goto"}  # Browser methods that change the page


def python_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_only_the_browser_layer_imports_playwright():
    offenders = [str(p.relative_to(ROOT)) for p in python_files(SRC)
                 if any(m.startswith("playwright") for m in imported_modules(p))
                 and SRC / "browser" not in p.parents]
    assert offenders == []


def test_replay_never_imports_llm_code():
    for p in python_files(SRC / "replay"):
        bad = [m for m in imported_modules(p) if m.startswith(("src.agent", "src.recovery", "src.catalog.matcher",
                                                                "openai", "anthropic"))]
        assert bad == [], f"{p.name} imports {bad}"


def test_only_the_session_gate_acts_on_the_page():
    """Every click/type/select/navigation must go through Session.perform (and so the safety guard).
    Reading the page (find, snapshot, heading, screenshot) is allowed anywhere."""
    allowed = {SRC / "browser" / "browser.py", SRC / "handoff" / "session.py"}  # the layer itself, and the gate
    offenders = []
    for p in python_files(SRC):
        if p in allowed:
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in BROWSER_ACTIONS and _looks_like_browser(node.func.value):
                offenders.append(f"{p.relative_to(ROOT)}:{node.lineno} .{node.func.attr}()")
    assert offenders == []


def _looks_like_browser(node: ast.AST) -> bool:
    # b.click(...), self.browser.click(...), session.browser.click(...)
    text = ast.unparse(node)
    return text in ("b", "browser") or text.endswith(".browser")


def test_no_secrets_in_tracked_files():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ".env" not in tracked
    key_like = re.compile(r"(gsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9-]{20,}|sk-ant-[A-Za-z0-9-]{20,})")
    hits = [f for f in tracked if (ROOT / f).is_file() and (ROOT / f).suffix in (".py", ".json", ".md", ".txt", ".example")
            and key_like.search((ROOT / f).read_text(errors="ignore"))]
    assert hits == []


def test_runs_and_docs_are_git_ignored():
    for path in (".env", "runs/x/log.jsonl", "docs/assignment.pdf", "bank_app/data/bank.db", ".venv/x"):
        ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0
        assert ignored, f"{path} is not git-ignored"
