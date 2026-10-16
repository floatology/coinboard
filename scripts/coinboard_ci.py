#!/usr/bin/env python3
"""Hourly job for the private Coin Board: refresh the archives, rebuild the analytics and the page, deploy.

    coinboard_ci.py --positions positions.json [--no-deploy]

Runs in GitHub Actions (see .github/workflows/coinboard.yml) and works the same on any machine with the .venv set up.
Needs VERCEL_TOKEN in the environment to deploy. positions.json holds the owner's costs and quantities: it comes from a
secret and is never committed.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(cmd: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=ROOT, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", default="positions.json")
    ap.add_argument("--no-deploy", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(Path(a.positions).read_text())
    t0 = time.time()
    procs = []
    for p in cfg["positions"]:
        if p["chain"] == "arc":
            cmd = f"{PY} scripts/arc_archive.py {p['token']} && {PY} scripts/arc_ledger.py {p['token']}"
        else:
            cmd = f"{PY} scripts/track_tokens.py build --only {p['name']}"
        procs.append((p["name"], subprocess.Popen(cmd, shell=True, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)))
    bad = []
    for name, pr in procs:
        err = pr.communicate()[1]
        print(f"refresh {name}: {'ok' if pr.returncode == 0 else 'FAILED'}", flush=True)
        if pr.returncode:
            bad.append(name)
            print((err or "")[-1500:], flush=True)
    if bad:
        print("archive refresh failed for " + ", ".join(bad) + "; not publishing stale numbers as fresh", file=sys.stderr)
        return 1
    print(f"archives refreshed in {time.time() - t0:.0f}s", flush=True)
    for cmd in ([PY, "scripts/dashboard_analytics.py"], [PY, "scripts/dashboard_build.py", "--positions", a.positions]):
        subprocess.run(cmd, cwd=ROOT, check=True)
    if not a.no_deploy:
        subprocess.run([PY, "scripts/dashboard_deploy.py"], cwd=ROOT, check=True)
    print(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
