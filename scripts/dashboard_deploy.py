#!/usr/bin/env python3
"""Deploy dashboard/site to the private Vercel project (keeps its own vercel.json: function limits and headers).

    VERCEL_TOKEN=... dashboard_deploy.py [--project NAME]  (default: $VERCEL_PROJECT)

Refuses to deploy unless Vercel Authentication covers every URL (ssoProtection deploymentType "all").
"""
import argparse, hashlib, os, sys, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--project", default=os.environ.get("VERCEL_PROJECT") or "coinboard")
ap.add_argument("--dir", type=Path, default=ROOT / "dashboard/site")
a = ap.parse_args()
tok = os.environ.get("VERCEL_TOKEN") or sys.exit("Set VERCEL_TOKEN")
with httpx.Client(base_url="https://api.vercel.com", headers={"Authorization": f"Bearer {tok}"}, timeout=90) as c:
    team = c.get("/v2/user").raise_for_status().json()["user"].get("defaultTeamId")
    q = {"teamId": team} if team else {}
    proj = c.get(f"/v9/projects/{a.project}", params=q).raise_for_status().json()
    if (proj.get("ssoProtection") or {}).get("deploymentType") != "all":
        sys.exit("Protection is not 'all'; refusing to deploy")
    blobs = {f.relative_to(a.dir).as_posix(): f.read_bytes() for f in sorted(a.dir.rglob("*")) if f.is_file()}
    files = [{"file": n, "sha": hashlib.sha1(d).hexdigest(), "size": len(d)} for n, d in blobs.items()]
    by = {f["sha"]: blobs[f["file"]] for f in files}
    body = {"name": a.project, "files": files, "target": "production", "projectSettings": {"framework": None, "buildCommand": None, "outputDirectory": None}}
    for _ in range(3):
        r = c.post("/v13/deployments", params=q, json=body)
        err = r.json().get("error", {}) if r.status_code >= 400 else {}
        if err.get("code") != "missing_files":
            break
        for sha in err.get("missing", []):
            c.post("/v2/files", params=q, content=by[sha], headers={"x-vercel-digest": sha, "Content-Type": "application/octet-stream"}).raise_for_status()
    r.raise_for_status()
    did = r.json()["id"]
    for _ in range(60):
        st = c.get(f"/v13/deployments/{did}", params=q).json()
        if st.get("readyState") in ("READY", "ERROR", "CANCELED"):
            break
        time.sleep(3)
    print(st.get("readyState"), "https://" + st["url"], [x for x in st.get("alias") or []])
