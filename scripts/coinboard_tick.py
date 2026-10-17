#!/usr/bin/env python3
"""Coin Board's 5-minute tick (the "alerts" workflow; cron-job.org starts it, GitHub's own schedule is the fallback).

Starts the hourly update (coinboard.yml) when the last one began 55 or more minutes ago and none is running, and warns on
Telegram when the board has not updated for 2, 6 or 24 hours (runs started by the tick belong to GitHub's bot, so GitHub
emails nobody when they fail) or when cron-job.org has stopped starting the tick (seen by GitHub's fallback schedule).
A GitHub hiccup never fails the run: a red run every 5 minutes would flood the owner's email.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=floatology/coinboard python scripts/coinboard_tick.py
    FORCE_UPDATE=true starts the update whatever its age.
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

import telegram_notify

EVERY = 55 * 60          # an hourly update, started on the first tick after 55 minutes
STUCK = 3 * 3600         # a run still queued or running after this long is ignored (the next one replaces it)
STALE = (2 * 3600, 6 * 3600, 24 * 3600)   # warn as the last successful update passes each of these ages


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


def crossed(age: float | None, marks: tuple, window: float) -> float | None:
    """The mark that age passed less than `window` seconds ago: a check repeated every `window` seconds warns once per mark."""
    return next((m for m in marks if age is not None and m <= age < m + window), None)


def warnings(repo: str, now: float, event: str) -> list[str]:
    out = []
    ticks = gh("GET", f"/repos/{repo}/actions/workflows/alerts.yml/runs?event=workflow_dispatch&per_page=1")["workflow_runs"]
    tick_age = now - _ts(ticks[0]["created_at"]) if ticks else None
    cron_down = tick_age is None or tick_age > 25 * 60
    if event == "schedule" and crossed(tick_age, (30 * 60, 24 * 3600), 20 * 60):
        out.append("⚠️ <b>cron-job.org has stopped starting the 5-minute check</b>\n\nGitHub's backup schedule is keeping the "
                   "board going, but late and irregularly. Open console.cron-job.org and check the Coin Board job: it is "
                   "switched off after repeated failures (for example if its GitHub token stopped working).")
    # the 5-minute ticks warn (one each per mark); GitHub's ~20-minute fallback ticks only while cron-job.org is down
    if event != "schedule" or cron_down:
        ok = gh("GET", f"/repos/{repo}/actions/workflows/coinboard.yml/runs?status=success&per_page=1")["workflow_runs"]
        age = now - _ts(ok[0].get("run_started_at") or ok[0]["created_at"]) if ok else None
        mark = crossed(age, STALE, 5 * 60 if event != "schedule" else 20 * 60)
        if mark:
            last = gh("GET", f"/repos/{repo}/actions/workflows/coinboard.yml/runs?per_page=1")["workflow_runs"][0]
            why = ""
            if last["status"] == "completed" and last["conclusion"] != "success":
                jobs = gh("GET", f"/repos/{repo}/actions/runs/{last['id']}/jobs")["jobs"]
                step = next((st["name"] for j in jobs for st in j.get("steps") or [] if st.get("conclusion") == "failure"), None)
                why = "\nThe latest update failed" + (f" at <b>{html.escape(step)}</b>" if step else "") + "."
            out.append(f"⚠️ <b>Coin Board hasn't updated for {mark // 3600:.0f} hours</b>{why}\n\n"
                       f"<a href=\"{html.escape(last['html_url'])}\">Open the latest run</a>")
    return out


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
    try:
        for msg in warnings(repo, time.time(), os.environ.get("GITHUB_EVENT_NAME", "")):
            print("warning sent:", msg.split("\n")[0])
            if telegram_notify.configured():
                telegram_notify.send(msg)
    except Exception as e:
        print(f"warning: health check failed ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
