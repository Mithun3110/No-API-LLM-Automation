# No-API LLM Automation

Banks run back-office apps with no API. This project lets an LLM work out how to do a task in such an app by
driving the real UI, records the successful run as a typed, versioned recipe, and then replays that
recipe deterministically, with no LLM deciding anything. When automation is stuck, a human takes over the
same live browser and hands it back. A fake legacy bank app (`bank_app/`) is the target.

Design and trade-offs: [REPORT.md](REPORT.md). Real runs: [evidence/](evidence/README.md).

## Setup (macOS, Python 3.11+)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

`.env` (never committed) holds:

| Key | Needed for | Default in `.env.example` |
| --- | --- | --- |
| `LLM_PROVIDER` | real LLM runs: `groq`, `anthropic` or `openai` | `groq` |
| `LLM_API_KEY` | real LLM runs only, not `--mock` | placeholder |
| `LLM_MODEL` | optional: overrides `llm_models` in `config/settings.json` | unset (`openai/gpt-oss-120b` on Groq) |
| `BANK_USERNAME`, `BANK_PASSWORD` | the automated login to the fake bank | `demo` / `demo123` |

Other settings (step limit, wait timeout, retries, pause per action) are in `config/settings.json`.
The allowlist and masking rules are in `config/policy.json`.

## Run without an API key

Add `--mock` to `ask`, `replay` (with a request) or `discover`. The LLM is replaced by scripted decisions from
`config/mock_scripts/` (savings balance lookup, open a sub-account, update a phone number), behind the same
interface as the real model. `replay --recipe` never needs a key: it uses no LLM at all.

## Demo path

Start the fake bank in one terminal (port 5050). It creates `bank_app/data/bank.db` from the seed data on
first start. Delete that file to start fresh.

```bash
python bank_app/app.py
```

In a second terminal, with the venv active:

1. See the recipe catalog:
   ```bash
   python run.py list
   ```
2. Run the agent on a goal and a target. The LLM drives the website live, then the run is saved as a
   draft recipe (drop `--mock` to use the real model). The target (app and start page) is optional:
   without it, `config/settings.json` decides (`http://localhost:5050`, starting at `/search`):
   ```bash
   python run.py discover "Look up member 12345 and read their savings balance" --mock
   ```
   ```bash
   python run.py discover "Look up member 12345 and read their savings balance" --target http://localhost:5050/search --mock
   ```
   A target outside the allowlist (`config/policy.json`) is refused before anything starts. The recipe
   records where it was recorded (`app.entry_url`), and replay always runs it against that app.
3. Review the draft and approve it:
   ```bash
   python run.py approve member.lookup_savings_balance
   ```
4. Replay the recipe for another member: deterministic, no LLM at all:
   ```bash
   python run.py replay --recipe member.lookup_savings_balance --input member_id=12346
   ```
5. Ask in plain English. A matching recipe is replayed; the LLM only picks the recipe and its inputs. With no
   matching recipe, `ask` runs discovery instead and saves a new draft:
   ```bash
   python run.py ask "What's the savings balance for member 23456?" --mock
   ```
6. Error cases:
   ```bash
   python run.py replay --recipe member.lookup_savings_balance --input member_id=99999
   ```
   ```bash
   python run.py replay --recipe member.lookup_savings_balance --input member_id=12345 --inject popup
   ```
   ```bash
   python run.py replay --recipe member.lookup_savings_balance --input member_id=12345 --inject server_error
   ```
   `99999` returns the business outcome `MEMBER_NOT_FOUND`. `--inject popup` shows a dialog that the recipe's
   error handler dismisses. `--inject server_error` is a hard failure: with you at the keyboard, the browser
   is handed to you (fix it in the window, e.g. Back then Search, then type `resume` in the terminal);
   with `--no-human` it stops with a screenshot, the accessibility tree and a trace.
7. A data-changing action asks for approval at the Confirm button (answer `yes` or `no`):
   ```bash
   python run.py discover 'Open a Money Market account for member 12399 with an initial deposit of $50.00' --mock
   ```

Every run prints how it was answered (`answered by: RECIPE` or `LLM DISCOVERY`, and what the LLM was used for)
and writes `runs/<run_id>/` with `log.jsonl` and `result.json` (masked).

| Command | What it does |
| --- | --- |
| `ask "<request>" [--target URL]` | Main command: a matching recipe is replayed; on an unexpected step failure, one bounded LLM recovery attempt, then a human. No match: LLM discovery, new draft. With `--target`, only recipes recorded for that app are considered. |
| `replay "<request>"` | Strict: recipes only, never falls back to discovery or LLM recovery. Drafts refused without `--allow-draft`. |
| `replay --recipe <id> --input k=v` | Strict, and no LLM anywhere, not even for matching. No `--target`: a recipe runs against the app it was recorded on. |
| `discover "<goal>" [--target URL]` | Discovery agent on a goal and target; a successful run becomes a draft recipe. |
| `approve <id>` | Shows a review summary and marks the latest version approved. |
| `list` | The catalog: versions, status, inputs and outputs. |

Useful flags: `--mock`, `--no-human` (no approvals or takeover: risky steps are rejected), `--fast` (no pause
before each action), `--slow`, `--auto-close`, `--inject <popup|slow_page|session_expired|server_error>`.

## How this was built

I used Claude Code (Anthropic) as a coding assistant, as encouraged by the brief. I defined the requirements
and made the key design decisions, including the mock banking environment, SQLite, retry behavior, human
takeover and resumption, approval of risky actions, and the `--target` option.

Claude Code generated most of the code within that design. I directed the work one step at a time, reviewing
and testing each step before committing it. I personally conducted the real-model runs, human takeover, and
approval scenarios documented in `evidence/`. These tests exposed issues, including recipes storing
transaction amounts as fixed values and traces capturing passwords, which were fixed and covered by new tests.

## Tests

```bash
pytest -m "not browser"
```
```bash
pytest
```

The first runs the fast tests (under a second). The second runs all of them (about 2.5 minutes), including
browser tests against a private copy of the bank on port 5051 with a throwaway database. Your bank and data are
never touched.

## Repository layout

| Path | Contents |
| --- | --- |
| `bank_app/` | The fake legacy bank (Flask, SQLite, seed data in `data/members.json`) |
| `src/browser/` | The only Playwright code: finding elements, actions, accessibility snapshots, traces |
| `src/handoff/` | The live session, the single action gate, control state, human takeover |
| `src/safety/` | Allowlist and risk checks, masking |
| `src/agent/` | Discovery agent, LLM clients (Groq, Anthropic, OpenAI), mock LLM |
| `src/recorder/` | Successful run to draft recipe |
| `src/replay/` | Deterministic replay engine (imports no LLM code) |
| `src/recovery/` | Bounded LLM recovery for one failed step (`ask` only) |
| `src/catalog/` | Recipe store, versions, approval, request matching |
| `src/models/` | Pydantic models: recipe, result, log entry, intervention request, policy, settings |
| `config/` | Policy, shared app error rules, settings, mock scripts |
| `recipes/` | Recipe files: `<recipe_id>@<version>.json` |
| `evidence/` | Selected real runs for review |
