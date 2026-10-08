"""Try replay before the CLI exists (step 10). No LLM is involved at any point:

    python -m src.replay recipes/member.lookup_savings_balance@1.0.0.json member_id=12345 [--slow]
    python -m src.replay <recipe> member_id=12345 --inject popup --at-step 3

--inject arms one of the bank's test errors (popup, slow_page, session_expired, server_error)
right before a step's action. It calls the bank's /_admin switch directly, like curl: test
tooling, outside the browser and outside the automation's allowlist.

The bank must be running on :5050. Risky steps are rejected for now (approval prompt: step 9).
The browser stays open after the run until you press Enter (use --auto-close to skip that).
"""

import argparse
import sys
import urllib.request
from pathlib import Path

from src.handoff import open_session
from src.models import Recipe

from .engine import replay


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a recipe deterministically (no LLM).")
    parser.add_argument("recipe", type=Path)
    parser.add_argument("inputs", nargs="*", help="name=value pairs, e.g. member_id=12345")
    parser.add_argument("--slow", action="store_true", help="slow the browser down so you can watch")
    parser.add_argument("--auto-close", action="store_true", help="close the browser as soon as the run ends")
    parser.add_argument("--inject", choices=["popup", "slow_page", "session_expired", "server_error"],
                        help="arm a bank test error before a step (demo/testing only)")
    parser.add_argument("--at-step", type=int, default=3, help="which step --inject fires before (default 3)")
    args = parser.parse_args()

    recipe = Recipe.model_validate_json(args.recipe.read_text())
    try:
        inputs = dict(pair.split("=", 1) for pair in args.inputs)
    except ValueError:
        raise SystemExit("inputs must look like name=value")

    with open_session("replay", slow_mo_ms=600 if args.slow else 0) as session:
        if args.inject:
            inject_before_step(session, args.inject, args.at_step)
        result = replay(recipe, inputs, session).run_result
        print(f"\nResult: {result.status.value}" + (f" ({result.outcome_code})" if result.outcome_code else "")
              + (f" - {result.message}" if result.message else ""))
        for name, value in result.outputs.items():
            print(f"  {name} = {value}")
        if result.failure:
            f = result.failure
            print(f"  failed at step {f.step} [{f.error_type}]\n    expected: {f.expected}\n    observed: {f.observed}")
            print("    evidence: " + ", ".join(Path(e).name for e in f.evidence))
        for note in result.recoveries + result.warnings:
            print(f"  note: {note}")
        print(f"  run folder: {session.logger.folder.path}")
        if not args.auto_close and sys.stdin.isatty():
            input("\nBrowser left open so you can look around. Press Enter to close it...")


def inject_before_step(session, error: str, step: int) -> None:
    """Arm the bank's test error just before `step` acts. Armed after login, so login cannot use it up."""
    original = session.perform
    armed = False

    def perform(action):
        nonlocal armed
        if action.step == step and not armed:
            armed = True
            urllib.request.urlopen(f"{session.settings.bank_base_url}/_admin/inject?error={error}")
            print(f"  [demo] armed '{error}' before step {step}")
        return original(action)
    session.perform = perform


if __name__ == "__main__":
    main()
