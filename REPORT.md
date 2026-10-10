# Report

The model discovers, the recipe becomes the capability, and deterministic replay is how an agent invokes it.
Everything below is built and tested (208 tests) unless marked as design or listed under Cuts.

## Architecture

```
ask / replay / discover / approve / list          (run.py, one program)
   |-- catalog: recipes, versions, approval; request -> recipe + typed inputs (LLM or mock)
   |-- discovery agent (LLM) --> recorder --> recipes/<id>@<version>.json (draft)
   |-- replay engine (no LLM) --(hard failure, ask only)--> bounded LLM recovery --> human takeover
   v
Session.perform(action): control check -> find element -> safety guard -> approval -> act -> masked log
   v
Browser layer (the only Playwright code)  -->  fake legacy bank (Flask + SQLite, :5050)
```

- Two brains, kept apart. The LLM does what needs judgement (discovering a flow, matching a request, one
  bounded repair); the recipe does what needs repeatability (production replay), because LLM steps are slow,
  costly and non-deterministic. A test checks that `src/replay` imports no LLM code; recovery reaches replay
  only as a callback that `ask` passes in.
- One gate for every action. Discovery, replay, recovery and login all act through `Session.perform`, so
  safety and control are enforced in one place; a test fails if any other code clicks, types or navigates.
- The browser layer is the surface seam: only `src/browser/` imports Playwright and it returns its own types,
  so another surface replaces this layer, not the recipes or the engine.
- The agent reads the accessibility tree, not pixels: roles and names are semantic, survive restyling and
  exist on desktop apps too; coordinates break on any layout change. The tree is compacted (about 40%),
  because nested legacy tables repeat their text at every level.
- Goal plus target as input: `discover` and `ask` take an optional `--target` (app and start page), checked
  against the allowlist before anything starts; otherwise `config/settings.json` decides.
- One process with terminal commands: the brief asks for a vertical slice, not infrastructure.
- The fake bank is deliberately awkward (nested tables, inputs named `f1`, no test IDs, weak labels, page names
  repeated in navigation), with errors that fire only when triggered, so demos are repeatable.
- The LLM client is provider-agnostic: each turn is a fresh prompt with one required tool call, which bounds
  tokens and avoids provider-specific formats. Groq (`openai/gpt-oss-120b`) was chosen after testing tool calls.

## Artifact schema

A recipe (`src/models/recipe.py`, example `evidence/example_recipe.json`) has two parts. The contract is
what a calling agent reads: typed `inputs` (regex pattern, sensitivity), typed `outputs`, `outcomes`
(SUCCESS plus business codes) and a `success_check`. The execution part is what replay runs: `steps` and
`error_handlers`. A caller can decide whether to invoke a capability without reading its steps, and steps can
change in a new version without changing the contract.

- Placeholders, never values: typed values and URLs hold `{{member_id}}`. The recorder refuses to store a
  sensitive-looking value, or any value from the goal, as a fixed value, because recipes are shared.
- Several locators per element, each proven at record time to match exactly one visible element and the same
  one: role and name, label, link text, `near_text` (the value next to a stable label, since values change
  per record), and CSS last, with a `why` for reviewers. No coordinates.
- Checkpoints use the page heading (`expect_page` before a step, `wait_for` after it), because text anywhere
  cannot identify a page: "Member Search" is a link on every page (found in testing).
- `risk: irreversible` marks data-changing clicks; `source: human` marks steps a person did in a takeover.
- Error handlers come from shared app rules (`config/app_rules.json`), since they belong to the app, not one
  flow; `default_on_unknown` is fixed to hard failure, so nothing unrecognised is guessed past.
- Validated on load: unknown fields, unused or undeclared placeholders, outputs without an extract step, and
  undeclared business codes are rejected, so a broken recipe fails before it touches a bank screen.
- Versioned as `<id>@<x.y.z>.json`; re-discovery bumps the minor version and keeps the old file. The active
  version is the latest approved one, so a new draft never replaces production unreviewed. Approval (via
  `approve`, or after `ask` replays a draft successfully) records who, when and why. `needs_review` (human
  steps, inputs the system declared itself) forces the full review. `app.entry_url` binds the recipe to its app.

## Determinism & error handling

- Locators are tried in order and count only if they match exactly one visible element: the balance appears
  twice on the member page, so a text match is skipped, never guessed. A backup match logs a drift warning.
- Inputs are validated before a browser opens (`INVALID_INPUT`).
- Each step: `only_if`, error handlers, `expect_page`, the action, then `wait_for` (10 s from the action) while
  watching the handlers; then the success check.
- Three result kinds, checked in this order: business outcomes (`MEMBER_NOT_FOUND` is an answer, not a
  crash), hard failures, then recoverable conditions (popup, slow page) bounded by `max_attempts`. Anything
  unknown is a hard failure with step, expected, observed, screenshot, masked accessibility tree and trace.
- Retry rule: never redo an action that may have changed data. Slow page: keep waiting. Popup before an
  action: the click never happened, so dismiss it and act. Popup after: dismiss it and keep waiting. Only
  navigate, type and extract may be re-run. Testing showed why: with a slow page, Playwright's click timed out
  although the click had happened. Clicks now return once dispatched, a covered click raises a distinct "not
  performed" error, and each outcome carries `performed: True / False / unknown`.
- Bounded LLM recovery (`ask` only): for unexpected failures, one attempt at that one step, at most 3 actions
  through the gate, risky clicks blocked, typing limited to this run's inputs. It ends when the step's own
  action succeeds (with Groq, a model asked to declare success kept repeating a correct action), and replay
  re-verifies the step. Never for known bad states or irreversible steps; the recipe file is never changed.

## Heterogeneity & multi-tenant

*Design, except where noted.*

- The seam: recipes speak in strategies, page conditions and actions; only the browser layer maps them to a
  surface. A desktop surface implements the same methods on OS accessibility APIs (UI Automation, macOS AX),
  which expose role, name and value, so role/name and `near_text` recipes carry over and CSS becomes an
  automation ID. Framesets add a frame path. Terminal and image-only apps get a vision surface, the only place
  coordinates appear, flagged in review. `app.surface` already exists.
- Reuse across banks. Built: a recipe is bound to its app, and `ask --target` only offers recipes of that app,
  so one bank's recipe never runs silently on another's instance. Design: the base recipe belongs to the
  vendor product and version; each bank adds a small reviewed overlay limited to presentation (labels, extra
  locators, error texts, entry URL), never steps or contract, validated by the same schema.
- Drift: drift warnings and recoveries are recorded per run; aggregated per tenant they trigger re-discovery
  into a draft. Comparing the app's reported version (the bank shows `v4.2`) with `app.version`, plus a
  scheduled canary replay per tenant, catches a broken recipe before a real request does.

## Escalation & handoff

- Stuck: the same action failed twice, a click changed nothing, the model asked for help, or a limit was hit
  (discovery); a hard failure that recovery could not fix (replay).
- Control goes `PAUSED`; a screenshot and `intervention_<n>.json` (goal or recipe, step, reason code, masked
  detail and URL) are saved and shown in the terminal. Then `HUMAN`: the person uses the same browser window,
  `perform` refuses to act until control is `AUTOMATION` again, and every change of control is logged.
- An injected page script reports the person's clicks, typing and choices (never passwords), logged masked.
- `resume` continues at the latest step whose `expect_page` matches, moved back to the first step recorded on
  that page (resuming at "click Search" would submit an empty box). No match is a hard failure, `abort` gives
  `ABORTED_BY_OPERATOR`, and after 3 takeovers the result is `ESCALATED`. In discovery, the person's actions
  become `source: human` steps. Evidence: `evidence/human_takeover/`.
- Production: the `Operator` interface (terminal and scripted versions built) backed by an operator queue
  routed on the reason code, with remote co-browsing.

## Safety

- The allowlist (`config/policy.json`: domains, the bank's paths without `/logout` or the `/_admin` test switch,
  action types) is enforced in code on the current page and every destination, after normalising the path.
  The LLM proposes and the code decides, so page text cannot talk its way past it.
- Risky actions: the form is filled, then the run pauses before the data-changing click and a person answers
  yes or no from a masked summary. Blocking would make the tool useless; a flag alone would still change data.
  Risk is judged on the element's recorded identity, so a CSS fallback cannot hide a Confirm button.
- Masking, one function for all logs and results, in three layers: field names (`member_id` as `***45`),
  this run's known values, and patterns (account numbers, phones, amounts). A test scans run folders after
  discovery, replay, a failure and a takeover for the members' real data.
- Credentials only in `.env`; tracing starts after login, because traces record typed values.
- Limits: screenshots, failure trees and traces show the page as it was (local, fake data; production needs
  redaction). Discovery sends the page tree to the LLM provider; replay sends nothing. Risky button names and
  error texts must be maintained per app. Names are masked only when they are a known input or output.

## Cuts

- Operator console (a terminal prompt instead; the control model is real), desktop surface, tenant overlays
  and drift aggregation: designed, not built, to go deep on the core.
- Stretch goals built: the agent-facing catalog (`ask` matches a request to a recipe with typed arguments) and
  assisted fallback (bounded recovery). Approval gating is part of the safety model; confidence scoring, code
  generation and stability statistics were not built.
- Not built: a web API for the catalog, redaction of screenshots and traces. Anthropic and OpenAI clients are
  untested.
- Fixed after the real runs: under-declared inputs (an amount stored as a fixed value and logged unmasked),
  Groq rejecting tool calls server-side, LLM calls without a timeout, traces capturing the password.
- Next: tenant overlays with drift monitoring, a desktop surface on UI Automation, redaction, canary replays.
