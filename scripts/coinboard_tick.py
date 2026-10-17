#!/usr/bin/env python3
"""Coin Board's 5-minute tick (the "alerts" workflow; cron-job.org starts it, GitHub's own schedule is the fallback).

Starts the hourly update (coinboard.yml) when the last one began 55 or more minutes ago and none is running. A GitHub
hiccup never fails the run: a red run every 5 minutes would flood the owner's email.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=floatology/coinboard python scripts/coinboard_tick.py
    FORCE_UPDATE=true starts the update whatever its age.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime

EVERY = 55 * 60          # an hourly update, started on the first tick after 55 minutes
STUCK = 3 * 3600         # a run still queued or running after this long is ignored (the next one replaces it)


def gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "user-agent": "coinboard-tick",
                                          "accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return json.loads(data) if data else None


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def update_due(runs: list[dict], now: float) -> tuple[bool, str]:
    """runs: the update workflow's runs, newest first."""
    for r in runs:
        age = now - _ts(r.get("run_started_at") or r["created_at"])
        if r["status"] != "completed":
            if age < STUCK:
                return False, f"an update is {r['status'].replace('_', ' ')} (started {age / 60:.0f} min ago)"
            continue
        if r["conclusion"] in ("cancelled", "skipped"):
            continue
        return age >= EVERY, f"last update started {age / 60:.0f} min ago"
    return True, "no earlier update found"


def main() -> int:
    repo, force = os.environ["GITHUB_REPOSITORY"], os.environ.get("FORCE_UPDATE") == "true"
    try:
        runs = gh("GET", f"/repos/{repo}/actions/workflows/coinboard.yml/runs?per_page=10")["workflow_runs"]
        due, why = update_due(runs, time.time())
        if due or force:
            gh("POST", f"/repos/{repo}/actions/workflows/coinboard.yml/dispatches", {"ref": "main"})
            print(f"{why}: started the hourly update" + (" (forced)" if not due else ""))
            time.sleep(5)                        # let the new run show up before another tick looks
        else:
            print(f"{why}: nothing to start")
    except Exception as e:
        print(f"warning: could not check or start the hourly update ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
