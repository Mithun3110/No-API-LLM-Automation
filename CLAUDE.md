# CLAUDE.md

Read docs/PROJECT_CONTEXT.md (local, git-ignored) before doing anything. It is the single source of truth for this project: the assignment, every design decision, the architecture, file formats, build order, and deliverables.

## Rules
- Follow docs/PROJECT_CONTEXT.md. Ask before changing any design decision.
- Build one step from Part J at a time. After each step, explain what you built and why, tell me how to run and test it, and wait for me.
- Python, Flask (bank_app on port 5050), Playwright (Chromium), Pydantic, pytest.
- Only src/browser/ imports Playwright.
- The replay engine must never import or call the LLM.
- Every browser action goes through src/safety/ before it runs.
- Never put API keys, credentials, or real values in code, recipes, or logs. Use .env.
- Mask sensitive data in all logs.
- Keep code simple and readable, with type hints and short comments explaining why.
- I must be able to explain every line in an interview, so prefer clarity over cleverness.
