# No-API LLM Automation

An LLM discovers how to complete a goal in a legacy bank UI that has no API. The successful run is saved as a typed, versioned recipe, which then replays deterministically with no LLM in the loop. A human can take over the same live browser session when automation gets stuck.

## Setup (macOS)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

## Run without an API key

_TODO (step 14): `--mock` mode._

## Demo path

_TODO (step 14): start the bank, discover, approve, ask, replay with `--recipe`, an error case._

## Evidence

_TODO (step 14): what is in `evidence/`._
