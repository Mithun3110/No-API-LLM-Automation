# PROJECT CONTEXT: Computer-Use Automation System

This file contains everything needed to build this project from an empty repo to submission: the assignment, every design decision, the architecture, file formats, the build order, and the deliverables. Treat it as the single source of truth. If something here is unclear or seems wrong, ask before changing the design.

---

## PART A: THE ASSIGNMENT

### A1. Who and why
- Company: interface.ai. They build AI agents for banks and credit unions.
- This is a take-home project for an engineering role.
- Submission: a public GitHub repo, link emailed to assignments@interface.ai (repo URL on its own line, from the email address used to apply, no zip file).
- No deadline. They want a focused effort, not a polished product. Target: about 2 weeks.
- AI-assisted development is expected. The candidate must be able to explain and defend every line and every decision in an interview.

### A2. The problem in their words (summarised)
Banks run many legacy back-office apps (core banking screens, servicing tools, admin consoles) that have no API. The only way in is to drive the UI like a human operator. The system should:
1. Take a goal in natural language for a target app (e.g. "look up member 12345 and read their current savings balance").
2. Use an LLM to accomplish the goal by driving a real UI (observe, decide, act).
3. Record the successful run as a structured, reusable, typed, versioned artifact (a "capability"), separate from the raw model transcript.
4. Replay that artifact deterministically, with no LLM in the decision loop, using stable element targeting, and report success or failure.
5. Escalate to a human when stuck: the human takes control of the same live session, then hands control back.
6. Stay within safety guardrails: an allowlist, careful handling of risky actions, and no leaking or saving of sensitive data.

Their one-line summary: the model discovers, the artifact becomes a reusable capability, and deterministic replay is how the AI agent invokes it in production.

### A3. The real environment they care about
- Stable UIs but real runtime errors: validation errors, record not found, permission denied, unexpected dialogs, session timeout, slow loads, app errors. Replay must handle these, not only the happy path.
- Heterogeneous, legacy surfaces: modern web, legacy web (framesets, nested tables, no test IDs), and native desktop apps. Cannot assume a clean DOM or stable selectors.
- Multi-tenant at scale: hundreds of banks, about 20 apps each. Many banks run the same vendor product with different config, branding, and versions. Automation should be reusable across them.
- API integration is out of scope. The system must use the UI.

### A4. Core requirements (must-have, all of them)

3.1 Goal-driven agent loop
- Input: a goal plus a target (app, URL, entry point).
- LLM-driven observe, decide, act loop on a live UI until the goal is met or a stop condition (max steps, timeout, dead end).
- Must really click, type, navigate, and read state. Prefer an approach that still works without a clean DOM.

3.2 Structured artifact (agent-invocable capability)
- Typed, serialisable, with a clear contract, not just a step list.
- Must include: ordered steps; how each element is identified, with reasoning about robustness; typed input parameters; typed outputs and their shape; a checkpoint or success condition.
- Versioned and reviewable by both humans and calling agents. The schema is a focal point of evaluation.

3.3 Deterministic replay
- Saved artifact plus input parameters, replayed without the LLM making decisions.
- Stable element targeting, checkpoint verification, returns declared outputs.
- Detects and handles runtime errors explicitly. The result contract must separate:
  - expected business outcomes (e.g. "no such member" is a valid result, not a crash),
  - recoverable conditions (dismiss a known popup, wait and retry a slow load),
  - hard failures (stop with a clear, debuggable error).
- Structured result: success with outputs, a known business outcome, or a failure with step, expected, and observed.

3.4 Safety and policy guardrails
- Explicit, configurable allowlist (domains, routes, action types). The agent must not act outside it.
- Separate safe or reversible actions from risky or irreversible ones, and handle risky ones conservatively, with justification.
- Never save secrets or raw sensitive data (credentials, tokens, full PII) in artifacts or logs. Redact.

3.5 Evidence and observability
- Structured log of what the agent did and why.
- At least one richer signal on failure (screenshot, DOM snapshot, trace).

3.6 Human-in-the-loop escalation and handoff
- Detect a stuck or blocked state and raise an intervention request with context: goal or capability, current step, current state or screenshot, and why it stopped.
- The human operates the same live session, not a fresh one, then hands control back so the run resumes or completes. Preserve context and evidence, and record what the human did.
- Automation must be able to pause, give up control, and resume on the same session. There must be a clear model of who is in control.
- A full operator console is out of scope. A minimal but real handoff is required. The operator UI can be mocked; the mechanism and control model must be real.

3.7 Heterogeneity and scale (design in the write-up, not built)
- Surface abstraction: how the artifact and replay extend to legacy web and desktop apps. What is the seam between "how we perceive and act on a surface" and "the recorded flow"?
- Multi-tenant reuse: how one artifact is reused or safely overridden across banks running the same app, and how per-bank or per-version drift is detected and managed.

### A5. Scope rules
- A complete end-to-end vertical slice touching every core requirement: goal, LLM run, saved artifact, deterministic replay with inputs, outputs, and error handling, human escalation on the live session, evidence for both runs.
- Go deep on: the artifact schema, deterministic replay plus error handling, and the safety and escalation model.
- Cut depth, not whole capabilities. Thin but real everywhere. Mocking at a clean seam is fine if documented.
- If live LLM or browser access is not available, mock the boundary cleanly and document it.
- Do not build scaling infrastructure (queues, clusters, multi-tenant plumbing). Design for it, don't build it.

### A6. Evaluation criteria (in their order of importance)
1. System design: clear boundaries, sensible data models, good trade-offs, simplicity. Artifact schema and replay contract are central.
2. Correctness of the core loop: the agent completes a real goal; the artifact replays deterministically and verifies success.
3. Robustness and error handling: business outcomes vs. recoverable vs. hard failures; locator, wait, and checkpoint strategy.
4. Human-in-the-loop escalation: real detection, routing with context, live session transfer, resume.
5. Generalisation: credible story for other surfaces and multi-tenant reuse.
6. Safety and data handling: allowlist, risky actions, redaction.
7. Code quality: readable, typed and tested where it counts, easy to run.
8. Communication: the write-up explains reasoning, trade-offs, and cuts.
Not rewarded: feature breadth, framework name-dropping, building scaling infrastructure.

### A7. Optional stretch goals (we chose two that build on the core)
Chosen:
- Agent-facing capability interface (capability catalog): the LLM picks a recipe by name from a plain-English request and invokes it with typed arguments.
- Assisted fallback (bounded LLM recovery): on replay failure, a bounded, policy-checked LLM recovery for a single step, recorded as evidence.
Light support (field exists, enforcement simple):
- Approval state: recipes are `draft` or `approved`.
Not chosen: code generation, cross-tenant demo, multi-run stability.

### A8. Required deliverables (exact paths and headings)
1. Public repo with `/README.md`:
   - How to set up and run, including keys and config, and how to run without live services (mock LLM mode).
   - Demo path: exact commands to run the agent on a goal, then replay the resulting artifact.
2. `/REPORT.md`, about 1 to 3 pages, with exactly these headings:
   - `## Architecture`
   - `## Artifact schema`
   - `## Determinism & error handling`
   - `## Heterogeneity & multi-tenant`
   - `## Escalation & handoff`
   - `## Safety`
   - `## Cuts`
3. `/evidence/` with: a saved example artifact, logs from a discovery run, logs from a replay run, and ideally a replay that hits an error (bad input, not found, or injected failure). A screen recording is optional (we are not doing one).

### A9. Ground rules
- No real bank systems, real credentials, or real PII. Fake data only.
- Keep secrets out of the repo.

---

## PART B: ALL DECISIONS

| Area | Decision | Why |
| --- | --- | --- |
| Machine | macOS | Developer's machine |
| Language | Python 3 | One language for everything; best AI and automation libraries |
| Target app | Fake bank website built with Flask | Same language; tiny; easy to inject errors; server-rendered HTML like legacy apps |
| Bank data store | SQLite (built-in `sqlite3`, plain SQL, no ORM), seeded from `members.json` on first start; money stored as integer cents; data changes use transactions | Looks like a real back-office system; every query is visible and explainable |
| Website style | Slightly messy legacy: table layouts, no test IDs, non-semantic names like `f1` | Matches the "no clean DOM" reality |
| Automation program | Plain Python, one program, terminal commands | Assignment says not to build infrastructure |
| Browser control | Playwright (Chromium), visible window | Accessibility tree, role-based locators, screenshots, traces; human can use the same window |
| How the agent sees the page | Accessibility tree only for decisions | Small, stable, semantic, and exists on desktop apps too |
| Screenshots | For logs and evidence only, not for clicking | Evidence on failure; coordinate clicking is fragile |
| LLM | Claude or GPT with tool calling (key added later) | Structured actions instead of free text |
| No API key yet | Mock LLM mode built first | Reviewers can run without a key; clean seam |
| Recipe format | JSON, schema defined with Pydantic | Readable by humans and agents; validated |
| Recipes saved | Only after a successful discovery run, status `draft` | Never save a failed or partial flow |
| Approval | `draft` then `approved` via a command | Banks should not run unreviewed automation |
| Error triggers | Special member IDs for data errors plus a config setting for system errors | Realistic and repeatable demos |
| Random errors | Only when triggered, never random | Repeatable evidence |
| Agent step limit | 20 | Stops runaway loops |
| Stuck in discovery | Human takeover, and log what failed and why | Requirement 3.6 |
| Replay wait time | 10 seconds | Then counts as slow |
| Recoverable retries | 3 | Bounded |
| Backup locator used | Continue, log a drift warning; backup must match exactly one element | Keeps working but surfaces drift; never guesses |
| Allowed actions | Read and navigate freely; data-changing clicks only with human approval; nothing outside the bank app | Usefulness vs. bank safety |
| Risky actions | Fill in everything, pause before the final click, human approves or rejects | Blocking is useless; flagging is too weak |
| Masking in logs | Member IDs (last 2 digits), balances, names, phone numbers, passwords | Regulated financial data |
| Screenshots privacy | Kept local, all data fake; documented as a limit | Honest about the gap |
| Human signals | Type `resume` or `abort` in the terminal | Minimal but real operator surface |
| Human actions | Recorded (clicks and typing), masked | Requirement 3.6 |
| Log format | JSON lines, one entry per event | Structured, easy to read and parse |
| Traces | Playwright trace saved on failure | Rich debugging signal |
| Main command | `ask` | LLM picks the recipe; falls back to discovery or recovery |
| Strict command | `replay` | LLM may only match the request to a recipe; never acts |
| Pure deterministic replay | `replay --recipe <id> --input k=v` skips matching entirely; used in the README demo | Clearest proof that replay uses no LLM at all |
| Draft recipes in strict replay | Refused unless `--allow-draft` is passed | Strict mode should be strict |
| Retry rule | Never redo an action that may have changed data (see C1.6) | Avoid double submissions |
| Resume point | Optional `expect_page` on each step; resume at the latest step whose `expect_page` matches | Deterministic, checkable resume rule |
| Human steps in discovery | Recorded into the recipe as steps with `"source": "human"`; recipe saved with `needs_review: true` | Recipe stays complete and replayable |
| Re-discovery of an existing recipe | Bump minor version, save as a new draft file `recipes/<recipe_id>@<version>.json`, keep the old file | Old version stays active until the new one is approved |
| Masking format | Member IDs `***45` (last 2 digits); balances, names, phones, addresses, passwords fully `***` | Regulated data; last digits help debugging |
| LLM provider | Anthropic by default; provider-agnostic client, switchable to OpenAI via `.env`. The user has a Groq key in `.env` (Groq is OpenAI-compatible); confirm provider wiring in step 6 | Real key available from step 6 |
| Git | Commit after each step once the user has tested and confirmed; the user pushes | Clear history, user stays in control |
| Tests | pytest, focused on recipe schema, error classification, safety, replay against the fake bank | Tested where it counts |
| Flask port | 5050 (not 5000) | macOS AirPlay Receiver uses port 5000 |
| Test isolation | Tests start their own bank on port 5051 with a throwaway DB (`BANK_PORT`, `BANK_DB_PATH` env overrides) | Tests never touch the user's bank or data; always start from seed |

---

## PART C: ARCHITECTURE

### C1. Components

1. Fake bank website (`bank_app/`)
   The target. Knows nothing about AI. See Part F.

2. Command line (`run.py`)
   - `ask "<request>"`: main command.
   - `replay "<request>"`: strict, recipe-only execution (LLM or mock keyword rules only match the request).
   - `replay --recipe <id> --input <name>=<value> ...`: pure deterministic replay, no LLM anywhere. Used in the README demo.
   - `--allow-draft`: lets `replay` run a `draft` recipe (otherwise refused).
   - `discover "<goal>"`: run discovery directly (useful for the README demo and evidence).
   - `approve <recipe_id>`: mark a draft recipe approved.
   - `list`: show the recipe catalog.
   - Flag `--mock`: use the mock LLM.

3. Recipe catalog (`src/catalog/`)
   - Loads all recipes from `recipes/`. Files are named `<recipe_id>@<version>.json`. For each recipe id, the catalog uses the latest `approved` version; if none is approved, the latest `draft` (which `ask` confirms before running and strict `replay` refuses without `--allow-draft`).
   - Gives the LLM a menu (each recipe's id, name, description, inputs, outputs). Each recipe is presented as a tool; the LLM calls one with typed arguments, or calls `no_match`.
   - Validates the extracted inputs against the recipe's types and patterns before anything runs. Invalid input returns `INVALID_INPUT`.
   - In mock mode, matching is done with simple keyword rules.

4. Discovery agent (`src/agent/`)
   - First, the LLM defines the task: a proposed recipe id, name, and description; the inputs it will use, with their values from the goal (e.g. `member_id = 12345`); and the outputs it must return. This is how the recorder later knows which typed values become placeholders.
   - Then the loop: read the accessibility tree, the LLM returns exactly one action through tool calling, the safety layer checks it, the browser layer performs it, the result is logged, repeat.
   - Actions the LLM can choose: `navigate`, `click`, `type`, `select`, `extract`, `wait`, `done`, `ask_human`.
   - Targets are referenced by role and name as seen in the accessibility tree.
   - Stops on: `done` (goal met), 20 steps, timeout, or stuck.
   - Stuck means: the same action failed twice, the page did not change after an action, the LLM called `ask_human`, or the step limit was reached. Stuck leads to human takeover.
   - Mock LLM: plays back scripted decisions from `config/mock_scripts/<name>.json`, so discovery runs without a key.

5. Recorder (`src/recorder/`)
   - Runs only after a successful discovery run.
   - For each successful action, records several locator strategies for the element (role and name, label, nearby text, CSS as last resort), a description, the risk level, and what appeared after the action (`wait_for`).
   - Replaces input values with placeholders, e.g. `12345` becomes `{{member_id}}`.
   - Fills `expect_page` for each step automatically: from the previous step's `wait_for`, or from the page heading for step 1.
   - Steps the human performed during a takeover are included as normal steps with `"source": "human"` (others are `"source": "agent"`), and the recipe is saved with `needs_review: true`.
   - Adds the shared error rules from `config/app_rules.json`.
   - Saves the recipe as `draft`, version `1.0.0`, with provenance (which run created it), to `recipes/<recipe_id>@1.0.0.json`. Never stores real values.
   - If a recipe with the same id already exists, bumps the minor version of the latest one (1.0.0 -> 1.1.0) and saves a new draft file. The old file is kept and stays active until the new one is approved.

6. Replay engine (`src/replay/`)
   - Loads a recipe and inputs. No LLM is ever called from this module.
   - Validates inputs first.
   - For each step: check `only_if` (skip the step if its condition is not met), check error rules, check `expect_page` (wrong page is caught early and handled like any other unexpected state), find the element (strategies in order, must match exactly one; a backup match logs a drift warning), perform the action through the safety layer, wait for `wait_for` (up to 10 seconds), then check error rules again.
   - Error handling:
     - Business outcome: stop, return the result code.
     - Recoverable: apply the fix, up to 3 attempts, following the retry rule below.
   - Retry rule: never redo an action that may have changed data.
     - Slow page: keep waiting for `wait_for` (up to the retry limit). Do not redo the action.
     - Popup before an action: dismiss it, then do the action.
     - Popup after an action: dismiss it, then continue waiting for `wait_for`. Do not redo the action.
     - Only safe, repeatable steps (`navigate`, `type`, `extract`) may be fully re-run.
     - This rule is explained in REPORT under `## Determinism & error handling`.
     - Hard failure or anything unknown: stop, save a screenshot, accessibility snapshot, and trace; return a failure with step, expected, and observed.
   - At the end, verify the success check and return the outputs.
   - What happens after a hard failure depends on the caller: `ask` tries bounded LLM recovery; `replay` goes straight to human takeover.

7. Bounded LLM recovery (`src/recovery/`), used only by `ask`
   - Triggered only by a hard failure, never by a business outcome.
   - The LLM sees the current accessibility tree, the failed step, and its description, and may try to complete only that one step.
   - Limits: one step, at most 3 actions, every action goes through the safety layer, and it may never perform a risky step.
   - If it succeeds, replay continues from the next step and the recipe is flagged `needs_review`. The recipe file is never changed automatically.
   - If it fails, human takeover.
   - Every attempt is logged as evidence.

8. Safety layer (`src/safety/`)
   - Every action from discovery, replay, recovery, and the catalog passes through it before the browser runs it. Enforced in code, never only in a prompt.
   - Allowlist check: domain, path, and action type from `config/policy.json`.
   - Risk check: clicks on buttons that change data (from the risky list in the policy, plus any step marked `irreversible`) pause for human approval.
   - Masking: one function used by all logging, masking sensitive inputs, outputs, and known patterns. Three layers (`src/safety/masking.py`): (1) field names matching `mask_fields` (`member_id`, `savings_balance`, `new_phone`, ...); (2) known values of this run (password, inputs, outputs) wherever they appear in free text; (3) patterns: 8+ digit numbers (accounts), phones, money, 5-digit numbers (member IDs as `***45`; zip codes are masked too).
   - `mask_fields` also includes `amount` and `deposit` (money typed into forms).
   - The guard (`src/safety/guard.py`) is a pure function: `check(ProposedAction) -> Decision(allow | needs_approval | block, rule, reason)`. It checks the action type, the current page URL AND the navigate destination (normalised, so `/member/../_admin` cannot slip through), and flags clicks on buttons whose name contains a risky word (whole word, case-insensitive), or any `irreversible` step. Links are never risky (they only open pages); an unknown role is treated like a button. `allow_risky=False` turns approval into a block for bounded recovery.
   - Where it is enforced: the session layer (step 5) owns the only path to the browser for actions and calls the guard before every action.

9. Session and control (`src/handoff/`)
   - One browser session per run, shared by the automation and the human.
   - Logs in automatically at the start using `BANK_USERNAME` and `BANK_PASSWORD` from `.env`. Never logged or saved.
   - Control state: `AUTOMATION`, `HUMAN`, or `PAUSED`. Only one can act. Every change is logged.
   - Intervention request: printed in the terminal and saved as JSON in the run folder, with goal or recipe, current step, reason, screenshot path, and current URL.
   - While the human is in control, a small script injected into the page records their clicks and typing (masked) and sends them back to Python through Playwright's `expose_binding`.
   - The human types `resume` or `abort`. On `resume`, the automation reads the page again and continues from the latest step whose `expect_page` matches the current page. If no step matches, it is a hard failure. (In discovery, resume simply returns control to the agent loop, which re-reads the page.)
   - Risky action approval: the terminal shows exactly what will happen (masked) and asks `yes` or `no`.

10. Browser layer (`src/browser/`)
    - The only module that imports Playwright.
    - Functions: open, goto, accessibility snapshot, find element by strategies, click, type, select, read text, screenshot, start and stop trace, current URL.
    - This is the surface seam. A desktop version would implement the same functions with a desktop accessibility API.
    - Returns its own types (`Element`, `FindResult`, `BrowserError`), never Playwright objects or exceptions, so callers do not depend on Playwright.
    - `find` tries strategies in order; a strategy counts only if it matches exactly one VISIBLE element. `FindResult` records every attempt (e.g. `role=button "Search": 1 match`) and whether a backup was used (drift).
    - `compact_snapshot` strips the repeated names of layout containers (nested tables repeat all inner text at every level), about 40% smaller, so the LLM sees each text once.
    - Short action timeout (5s) so a click blocked by a popup fails fast with "blocked by <overlay>" for the replay engine to classify.
    - Manual check: `python -m src.browser.demo [member_id]` (bank must be running).

11. Logging and evidence (`src/logs/`)
    - Each run gets a folder: `runs/<run_id>/` with `log.jsonl`, `result.json`, screenshots, traces, and intervention requests.
    - Log entry fields: timestamp, run_id, mode (discover, replay, recovery, human), step, action, target, reason, outcome, controller, warnings.
    - All values pass through the masking function before writing.
    - Run ids look like `20261008T203012Z-a3f9` (UTC, matching log timestamps). `result.json` is masked like the log, except structural fields (ids, status, evidence paths); the caller still receives real output values.
    - Selected runs are copied into `evidence/` for submission.

12. Models (`src/models/`)
    - Pydantic models for: Recipe (and its parts), RunResult, LogEntry, InterventionRequest, Policy.

### C2. How the parts connect

```
                           You (terminal)
                                 │
        ┌──────────────┬─────────┼───────────┬───────────┐
        ▼              ▼         ▼           ▼           ▼
       ask          replay    discover    approve       list
        │              │         │
        ▼              ▼         │
   Recipe catalog ◄──► LLM       │
        │                        │
   ┌────┴─────────┐              │
 match        no match ──────────┤ (ask only)
   │                             ▼
   ▼                      Discovery agent ◄──► LLM / mock LLM
 Replay engine                   │
   │                             ▼
   │ hard failure           Recorder ──► recipes/*.json (draft)
   │ (ask only)
   ▼
 Bounded LLM recovery
   │
   │  every action from every mode
   ▼
 Safety layer (allowlist, risk approval, masking)
   │
   ▼
 Session and control (AUTOMATION / HUMAN / PAUSED)
   │
   ▼
 Browser layer (Playwright) ──► Fake bank website (Flask, :5050)
   │
   ▼
 runs/<run_id>/ logs, screenshots, traces ──► evidence/
```

### C3. Flows

`ask` (main)
1. `python run.py ask "What's the savings balance for member 12345?"`
2. The catalog's LLM picks a recipe and inputs, or returns no match. Inputs are validated.
3. If the recipe is `draft`, ask the user to confirm before running.
4. Replay runs. Success returns outputs. Business outcome returns the code. Hard failure tries bounded recovery, then human takeover.
5. No match: run discovery on the request. On success, save a new draft recipe and return the result.

`replay` (strict)
1. `python run.py replay "What's the savings balance for member 12345?"`, or with no LLM at all: `python run.py replay --recipe member.lookup_savings_balance --input member_id=12345`
2. The LLM may only match the request to a recipe (skipped entirely with `--recipe`). No match prints "No matching recipe" and stops.
3. If the recipe is `draft`, refuse with "Recipe is draft; approve it first or pass --allow-draft".
4. Replay runs with no LLM. Hard failure goes straight to human takeover.

`discover`
1. Log in, open the entry page, the LLM defines the task (inputs, outputs).
2. Loop: tree, LLM action, safety check, browser, log.
3. Risky action: human approval. Stuck: human takeover.
4. Done: the recorder saves the draft recipe.

Human takeover
1. Control to `PAUSED`, intervention request printed and saved.
2. Control to `HUMAN`. The human acts in the same browser; actions are recorded.
3. `resume`: control to `AUTOMATION`, re-read the page, continue at the matching step. `abort`: result `ABORTED_BY_OPERATOR`.

Risky action approval
1. The form is filled in. The system stops before the click that changes data.
2. The terminal shows the masked summary and asks `yes` or `no`.
3. `yes` performs the click. `no` returns `REJECTED_BY_OPERATOR`. The decision is logged.

---

## PART D: FILE FORMATS

### D1. Recipe format (template)
Real recipes use real booleans and numbers, not quoted placeholders.

```json
{
  "schema_version": "1.0",
  "recipe_id": "<unique id, e.g. member.lookup_savings_balance>",
  "name": "<short readable name>",
  "description": "<one line: what this recipe does>",
  "version": "<x.y.z>",
  "status": "<draft | approved>",
  "needs_review": false,
  "app": { "name": "<app name>", "version": "<app version>", "surface": "web", "entry_url": "<start URL>" },
  "provenance": { "recorded_from_run": "<run id>", "recorded_at": "<timestamp>" },

  "inputs": {
    "<input_name>": {
      "description": "<what this input is>",
      "type": "<string | number | currency>",
      "pattern": "<format rule>",
      "required": true,
      "sensitive": true
    }
  },

  "outputs": {
    "<output_name>": {
      "description": "<what this output is>",
      "type": "<string | number | currency>",
      "sensitive": true
    }
  },

  "outcomes": {
    "SUCCESS": "<meaning>",
    "<BUSINESS_OUTCOME_CODE>": "<meaning>"
  },

  "steps": [
    {
      "step": 1,
      "description": "<what this step does, in plain words>",
      "source": "<agent | human>",
      "expect_page": { "text_visible": "<text that proves we are on the right page before this step, e.g. Member Search>" },
      "action": "<navigate | click | type | select | extract>",
      "target": {
        "strategies": [
          { "by": "role", "role": "<button | textbox | link | combobox>", "name": "<visible name>" },
          { "by": "label", "text": "<label text>" },
          { "by": "text", "text": "<visible text>" },
          { "by": "css", "value": "<last resort selector>" }
        ],
        "why": "<why these ways of finding it are reliable>"
      },
      "value": "<{{input_name}}, only for type/select>",
      "save_as": "<output_name, only for extract>",
      "parse": "<currency | text | number, only for extract>",
      "only_if": { "text_visible": "<optional: run this step only if this is on the page>" },
      "risk": "<safe | irreversible>",
      "wait_for": { "any_of": [ { "text": "<text expected after this step>" } ] }
    }
  ],

  "success_check": {
    "description": "<how we know it worked>",
    "text_visible": "<text that proves it worked>",
    "outputs_present": ["<output_name>"]
  },

  "error_handlers": [
    { "id": "<id>", "description": "<meaning>", "scope": "<any_step | after_step:N>", "when": { "text": "<text on page>" },  "type": "business_outcome", "result": "<RESULT_CODE>" },
    { "id": "<id>", "description": "<meaning>", "scope": "any_step", "when": { "dialog": "<popup name>" }, "type": "recoverable", "fix": { "action": "click", "target": { "strategies": [ { "by": "role", "role": "button", "name": "OK" } ] } }, "max_attempts": 3 },
    { "id": "<id>", "description": "<meaning>", "scope": "any_step", "when": { "wait_timed_out": true }, "type": "recoverable", "fix": { "action": "wait", "seconds": 5 }, "max_attempts": 3 },
    { "id": "<id>", "description": "<meaning>", "scope": "any_step", "when": { "text": "<text on page>" },  "type": "hard_failure", "then": "escalate" }
  ],

  "default_on_unknown": { "type": "hard_failure", "then": "escalate" }
}
```

Schema source of truth: `src/models/recipe.py` (Pydantic). The template above is a summary; additions made in step 2:
- `url` on `navigate` steps (path relative to the entry URL's origin). Navigate needs a destination.
- `near_text` locator strategy: `{ "by": "near_text", "anchor": "Savings Balance:", "relation": "right_of" }`. Finds the element next to a stable label. Needed for `extract`, because the value itself changes per member and cannot be the locator.
- `exact` on role strategies (default true), so "Search" does not also match "Member Search".
- `wait_for` conditions may be `{ "text": ... }` or `{ "url_contains": ... }`.
- Validation rules (all tested in `tests/test_models.py`): unknown fields rejected; steps numbered 1..n; every `{{placeholder}}` is a declared input and every input is used; every output is produced by exactly one extract step; business outcome codes must be declared in `outcomes`; `outcomes` includes SUCCESS; handler scopes point at real steps; handler ids unique; CSS may only be the last strategy; no coordinate strategies; `default_on_unknown` is always hard_failure.
- Example recipe: `tests/fixtures/member.lookup_savings_balance@1.0.0.json` (hand-written against the real bank pages).

Step field notes:
- `source`: `agent` if the discovery agent performed the step, `human` if the operator did it during a takeover. Any `human` step means the recipe is saved with `needs_review: true`.
- `expect_page` (optional): checked before the step runs during replay, so a wrong page is caught early. Also used to pick the resume point after a human takeover (the latest step whose `expect_page` matches). The recorder fills it from the previous step's `wait_for`, or the page heading for step 1.

Recipe files are named `recipes/<recipe_id>@<version>.json`, e.g. `recipes/member.lookup_savings_balance@1.0.0.json`. Old versions are kept.

Locator rules:
- Order of preference: role and name, then label, then nearby or visible text, then CSS.
- A strategy counts only if it matches exactly one element.
- Using any strategy other than the first logs a drift warning.
- No coordinates.

### D2. Run result format

```json
{
  "status": "SUCCESS | BUSINESS_OUTCOME | FAILED | ESCALATED | ABORTED_BY_OPERATOR | REJECTED_BY_OPERATOR | NO_MATCHING_RECIPE | INVALID_INPUT | DRAFT_NOT_ALLOWED",
  "message": "<one readable line, e.g. why the input was invalid>",
  "run_id": "<id>",
  "mode": "<ask | replay | discover>",
  "recipe_id": "<recipe id>",
  "recipe_version": "<x.y.z>",
  "outputs": { "<output_name>": "<value>" },
  "outcome_code": "<e.g. MEMBER_NOT_FOUND>",
  "failure": {
    "step": 3,
    "expected": "<what should have happened>",
    "observed": "<what actually happened>",
    "error_type": "<hard_failure type>",
    "evidence": ["<screenshot path>", "<trace path>"]
  },
  "recoveries": ["<fixes applied>"],
  "llm_recovery_used": false,
  "human_interventions": ["<what the human did, masked>"],
  "approvals": ["<risky actions approved or rejected>"],
  "warnings": ["<e.g. backup locator used at step 3>"],
  "log_file": "<path>"
}
```

Consistency rules (enforced in `src/models/result.py`): SUCCESS has no failure or outcome_code; BUSINESS_OUTCOME needs outcome_code; FAILED and ESCALATED need failure; only SUCCESS returns outputs. Currency outputs are `Decimal`, never float.

Note: outputs are returned to the caller in the terminal. Sensitive outputs are masked in log files but shown to the caller in the result.

### D3. Policy (`config/policy.json`)

```json
{
  "allowed_domains": ["localhost:5050", "127.0.0.1:5050"],
  "allowed_paths": ["/login", "/search", "/members*", "/member/*"],
  "allowed_actions": ["navigate", "click", "type", "select", "extract", "wait"],
  "risky_button_names": ["Confirm", "Submit", "Create", "Update", "Save", "Transfer", "Close Account", "Delete"],
  "risky_requires": "human_approval",
  "mask_fields": ["member_id", "phone", "name", "balance", "password", "account_number", "address"]
}
```
The paths above are examples; match them to the real bank app routes.

### D4. Shared app error rules (`config/app_rules.json`)
Error rules for the bank app that the recorder adds to every recipe: member not found (business outcome), permission denied (business outcome), account already exists (business outcome), validation error (business outcome `VALIDATION_ERROR`), Notice popup (recoverable), slow page (recoverable), session expired (hard failure), server error (hard failure).

### D5. Settings (`config/settings.json`)
Step limit 20, wait timeout 10 seconds, recoverable retries 3, recovery max actions 3, LLM provider, model name, mock mode default, bank base URL `http://localhost:5050`.

### D6. Environment (`.env`, never committed; `.env.example` committed)
```
LLM_PROVIDER=anthropic
LLM_API_KEY=your_key_here
BANK_USERNAME=demo
BANK_PASSWORD=demo123
```

---

## PART E: SAFETY MODEL (summary)
1. One allowlist in `config/policy.json`: domains, paths, action types.
2. Enforced in code by the safety layer. The LLM proposes; the safety layer decides.
3. Checked for every action, every time, in every mode. Recipes are not trusted just because they were safe when saved.
4. Reading and navigating are free. Clicks that change data need human approval after the form is filled in.
5. Masking in all logs. Recipes store placeholders, never real values. Credentials only in `.env`.
6. Known limits (for REPORT): screenshots may contain private data (kept local, fake data); the risky button list must be maintained; text-based error detection depends on known messages.

---

## PART F: FAKE BANK WEBSITE SPEC (`bank_app/`)

### F1. Basics
- Flask, server-rendered HTML with Jinja templates, runs on `http://localhost:5050`.
- Legacy style: table layouts, nested tables, `<font>`-style plain markup, no `data-testid`, non-semantic input names like `f1`, `f2`. Labels should still be real text next to inputs so the accessibility tree has names (most, not all: one or two inputs may have weak labels to exercise backup locators).
- Fake logins (session cookie):
  - `demo` / `demo123`, role `teller`: account numbers and phones are masked on screen; restricted members are denied. This is the ONLY account the automation uses (`.env`).
  - `admin` / `admin123`, role `admin`: sees full account numbers and phones, and can open restricted members. For manual inspection only; never put in `.env` or used by automation (least privilege).
- Data in SQLite at `bank_app/data/bank.db` (git-ignored, generated). `bank_app/db.py` creates the tables and seeds them from `bank_app/data/members.json` (committed, readable seed) when the database file does not exist. Changes persist across restarts. No reset command: delete `bank.db` to start fresh.
- Tables: `users`, `members`, `accounts`, `transactions`, `confirmations`. Money is stored as integer cents. Writes that change data (open, update, close, transfer) run inside a single transaction.
- The automation never touches the database; it only drives the web pages.

### F2. Fake data
At least 20 members with 5-digit IDs, e.g. `12345`, `12346`, `12399`, `23456`, `34567`. Each has: name, address, phone, savings balance, checking balance, list of sub-accounts, and recent transactions. Several IDs share a prefix (e.g. `123xx`) so partial search returns a list.

### F3. Pages
1. Login.
2. Member search: one input (Member ID) and a Search button.
3. Search results list: shown for partial IDs; each row links to a member.
4. Member detail: name, member ID, address, masked phone, savings and checking balances, links to the actions below.
5. Transaction history.
6. Open sub-account: form (account type dropdown, initial deposit), then a confirmation page with a Confirm button, then a success page with a confirmation number.
7. Update member info: form (phone, address), then a confirmation page, then a success message.
8. Close account: confirmation page, then success (optional extra).
9. Transfer between accounts: form, confirmation page, then success (optional extra).

### F4. Search behavior
- Full 5-digit ID that exists: go straight to member detail.
- Partial ID: results list of matching members.
- Full ID that doesn't exist: "No member found."

### F5. Error triggers
Data errors, by member ID:
- `99999`: "No member found"
- `77777`: "You are not authorized to view this account"

System errors, by the `INJECT_ERROR` setting (environment variable or a small admin route like `/_admin/inject?error=popup`; injected errors fire on the next page load, then clear):
- `popup`: a "Notice" dialog (an HTML modal with role `dialog` and an OK button) must be closed.
- `slow_page`: the next page takes 12 seconds.
- `session_expired`: redirect to the login page with "Session expired".
- `server_error`: "Something went wrong" page.

Form errors:
- Negative or empty deposit, or invalid phone: validation message on the form.
- Opening a sub-account type the member already has: "This account already exists."

---

## PART G: THE THREE FLOWS TO AUTOMATE

| Flow | Inputs | Outputs | Changes data |
| --- | --- | --- | --- |
| Look up savings balance | `member_id` | `savings_balance` | No |
| Open sub-account | `member_id`, `account_type`, `initial_deposit` | `confirmation_number` | Yes, needs approval |
| Update phone number | `member_id`, `new_phone` | `confirmation_message` | Yes, needs approval |

---

## PART H: FOLDER STRUCTURE

```
bank_app/
  app.py
  templates/
  static/
  db.py
  data/members.json   (seed; bank.db is generated and git-ignored)
src/
  browser/       Playwright wrapper (the surface seam)
  agent/         discovery loop, LLM client, mock LLM
  recorder/      run to recipe
  catalog/       recipe loading, LLM matching
  replay/        replay engine, locator resolution, error handling
  recovery/      bounded LLM recovery (ask only)
  safety/        allowlist, risk approval, masking
  handoff/       session, control state, human takeover, action recording
  logs/          run folders, JSON logging, screenshots, traces
  models/        Pydantic models
config/
  policy.json
  app_rules.json
  settings.json
  mock_scripts/
recipes/         <recipe_id>@<version>.json, old versions kept
runs/            (git-ignored working runs)
evidence/        (committed: selected runs for submission)
tests/
docs/            (git-ignored: assignment document)
run.py
requirements.txt
README.md
REPORT.md
CLAUDE.md
PROJECT_CONTEXT.md
.env.example
.gitignore
```

`.gitignore` must include: `.venv/`, `.env`, `__pycache__/`, `*.pyc`, `.DS_Store`, `runs/`, `docs/`, `.pytest_cache/`.

---

## PART I: COMMANDS (target)

```
python bank_app/app.py                                                start the fake bank on :5050
python run.py list                                                    list recipes
python run.py discover "Look up member 12345 and read their savings balance" --mock
python run.py approve member.lookup_savings_balance
python run.py ask "What's the savings balance for member 12346?"
python run.py ask "What's the savings balance for member 99999?"       business outcome demo
python run.py replay "What's the savings balance for member 12346?"    strict mode
python run.py replay --recipe member.lookup_savings_balance --input member_id=12346   pure replay, no LLM (README demo)
python run.py replay --recipe member.lookup_savings_balance --input member_id=12346 --allow-draft   run a draft recipe
pytest
```

---

## PART J: BUILD ORDER (one step at a time; stop after each for testing)

0. Project setup: venv, `requirements.txt`, folders, `.gitignore`, `.env.example`, README and REPORT skeletons (REPORT with the 7 headings).
1. Fake bank website with all pages, search behavior, and error triggers. Test by hand in a browser.
2. Pydantic models: Recipe, RunResult, LogEntry, InterventionRequest, Policy. Validate the example recipe.
3. Browser layer: open the bank, print the accessibility tree, find and click by role and name.
4. Safety layer and logging: allowlist, risk check, masking, run folders.
5. Session and login, control state.
6. Discovery agent with the mock LLM; then the real LLM client (provider-agnostic interface).
7. Recorder: produce the first real recipe from a discovery run.
8. Replay engine: locators with fallback, waits, `only_if`, error handlers, success check, result.
9. Human takeover and risky action approval (resume, abort, action recording).
10. Catalog, `ask`, `replay`, `approve`, `list` commands.
11. Bounded LLM recovery.
12. Tests.
13. Evidence runs: discovery, replay success, replay not found, replay with injected error, a human takeover run, an approval run. Copy into `evidence/`.
14. README and REPORT.

---

## PART K: EVIDENCE TO PRODUCE

```
evidence/
  example_recipe.json                   lookup_savings_balance recipe
  discovery_run/                        log.jsonl, result.json, screenshots
  replay_success/
  replay_member_not_found/              business outcome
  replay_injected_popup/                recoverable, fixed automatically
  replay_injected_server_error/         hard failure with screenshot and trace
  human_takeover/                       intervention request, human actions, resume
  risky_action_approval/                approval or rejection logged
```

---

## PART L: README AND REPORT CONTENT

README.md:
- What the project is (2 to 3 lines).
- Setup on macOS: venv, `pip install -r requirements.txt`, `playwright install chromium`, copy `.env.example` to `.env`.
- Run without an API key: `--mock`.
- Demo path: start the bank, discover, approve, ask, replay (using `replay --recipe ... --input ...` as the deterministic proof), an error case.
- Where to find evidence.

REPORT.md (1 to 3 pages, these exact headings):
- `## Architecture`: components, the two-brain idea (LLM in discovery, recipe in replay), one program, key trade-offs.
- `## Artifact schema`: the recipe format, the contract part vs. the execution part, why each field exists, placeholders, versioning, approval.
- `## Determinism & error handling`: locator order and uniqueness, waits, checkpoints (`expect_page`, `wait_for`, success check), the three error types, the retry rule (never redo an action that may have changed data), default unknown is a hard failure, shared app rules, drift warnings, bounded recovery and its limits.
- `## Heterogeneity & multi-tenant`: the browser layer as the seam; desktop via OS accessibility APIs; screenshots and coordinates as the last-resort surface; base recipes per vendor product and version with per-bank overrides; drift detection.
- `## Escalation & handoff`: what counts as stuck, the intervention request, control states, same live session, action recording, resume and abort, the production design (operator queue, remote co-browsing).
- `## Safety`: allowlist, enforcement in code, risky action approval and why, masking, credentials, limits.
- `## Cuts`: what was left out and why (operator console, desktop surface, multi-tenant overrides, web API for the catalog, screenshot redaction, stability testing) and what to build next.

---

## PART M: HOW TO WORK

- Follow this file. Ask before changing a design decision.
- Build one step from Part J at a time. After each step: explain what was built and the key design choices, say how to run and test it, and wait for confirmation.
- After the user confirms a step, commit it with a clear message (e.g. "Step 1: fake bank website"). The user handles pushing.
- Simple, readable code with type hints and short comments explaining why.
- The replay engine must never import or call the LLM client.
- Only `src/browser/` imports Playwright.
- Every action goes through `src/safety/`.
- No secrets or real values in code, recipes, or logs.
- Prefer small functions and clear names over clever code.
