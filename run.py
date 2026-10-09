"""Command line for the automation system.

    python run.py list
    python run.py ask "What's the savings balance for member 12346?"            [--mock]
    python run.py replay "What's the savings balance for member 12346?"         [--mock] [--allow-draft]
    python run.py replay --recipe member.lookup_savings_balance --input member_id=12346
    python run.py discover "Look up member 12345 and read their savings balance" [--mock]
    python run.py approve member.lookup_savings_balance

ask      main command: a matching recipe is replayed; no match -> LLM discovery, new draft recipe
replay   strict: recipes only, never falls back to the LLM; drafts refused without --allow-draft
         with --recipe: no LLM anywhere, not even for matching
discover run the discovery agent on a goal and save a draft recipe
approve  review a draft recipe and mark it approved
list     show the recipe catalog

Imports of LLM code are kept inside the functions that need them, so `replay --recipe`
never loads an LLM client.
"""

import argparse
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src.catalog import RECIPES_DIR, Catalog, StoredRecipe, approve
from src.handoff import TerminalOperator, open_session, reject_all
from src.logs import RunFolder, RunLogger
from src.logs.run_folder import DEFAULT_RUNS_DIR
from src.models import ControlState, Policy, Recipe, RunResult, RunStatus
from src.models.settings import Settings, load_settings
from src.replay import replay, validate_inputs
from src.safety import Masker, load_policy

DRAFT_REFUSED = "Recipe is draft; approve it first or pass --allow-draft"


@dataclass
class Context:
    """Everything a command needs. Tests build one pointing at the test bank and temp folders."""
    settings: Settings
    policy: Policy
    recipes_dir: Path = RECIPES_DIR
    runs_dir: Path = DEFAULT_RUNS_DIR
    interactive: bool = True     # a person at the keyboard: approvals, takeover, prompts
    operator: object = None      # overrides the terminal operator (tests)
    headless: bool | None = None

    def make_operator(self, args):
        if self.operator is not None:
            return self.operator
        return TerminalOperator() if self.interactive and not args.no_human else None


# ---------------------------------------------------------------- list / approve
def cmd_list(args, ctx: Context) -> int:
    catalog = Catalog.load(ctx.recipes_dir)
    if not catalog.ids():
        print("No recipes yet. Run `ask` or `discover` to create one.")
    for recipe_id in catalog.ids():
        r = catalog.active(recipe_id).recipe
        flags = r.status + (", needs review" if r.needs_review else "")
        others = [s.recipe.version for s in catalog.versions[recipe_id] if s.recipe.version != r.version]
        print(f"\n{recipe_id}  v{r.version}  ({flags})" + (f"  other versions: {', '.join(others)}" if others else ""))
        print(f"  {r.description}")
        print(f"  inputs : {', '.join(f'{n} ({s.type})' for n, s in r.inputs.items()) or 'none'}")
        print(f"  outputs: {', '.join(f'{n} ({s.type})' for n, s in r.outputs.items())}")
    for problem in catalog.problems:
        print(f"\n  ! skipped {problem}")
    return 0


def summary(recipe: Recipe) -> str:
    """What a reviewer needs to see before approving: contract, steps, risk, provenance."""
    lines = [f"{recipe.recipe_id} v{recipe.version} ({recipe.status})", f"  {recipe.description}",
             f"  recorded from run {recipe.provenance.recorded_from_run} at {recipe.provenance.recorded_at:%Y-%m-%d %H:%M}"]
    lines += [f"  input  {n}: {s.type}" + (f", pattern {s.pattern}" if s.pattern else "")
              + (", sensitive" if s.sensitive else "") for n, s in recipe.inputs.items()]
    lines += [f"  output {n}: {s.type}" + (", sensitive" if s.sensitive else "") for n, s in recipe.outputs.items()]
    lines.append("  steps:")
    for st in recipe.steps:
        target = ""
        if st.target:
            first = st.target.strategies[0]
            name = getattr(first, "name", None) or getattr(first, "text", None) or getattr(first, "anchor", None)
            target = f" {first.by} \"{name}\" (+{len(st.target.strategies) - 1} backup)"
        detail = st.value or st.url or ""
        flags = "".join([" [IRREVERSIBLE: needs approval at run time]" if st.risk == "irreversible" else "",
                         " [done by a HUMAN: check it]" if st.source == "human" else ""])
        lines.append(f"    {st.step}. {st.action}{target} {detail}{flags}")
    lines.append(f"  error handlers: {', '.join(h.id for h in recipe.error_handlers)}")
    return "\n".join(lines)


def cmd_approve(args, ctx: Context) -> int:
    catalog = Catalog.load(ctx.recipes_dir)
    versions = catalog.versions.get(args.recipe_id, [])
    stored = next((s for s in versions if s.recipe.version == args.version), None) if args.version \
        else (versions[-1] if versions else None)
    if stored is None:
        print(f"No recipe {args.recipe_id}" + (f" v{args.version}" if args.version else ""))
        return 1
    if stored.recipe.status == "approved":
        print(f"{args.recipe_id} v{stored.recipe.version} is already approved.")
        return 0
    print(summary(stored.recipe))
    if not args.yes and not confirm(ctx, f"\nApprove {args.recipe_id} v{stored.recipe.version}?"):
        print("Not approved.")
        return 1
    approve(stored, basis="manual_review")
    print(f"Approved {args.recipe_id} v{stored.recipe.version}.")
    return 0


# ---------------------------------------------------------------- discover
def cmd_discover(args, ctx: Context) -> int:
    return exit_code(run_discovery_command(args, ctx, args.goal, mode="discover"))


def run_discovery_command(args, ctx: Context, goal: str, mode: str) -> RunResult:
    from src.agent import LLMError, load_mock, make_llm, run_discovery  # LLM code: only on this path
    from src.recorder import RecorderError, record

    try:  # before opening a browser: no point signing in without a model
        llm = load_mock(goal) if args.mock else make_llm(ctx.settings)
    except LLMError as e:
        raise SystemExit(f"error: {e}")
    print(f"\n>>> No recipe used: the LLM ({llm.model}) drives the website live, deciding every step.\n")
    operator = ctx.make_operator(args)
    with session(args, ctx, "discover", operator) as s:
        s.logger.log("discover", "model", s.control, reason=f"{mode}: discovering with {llm.model}")
        result = run_discovery(s, llm, goal, operator)
        r = result.run_result
        print_result(r, s.logger.folder.path)
        if result.outcome == "success":  # only proven paths become recipes
            try:
                saved = record(result, s.logger.run_id, s.settings, s.guard, s.logger.masker, ctx.recipes_dir)
                review = " - needs review (contains human steps)" if saved.recipe.needs_review else ""
                print(f"  new recipe (draft): {saved.path.name}{review}")
                for note in saved.skipped:
                    print(f"    left out {note}")
                print(f"  review and approve it with: python run.py approve {saved.recipe.recipe_id}")
            except RecorderError as e:
                print(f"  recipe NOT saved: {e}")
        wait_before_closing(args, ctx)
    return r


# ---------------------------------------------------------------- replay (strict)
def cmd_replay(args, ctx: Context) -> int:
    catalog = Catalog.load(ctx.recipes_dir)
    if args.recipe:
        stored = catalog.active(args.recipe)  # no LLM anywhere on this path
        if stored is None:
            return exit_code(finish_offline(ctx, "replay", RunStatus.NO_MATCHING_RECIPE, f"no recipe '{args.recipe}'"))
        inputs = parse_inputs(args.input)
        how = "chosen with --recipe"
    else:
        if not args.request:
            raise SystemExit("give a request, or --recipe <id> --input name=value")
        match = match_request(args, ctx, catalog, args.request)
        if not match.found:
            return exit_code(finish_offline(ctx, "replay", RunStatus.NO_MATCHING_RECIPE,
                                            f"No matching recipe ({match.reason})", request=args.request))
        stored, inputs, how = match.stored, match.inputs, match.reason
    if stored.recipe.status != "approved" and not args.allow_draft:
        return exit_code(finish_offline(ctx, "replay", RunStatus.DRAFT_NOT_ALLOWED, DRAFT_REFUSED, stored.recipe))
    return exit_code(run_replay(args, ctx, stored, inputs, how))


# ---------------------------------------------------------------- ask (main)
def cmd_ask(args, ctx: Context) -> int:
    catalog = Catalog.load(ctx.recipes_dir)
    match = match_request(args, ctx, catalog, args.request) if catalog.ids() else None
    if match is not None and match.missing_value:
        # A recipe exists but the request lacks a value: ask the caller, do not discover a duplicate.
        return exit_code(finish_offline(ctx, "ask", RunStatus.INVALID_INPUT, match.reason, request=args.request))
    if match is None or not match.found:
        print(f"No matching recipe ({match.reason if match else 'the catalog is empty'}): "
              "discovering with the LLM, which will save a new draft recipe.")
        return exit_code(run_discovery_command(args, ctx, args.request, mode="ask"))

    stored, recipe = match.stored, match.stored.recipe
    if recipe.status != "approved":
        print(f"{recipe.recipe_id} v{recipe.version} is a DRAFT: it has not been reviewed.")
        if not confirm(ctx, "Run it anyway?"):
            return exit_code(finish_offline(ctx, "ask", RunStatus.DRAFT_NOT_ALLOWED, "draft not confirmed by the user",
                                            recipe))
    result = run_replay(args, ctx, stored, match.inputs, match.reason, mode="ask")
    offer_approval(ctx, stored, result)
    return exit_code(result)


def offer_approval(ctx: Context, stored: StoredRecipe, result: RunResult) -> None:
    """A draft that just replayed successfully on its own: offer to approve it, right now."""
    r = stored.recipe
    if r.status == "approved" or result.status != RunStatus.SUCCESS or not ctx.interactive:
        return
    if r.needs_review:
        print(f"  {r.recipe_id} contains human steps: review it with `python run.py approve {r.recipe_id}`.")
        return
    print(f"\n{r.recipe_id} v{r.version} is a draft and just replayed successfully (run {result.run_id}).")
    if confirm(ctx, "Approve it now?"):
        approve(stored, basis=f"successful_replay:{result.run_id}")
        print(f"  Approved {r.recipe_id} v{r.version}.")


# ---------------------------------------------------------------- shared
def match_request(args, ctx: Context, catalog: Catalog, request: str):
    from src.catalog.matcher import match_with_llm, match_with_mock  # LLM code: only on this path
    if args.mock:
        return match_with_mock(request, catalog)
    from src.agent.llm import make_llm
    return match_with_llm(request, catalog, make_llm(ctx.settings))


def run_replay(args, ctx: Context, stored: StoredRecipe, inputs: dict, how: str, mode: str = "replay") -> RunResult:
    recipe = stored.recipe
    _, errors = validate_inputs(recipe, inputs)
    if errors:  # checked before a browser is opened
        return finish_offline(ctx, mode, RunStatus.INVALID_INPUT, "; ".join(errors), recipe)
    llm_used_for = [] if how == "chosen with --recipe" else [f"matching the request to this recipe ({how})"]
    print(f"\n>>> Using recipe {recipe.recipe_id} v{recipe.version} ({recipe.status}): deterministic replay, "
          "the LLM makes no decisions on the website.")
    print(f"    LLM used for: {', '.join(llm_used_for) or 'nothing (no LLM at all)'}\n")
    operator = ctx.make_operator(args)
    with session(args, ctx, "replay", operator) as s:
        s.logger.log("catalog", "matched", s.control, reason=f"{recipe.recipe_id}@{recipe.version} ({how})",
                     data={"recipe_id": recipe.recipe_id, "version": recipe.version, "status": recipe.status,
                           "inputs": inputs})
        if getattr(args, "inject", None):
            inject_before_step(s, args.inject, args.at_step)
        result = replay(recipe, inputs, s, operator, llm_used_for).run_result
        print_result(result, s.logger.folder.path)
        wait_before_closing(args, ctx)
    return result


def session(args, ctx: Context, mode: str, operator):
    approver = operator.approve if operator else reject_all
    return open_session(mode, settings=ctx.settings, policy=ctx.policy, runs_dir=ctx.runs_dir, approver=approver,
                        headless=ctx.headless, slow_mo_ms=600 if getattr(args, "slow", False) else 0,
                        action_delay_s=0 if getattr(args, "fast", False) else None)


def finish_offline(ctx: Context, mode: str, status: RunStatus, message: str, recipe: Recipe | None = None,
                   request: str | None = None) -> RunResult:
    """A result that needed no browser (no match, draft refused, invalid input): still a run folder."""
    folder = RunFolder(runs_dir=ctx.runs_dir)
    logger = RunLogger(folder, Masker(ctx.policy.mask_fields), echo=False)
    logger.log("catalog", status.value, ControlState.AUTOMATION, reason=message,
               data={"request": request} if request else {})
    result = RunResult(status=status, run_id=folder.run_id, mode=mode,
                       recipe_id=recipe.recipe_id if recipe else None,
                       recipe_version=recipe.version if recipe else None,
                       message=message, log_file=str(folder.log_path))
    logger.write_result(result)
    print_result(result, folder.path)
    return result


def exit_code(result: RunResult) -> int:
    """0 for a valid answer (success or a business outcome), 1 for everything else."""
    return 0 if result.status in (RunStatus.SUCCESS, RunStatus.BUSINESS_OUTCOME) else 1


def print_result(r: RunResult, folder: Path) -> None:
    print(f"\nResult: {r.status.value}" + (f" ({r.outcome_code})" if r.outcome_code else "")
          + (f" - {r.message}" if r.message else ""))
    if r.answered_by == "recipe":
        print(f"  answered by: RECIPE {r.recipe_id} v{r.recipe_version} (deterministic replay, no LLM decisions)")
    elif r.answered_by == "llm_discovery":
        print("  answered by: LLM DISCOVERY (no recipe was used)")
    elif r.recipe_id:
        print(f"  recipe: {r.recipe_id}" + (f" v{r.recipe_version}" if r.recipe_version else ""))
    if r.answered_by:
        print(f"  LLM used for: {', '.join(r.llm_used_for) or 'nothing (no LLM at all)'}")
    for name, value in r.outputs.items():
        print(f"  {name} = {value}")  # the caller sees real values; the run folder has them masked
    if r.failure:
        f = r.failure
        print(f"  failed at step {f.step} [{f.error_type}]\n    expected: {f.expected}\n    observed: {f.observed}")
        print("    evidence: " + ", ".join(Path(e).name for e in f.evidence))
    for note in r.recoveries + r.warnings + r.human_interventions + r.approvals:
        print(f"  note: {note}")
    print(f"  run folder: {folder}")


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    try:
        return dict(p.split("=", 1) for p in pairs or [])
    except ValueError:
        raise SystemExit("inputs must look like --input name=value")


def confirm(ctx: Context, question: str) -> bool:
    if not ctx.interactive:
        return False  # nobody to ask: the conservative answer is no
    while True:
        answer = input(f"{question} [yes/no]: ").strip().lower()
        if answer in ("yes", "no"):
            return answer == "yes"


def wait_before_closing(args, ctx: Context) -> None:
    if ctx.interactive and not getattr(args, "auto_close", False):
        input("\nBrowser left open so you can look around. Press Enter to close it...")


def inject_before_step(session, error: str, step: int) -> None:
    """Demo/testing only: arm one of the bank's test errors right before `step` acts. It calls the
    bank's /_admin switch directly (like curl), outside the browser and the automation allowlist."""
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


# ---------------------------------------------------------------- arguments
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="Computer-use automation for a legacy bank UI.")
    sub = parser.add_subparsers(dest="command", required=True)

    def browser_flags(p, inject=False):
        p.add_argument("--mock", action="store_true", help="use the mock LLM (no API key needed)")
        p.add_argument("--slow", action="store_true", help="slow the browser down even more")
        p.add_argument("--fast", action="store_true", help="no pause before each action")
        p.add_argument("--auto-close", action="store_true", help="close the browser as soon as the run ends")
        p.add_argument("--no-human", action="store_true",
                       help="no operator: reject risky steps, and stop instead of handing over")
        if inject:
            p.add_argument("--inject", choices=["popup", "slow_page", "session_expired", "server_error"],
                           help="demo/testing: arm a bank test error before a step")
            p.add_argument("--at-step", type=int, default=3, help="the step --inject fires before (default 3)")

    p = sub.add_parser("list", help="show the recipe catalog")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("approve", help="review a draft recipe and mark it approved")
    p.add_argument("recipe_id")
    p.add_argument("--version", help="a specific version (default: the latest)")
    p.add_argument("--yes", action="store_true", help="approve without the confirmation prompt")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("discover", help="run the discovery agent on a goal")
    p.add_argument("goal")
    browser_flags(p)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("replay", help="strict: run a recipe, never fall back to the LLM")
    p.add_argument("request", nargs="?", help="plain-English request (matched to a recipe)")
    p.add_argument("--recipe", help="recipe id: skip matching, no LLM at all")
    p.add_argument("--input", action="append", metavar="NAME=VALUE", help="recipe input (repeatable)")
    p.add_argument("--allow-draft", action="store_true", help="allow running a draft recipe")
    browser_flags(p, inject=True)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("ask", help="main: use a recipe if one fits, otherwise discover one")
    p.add_argument("request")
    browser_flags(p, inject=True)
    p.set_defaults(func=cmd_ask)
    return parser


def main(argv: list[str] | None = None, ctx: Context | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    ctx = ctx or Context(settings=load_settings(), policy=load_policy(), interactive=sys.stdin.isatty())
    return args.func(args, ctx)


if __name__ == "__main__":
    sys.exit(main())
