# Evidence

Real runs of the system against the fake bank (`bank_app/`, fresh seed data, `http://localhost:5050`).
Discovery, request matching and recovery used a real LLM: `openai/gpt-oss-120b` on Groq.
The human takeover and the approvals were done by a person at the terminal, in the same browser window.

Each folder is one run, copied from `runs/<run_id>/`:

- `log.jsonl`: one JSON line per event (what happened, why, who was in control). Masked.
- `result.json`: the structured result (status, outputs, failure, how it was answered). Masked.
- On failure: a screenshot, the page's accessibility tree, and a Playwright trace (`trace.zip`, open with
  `npx playwright show-trace trace.zip`).

| Folder | Command | Result | What it shows |
| --- | --- | --- | --- |
| `example_recipe.json` | produced by `discovery_run`, then `approve` | approved recipe v1.0.0 | The artifact: contract (inputs, outputs, outcomes) and execution (steps, verified locators, checkpoints, error handlers). |
| `discovery_run/` | `discover "Look up member 12345 and read their savings balance"` | `SUCCESS` | The LLM drives the UI live (5 decisions): type, click, extract. The run becomes a draft recipe. |
| `replay_success/` | `replay --recipe member.lookup_savings_balance --input member_id=12346` | `SUCCESS` | Deterministic replay for another member. `llm_used_for: []`: no LLM at all. |
| `replay_member_not_found/` | `... --input member_id=99999` | `BUSINESS_OUTCOME` `MEMBER_NOT_FOUND` | A valid business answer, not a crash. |
| `replay_injected_popup/` | `... --input member_id=12345 --inject popup --at-step 3` | `SUCCESS` | Recoverable: the Notice dialog is dismissed by the recipe's error handler (`step 4: notice_popup`), the click is not repeated. |
| `replay_injected_server_error/` | `... --inject server_error --at-step 3` | `FAILED` at step 3, `server_error` | Hard failure with expected / observed and evidence: screenshot, accessibility tree, trace. No person was present, so it stops. |
| `ask_llm_recovery/` | `ask "How much does member 23456 have in savings?"` | `SUCCESS` | The LLM matched the request to the recipe. The recipe's Member ID locator was stale (`stale_recipe_used.json`: the app "renamed" the box), so bounded LLM recovery fixed that one step (1 action), replay verified it and continued. The result flags the recipe for review; the file was not changed. |
| `human_takeover/` | `replay ... --input member_id=12345 --inject server_error` (with a person at the keyboard) | `SUCCESS` | Hard failure -> control `PAUSED` -> intervention request (`intervention_1.json`, screenshot) -> `HUMAN`: the person clicked Search in the same window (recorded, masked) -> `resume` -> `AUTOMATION` -> replay continued at step 4, the step matching the page the person left. |
| `risky_action_approval/approved/` | `discover 'Open a Money Market account for member 23499 with an initial deposit of $50.00'` | `SUCCESS` | The form is filled, then the run pauses before Confirm; the person answered `yes`. The recorded recipe is `recipe_recorded_by_the_approved_run.json` (Confirm is marked `irreversible`). |
| `risky_action_approval/rejected/` | `replay --recipe member.open_money_market_account --allow-draft --input member_id=12347 ...` | `REJECTED_BY_OPERATOR` | The same recipe for another member; the person answered `no` at Confirm. Nothing was changed. |

## Notes

- Values are masked in logs and results: member IDs as `***45`, amounts, names, phones and addresses as `***`.
  The caller saw real values in the terminal; the files never contain them.
- Screenshots and the accessibility trees show the fake member's page as it was (names, balances). They are
  kept local in normal use and all data here is fake (see REPORT, Safety).
- `human_takeover/` has no `trace.zip`. That run was recorded before tracing was moved to start after
  login, and its trace contained the (fake) password, so it was not published. The log, result, request and
  screenshots show the whole takeover.
- In `risky_action_approval`, the model named the output `account_number`, although the value is a
  confirmation number. A reviewer would rename it before approving: that is why recipes start as drafts.
- Bugs these runs exposed, and fixed before these files were produced: goal values the model did not declare
  as inputs (stored as fixed values and logged unmasked), Groq rejecting tool calls server-side, LLM requests
  without a timeout, a login flake in the visible window, and traces recording the password.
