# coinboard

Hourly refresh job and Telegram alerts for a private coin dashboard.

- `.github/workflows/coinboard.yml` (hourly, started by the 5-minute check below or the page's "Refresh analytics" button):
  updates trade archives for a few tokens, rebuilds holder and volume analytics, takes live swap quotes for exit liquidity,
  builds the dashboard page and deploys it to a login-protected Vercel project, then runs the hourly alert checks (whales,
  launch wallets, exit liquidity).
- `.github/workflows/alerts.yml` (every 5 minutes, started by cron-job.org; GitHub's own schedule is the fallback): starts
  the hourly job when due, warns on Telegram if the board stops updating, and checks price moves, liquidity pulls, the
  price alerts set on the page, the daily summary and the weekly alert report. On request it sends one test message of
  every alert.
- `.github/workflows/telegram-test.yml`: sends a sample message to check the bot.

Thresholds and the rules for each alert are at the top of `scripts/coinboard_alerts.py`.

Nothing private is stored here. Positions, costs and wallet addresses come from repository secrets (`POSITIONS_JSON`) at run time and are
never committed; the trade archives are cached by the workflow and kept out of git (`data/` is ignored). The public run logs hide amounts,
addresses, watchlist coins and price-alert levels.

Secrets: `POSITIONS_JSON`, `VERCEL_TOKEN`, `VERCEL_PROJECT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `KV_REST_API_URL` and
`KV_REST_API_READ_ONLY_TOKEN` (the page's Upstash database, read-only: watchlist and price alerts), and optionally `SEED_REPO` +
`SEED_TOKEN`, a last-resort source of starting archives. The archives are kept in the Actions cache and backed up daily to the
`archive-backup` release.
Local run: `python -m venv .venv && .venv/bin/pip install -r requirements.txt`, then
`.venv/bin/python scripts/coinboard_ci.py --positions positions.json --no-deploy`.
