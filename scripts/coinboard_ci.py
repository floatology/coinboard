#!/usr/bin/env python3
"""Hourly job for the private Coin Board: refresh the archives, rebuild the analytics and the page, deploy.

    coinboard_ci.py --positions positions.json [--no-deploy]

Runs in GitHub Actions (see .github/workflows/coinboard.yml) and works the same on any machine with the .venv set up.
Needs VERCEL_TOKEN in the environment to deploy. positions.json holds the owner's costs and quantities: it comes from a
secret and is never committed.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(ROOT / "scripts"))
from dashboard_build import say   # noqa: E402  (log lines that may describe holdings: hidden amounts in GitHub's public logs)

COINS_FILE = ROOT / "data/coinboard_coins.json"
TOKEN = re.compile(r"0x[0-9a-f]{40}")
SHOWN = ROOT / "data/coinboard_shown.json"    # the holdings the last build showed (listed, worth $0.50+)
NEW_COIN_BUDGET = 25 * 60      # seconds the archive refresh may take in one run


def held_coins(cfg: dict) -> list[dict]:
    """Coins to keep archives and analytics for: the positions file's, plus any coin the last build showed (bought since;
    it gets its archive and analytics from the next run on). Unlisted airdrops never reach the page, so never get here."""
    out, seen = [], set()
    for p in cfg["positions"]:
        out.append({"name": p["name"], "chain": p["chain"], "token": p["token"], "dec": p.get("decimals", 18)})
        seen.add((p["chain"], p["token"].lower()))
    try:
        shown = json.loads(SHOWN.read_text()) if SHOWN.exists() else []
    except Exception:
        shown = []
    for c in shown:
        if (c["chain"], c["token"].lower()) not in seen and c["chain"] in ("arc", "robinhood"):
            out.append({"name": c["name"], "chain": c["chain"], "token": c["token"], "dec": c.get("dec", 18)})
            seen.add((c["chain"], c["token"].lower()))
            say(f"also tracking {c['name']} ({c['chain']}), held since the positions file")
    return out


def registered_symbol(token: str) -> str | None:
    try:
        e = json.loads((ROOT / "data/tokens/tokens.json").read_text()).get(token.lower())
        return e["symbol"] if e else None
    except Exception:
        return None


def check_site(site: Path) -> list[str]:
    """Basic sanity checks on the built page before it goes live."""
    problems = []
    try:
        html = (site / "index.html").read_text()
        data = json.loads(re.search(r"const DATA = (\{.*?\});\n", html).group(1))
        coins = data["coins"]
        if not coins:
            problems.append("no holdings on the page")
        for c in coins:
            if not (c.get("qty", 0) > 0 and c.get("cost", -1) >= 0):
                problems.append(f"{c.get('name')} has a missing or negative quantity or cost")
        if "__DATA__" in html or "__LOGO__" in html:
            problems.append("template placeholders left in the page")
    except Exception as e:
        problems.append(f"page unreadable ({e})")
    try:
        cfg = json.loads((site / "api/_config.json").read_text())
        if not all(ch.get("baseBlock", 0) > 0 for ch in cfg["chains"].values()):
            problems.append("scan config has no base block")
    except Exception as e:
        problems.append(f"scan config unreadable ({e})")
    try:
        json.loads((site / "analytics.json").read_text())["coins"]
    except Exception as e:
        problems.append(f"analytics unreadable ({e})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", default="positions.json")
    ap.add_argument("--no-deploy", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(Path(a.positions).read_text())
    t0 = time.time()
    coins = [c for c in held_coins(cfg) if TOKEN.fullmatch(c["token"].lower())]

    def refresh(c: dict) -> tuple[str, str]:
        """One coin's archive refresh: its commands in order, as argument lists (never through a shell: token symbols
        are chosen by whoever made the token). Coins are picked by address, never by symbol."""
        tok = c["token"].lower()
        if c["chain"] == "arc":
            steps = [[PY, "scripts/arc_archive.py", tok], [PY, "scripts/arc_ledger.py", tok]]
        else:                                       # bought since the positions file: register it first
            steps = ([[PY, "scripts/track_tokens.py", "add", tok]] if registered_symbol(tok) is None else []) + \
                    [[PY, "scripts/track_tokens.py", "build", "--only", tok]]
        for cmd in steps:
            try:   # a new coin's first archive can take longer than an hour: it resumes on the next run
                r = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                   timeout=max(60, NEW_COIN_BUDGET - (time.time() - t0)))
            except subprocess.TimeoutExpired:
                return "not finished this run (will resume next run)", ""
            if r.returncode:
                return "FAILED", r.stderr
        return "ok", ""

    bad = []
    with ThreadPoolExecutor(max_workers=max(1, len(coins))) as ex:
        for c, (status, err) in zip(coins, ex.map(refresh, coins)):
            say(f"refresh {c['name']}: {status}")
            if status == "FAILED":
                bad.append(c["name"])
                print(re.sub(r"[^\x20-\x7e\n]", "?", err[-1500:]).replace("::", ":"), flush=True)
    if bad:   # prices, holdings and charts are still current; only that coin's analytics stay at the last good refresh
        say("warning: archive refresh failed for " + ", ".join(bad) + "; its analytics stay at the last good refresh")
    COINS_FILE.write_text(json.dumps([{k: c[k] for k in ("name", "chain", "token", "dec")} for c in coins]))
    print(f"archives refreshed in {time.time() - t0:.0f}s", flush=True)
    for cmd in ([PY, "scripts/dashboard_analytics.py", "--coins", str(COINS_FILE)], [PY, "scripts/dashboard_build.py", "--positions", a.positions]):
        subprocess.run(cmd, cwd=ROOT, check=True)
    problems = check_site(ROOT / "dashboard/site")
    if problems:   # keep the last good page live rather than publish a broken one
        print("not deploying: " + "; ".join(problems), file=sys.stderr)
        return 1
    if not a.no_deploy:
        subprocess.run([PY, "scripts/dashboard_deploy.py"], cwd=ROOT, check=True)
    # whale alerts, from the archives just refreshed (after the deploy, and never able to fail the run)
    subprocess.run([PY, "scripts/coinboard_alerts.py", "whales"], cwd=ROOT)
    print(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
