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

## LLM API key (Groq recommended)

Real LLM runs (`discover`, `ask`, and `replay "<request>"` without `--mock`) need an API key. Groq is the
recommended and tested provider: the evidence was produced with `openai/gpt-oss-120b` on Groq. Without a key,
everything still runs with `--mock`.

1. Sign in at [console.groq.com](https://console.groq.com) and open **API Keys**
   ([console.groq.com/keys](https://console.groq.com/keys)).
2. Create a key and copy it straight away (you may not be able to see it again).
3. Put it in `.env` (created by `cp .env.example .env` above):
   ```
   LLM_PROVIDER=groq
   LLM_API_KEY=<your Groq key>
   ```
4. Check it with a real discovery run (the bank must be running, see How to run):
   ```bash
   python run.py discover "Look up member 12345 and read their savings balance"
   ```
   The banner should say `the LLM (openai/gpt-oss-120b) drives the website live`.

Groq limits tokens per minute (8,000 on the key used here). If a run hits the limit, it prints
`[llm] rate limit reached, waiting N s` and continues once the limit resets.

`.env` is git-ignored, so the key is never committed. Anthropic or OpenAI keys also work (`LLM_PROVIDER=anthropic`
or `openai`), but those two clients have not been tested.

## How to run

Start the fake bank in one terminal and leave it running (port 5050). It creates `bank_app/data/bank.db` from
the seed data on first start; delete that file to start fresh.

```bash
python bank_app/app.py
```

Run the commands below in a second terminal, with the venv active. The repo already contains an approved
recipe (`member.lookup_savings_balance`), so replay works straight away.

### With an API key (real LLM)

Set up the key first (see LLM API key above). Then run the commands as they are: discovery, request matching
and recovery use the real model. Requests can be phrased freely, e.g. "How much does member 23456 have in
savings?".

### Without an API key (`--mock`)

Add `--mock` to `discover`, `ask` and `replay "<request>"`. A scripted mock LLM replaces the model behind the
same interface; everything else (browser, safety, recording, replay, takeover, logs) runs exactly the same.
The mock knows three tasks and fixed phrasings:

- `Look up member <id> and read their savings balance` (and requests like `What's the savings balance for member <id>?`)
- `Open a <account type> account for member <id> with an initial deposit of $<amount>`
- `Update the phone number of member <id> to <555-555-0100>`

Commands that use no LLM at all (`replay --recipe`, `approve`, `list`) are the same in both modes. When a
step breaks during `ask`, the real model tries a bounded recovery; with `--mock`, recovery declines and the
step goes to a human.

### Demo path

Run the agent on a goal, approve the resulting recipe, then replay it deterministically. Drop `--mock` to use
the real model.

```bash
python run.py discover "Look up member 12345 and read their savings balance" --mock
```
```bash
python run.py approve member.lookup_savings_balance
```
```bash
python run.py replay --recipe member.lookup_savings_balance --input member_id=12346
```
```bash
python run.py replay --recipe member.lookup_savings_balance --input member_id=99999
```

The first command records a new draft version of the recipe, the second approves it, the third replays it
for another member with no LLM at all, and the last shows a business outcome (`MEMBER_NOT_FOUND`). The table
below covers every other feature.

### Which command for which feature

| Feature | With an API key | Without an API key |
| --- | --- | --- |
| See the recipe catalog | `python run.py list` | same |
| Discover a task (the LLM drives the UI; saves a draft recipe) | `python run.py discover "Look up member 12345 and read their savings balance"` | add `--mock` |
| Discover on a given app and start page | `python run.py discover "Look up member 12345 and read their savings balance" --target http://localhost:5050/search` | add `--mock` |
| Review and approve a recipe | `python run.py approve member.lookup_savings_balance` | same |
| Replay a recipe, no LLM at all | `python run.py replay --recipe member.lookup_savings_balance --input member_id=12346` | same |
| Ask in plain English (recipe if one fits, otherwise discovery) | `python run.py ask "How much does member 23456 have in savings?"` | `python run.py ask "What's the savings balance for member 23456?" --mock` |
| Strict replay from a request (never discovers) | `python run.py replay "What's the savings balance for member 12346?"` | add `--mock` |
| Business outcome: member not found | `python run.py replay --recipe member.lookup_savings_balance --input member_id=99999` | same |
| Recoverable: popup dismissed by the recipe | `python run.py replay --recipe member.lookup_savings_balance --input member_id=12345 --inject popup` | same |
| Recoverable: slow page, waited out | `python run.py replay --recipe member.lookup_savings_balance --input member_id=12345 --inject slow_page` | same |
| Hard failure, then human takeover (type `resume` or `abort`) | `python run.py replay --recipe member.lookup_savings_balance --input member_id=12345 --inject server_error` | same |
| Hard failure with evidence, no human | the same command plus `--no-human` | same |
| Risky action: approval at Confirm (`yes` or `no`) | `python run.py discover 'Open a Money Market account for member 12399 with an initial deposit of $50.00'` | add `--mock` |
| Another data-changing task | `python run.py discover "Update the phone number of member 12346 to 555-222-0199"` | add `--mock` |
| Run the tests | `pytest -m "not browser"` (fast) or `pytest` (all) | same: tests never call an LLM |

Notes:

- Use single quotes around goals that contain `$`, so the shell does not treat `$50` as a variable.
- In the takeover case, fix the page in the browser window (e.g. Back, then Search) and type `resume` in the
  terminal. Replay continues from the step that matches the page you left.
- A target outside the allowlist (`config/policy.json`) is refused before anything starts. A recipe records the
  app it was recorded on (`app.entry_url`), and replay always runs it against that app.
- Every run prints how it was answered (`answered by: RECIPE` or `LLM DISCOVERY`, and what the LLM was used
  for) and writes `runs/<run_id>/` with `log.jsonl` and `result.json`, both masked.
- Useful flags: `--no-human` (no approvals or takeover: risky steps are rejected), `--fast` (no pause before
  each action), `--slow`, `--auto-close`, `--allow-draft` (let `replay` run a draft recipe),
  `--inject <popup|slow_page|session_expired|server_error> --at-step N`.

### Commands

| Command | What it does |
| --- | --- |
| `ask "<request>" [--target URL]` | Main command: a matching recipe is replayed; on an unexpected step failure, one bounded LLM recovery attempt, then a human. No match: LLM discovery, new draft. With `--target`, only recipes recorded for that app are considered. |
| `replay "<request>"` | Strict: recipes only, never falls back to discovery or LLM recovery. Drafts refused without `--allow-draft`. |
| `replay --recipe <id> --input k=v` | Strict, and no LLM anywhere, not even for matching. |
| `discover "<goal>" [--target URL]` | Discovery agent on a goal and target; a successful run becomes a draft recipe. |
| `approve <id>` | Shows a review summary and marks the latest version approved. |
| `list` | The catalog: versions, status, inputs and outputs. |

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
