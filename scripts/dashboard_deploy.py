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
    # A token limited to one team cannot read /v2/user (404), so find the team that owns the project instead.
    cands = [os.environ.get("VERCEL_TEAM_ID")] if os.environ.get("VERCEL_TEAM_ID") else []
    u = c.get("/v2/user")
    if u.status_code == 200:
        cands.append(u.json()["user"].get("defaultTeamId"))
    t = c.get("/v2/teams")
    if t.status_code == 200:
        cands += [x["id"] for x in t.json().get("teams", [])]
    cands += [None]
    q, proj = None, None
    for team in dict.fromkeys(cands):
        qq = {"teamId": team} if team else {}
        r = c.get(f"/v9/projects/{a.project}", params=qq)
        if r.status_code == 200:
            q, proj = qq, r.json()
            break
    if proj is None:
        sys.exit(f"Vercel project '{a.project}' not found with this token (check VERCEL_PROJECT and that the token's scope is the team that owns it)")
    if (proj.get("ssoProtection") or {}).get("deploymentType") != "all":
        sys.exit("Protection is not 'all'; refusing to deploy")
    print("project Node.js:", proj.get("nodeVersion"), "(the site's package.json asks for", end=" ")
    try:
        import json
        print(json.loads((a.dir / "package.json").read_text())["engines"]["node"] + ")")
    except Exception:
        print("nothing)")
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
    if st.get("readyState") != "READY":   # a failed or stuck deployment must fail the run (the last good one stays live)
        sys.exit(f"deployment not ready: {st.get('readyState')} {st.get('errorMessage') or ''}".strip())
