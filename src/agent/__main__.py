"""Try discovery before the CLI exists (step 10):

    python -m src.agent "Look up member 12345 and read their savings balance" [--mock] [--slow]

The bank must be running on :5050. Risky clicks are rejected for now (approval prompt: step 9).
The browser stays open after the run until you press Enter (use --auto-close to skip that).
"""

import argparse
import sys

from dotenv import load_dotenv

from src.handoff import open_session

from .discovery import run_discovery
from .llm import LLMError, make_llm
from .mock_llm import load_mock


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the discovery agent on a goal.")
    parser.add_argument("goal")
    parser.add_argument("--mock", action="store_true", help="use a scripted mock LLM (no API key)")
    parser.add_argument("--slow", action="store_true", help="slow the browser down so you can watch")
    parser.add_argument("--auto-close", action="store_true", help="close the browser as soon as the run ends")
    args = parser.parse_args()

    load_dotenv()
    with open_session("discover", slow_mo_ms=600 if args.slow else 0) as session:
        try:
            llm = load_mock(args.goal) if args.mock else make_llm(session.settings)
        except LLMError as e:
            raise SystemExit(f"error: {e}")
        print(f"[discover] model: {llm.model}")
        result = run_discovery(session, llm, args.goal)

        # Report while the browser is still open, so the final page can be inspected.
        r = result.run_result
        print(f"\nResult: {r.status.value}" + (f" - {r.message}" if r.message else ""))
        for name, value in r.outputs.items():
            print(f"  {name} = {value}")  # real value for the caller; masked in the run folder
        print(f"  steps that worked: {len(result.successful_steps)} of {len(result.steps)}")
        print(f"  run folder: {session.logger.folder.path}")
        wait_before_closing(args.auto_close)


def wait_before_closing(auto_close: bool) -> None:
    # Only wait when a person is at the keyboard; scripts and pipes must not hang.
    if not auto_close and sys.stdin.isatty():
        input("\nBrowser left open so you can look around. Press Enter to close it...")


if __name__ == "__main__":
    main()
