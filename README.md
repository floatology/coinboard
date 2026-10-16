# coinboard

Hourly refresh job for a private coin dashboard. A GitHub Action (`.github/workflows/coinboard.yml`) updates trade archives for a few
tokens, rebuilds holder and volume analytics, builds the dashboard page and deploys it to a login-protected Vercel project.
A "Refresh analytics" button on the page can start the job on demand.

Nothing private is stored here. Positions, costs and wallet addresses come from repository secrets (`POSITIONS_JSON`) at run time and are
never committed; the trade archives are cached by the workflow and kept out of git (`data/` is ignored).

Secrets: `POSITIONS_JSON`, `VERCEL_TOKEN`, `VERCEL_PROJECT`, and for the first run `SEED_REPO` + `SEED_TOKEN`.
Local run: `python -m venv .venv && .venv/bin/pip install httpx pydantic duckdb pyarrow python-dotenv numpy`, then
`.venv/bin/python scripts/coinboard_ci.py --positions positions.json --no-deploy`.
