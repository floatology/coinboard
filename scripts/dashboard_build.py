#!/usr/bin/env python3
"""Build the private coin dashboard (one static page) from the owner's positions and the trade ledgers.

    dashboard_build.py [--out dashboard/site] --positions positions.json

The page shows, for each holding: name, live price, returns over 1h / 6h / 12h / 24h / 1 week, value, cost, profit and
loss, and the DexScreener chart in a 2x2 grid. Live prices come from DexScreener in the browser every 30 seconds; the
hourly price history for the last 8 days is baked in here (from the ledgers) so the period returns can be recomputed
against the live price. Run balances.py --refresh first for fresh ledgers, then deploy_dashboard.py.
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import time
from pathlib import Path

import duckdb
import httpx

ROOT = Path(__file__).resolve().parents[1]
CHAIN = {"robinhood": "robinhood", "arc": "arc"}


def best_pair(chain: str, token: str) -> str:
    r = httpx.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=30).json()["pairs"]
    ps = [p for p in r if p["chainId"] == chain and p["baseToken"]["address"].lower() == token.lower() and p.get("liquidity")]
    return max(ps, key=lambda p: p["liquidity"]["usd"])["pairAddress"]


def series(ledger: Path, now: float, hours: int = 8 * 24) -> list[list[float]]:
    con = duckdb.connect()
    rows = con.execute(f"select ts, price_usd from '{ledger}' where usd>=5 and ts>{now - hours * 3600 - 86400} order by block, log_index").fetchall()
    out, j, last = [], 0, None
    for h in range(hours, -1, -1):
        t = now - h * 3600
        while j < len(rows) and rows[j][0] <= t:
            last = rows[j][1]
            j += 1
        if last is not None:
            out.append([int(t), last])
    return out


CHAINS = {
    "arc": dict(rpc="https://rpc.mainnet.arc.io", stables=[{"address": "0x3600000000000000000000000000000000000000", "decimals": 6}], weth=None, maxSpan=9500),
    "robinhood": dict(rpc="https://rpc.mainnet.chain.robinhood.com", stables=[{"address": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", "decimals": 6}],
                      weth="0x0bd7d308f8e1639fab988df18a8011f41eacad73", maxSpan=500000),
}


def rpc(url: str, method: str, params: list):
    for attempt in range(4):
        try:
            return httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60).json()["result"]
        except Exception:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("rpc failed")


def onchain(url: str, token: str, wallets: list[str], dec: int) -> float:
    tot = 0
    for w in wallets:
        tot += int(rpc(url, "eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + w[2:].lower()}, "latest"]), 16)
    return tot / 10 ** dec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "dashboard/site")
    ap.add_argument("--positions", default="positions.json")
    a = ap.parse_args()
    cfg = json.loads(Path(a.positions).read_text())
    now = time.time()
    wallets = {"arc": [cfg["wallet_arc"]], "robinhood": [cfg["wallet_robinhood"], *cfg.get("wallets_robinhood_extra", [])]}
    sold = {x["name"]: x for x in cfg.get("partial_sales", [])}
    coins, base = [], {k: [] for k in CHAINS}
    for p in cfg["positions"]:
        dec = p.get("decimals", 18)
        s = sold.get(p["name"])
        sq, sp = (s["qty"], s["proceeds"]) if s else (0, 0)
        real = round(sp - s["cost"], 2) if s else 0
        coins.append({"name": p["name"], "symbol": p["name"], "chain": p["chain"], "token": p["token"], "qty": p["qty"], "cost": p["cost"],
                      "soldQty": sq, "soldProceeds": sp, "realised": real, "dec": dec,
                      "pair": best_pair(p["chain"], p["token"]), "hist": series(ROOT / p["ledger"], now), "bought": p.get("bought")})
        base[p["chain"]].append({"name": p["name"], "token": p["token"], "qty": p["qty"], "cost": p["cost"], "dec": dec,
                                 "soldQty": sq, "soldProceeds": sp, "realised": real})
        try:
            oc = onchain(CHAINS[p["chain"]]["rpc"], p["token"], wallets[p["chain"]], dec)
        except Exception:
            print(f"{p['name']:7} on-chain check skipped (RPC busy)")
            continue
        flag = "" if abs(oc - p["qty"]) <= max(0.01, p["qty"] * 0.002) else "   <-- MISMATCH, fix positions.json before deploying"
        print(f"{p['name']:7} positions {p['qty']:>16,.4f}  on-chain {oc:>16,.4f}{flag}")
    mkt = {}
    for cid in ("bitcoin", "ethereum"):
        for attempt in range(4):
            try:
                j = httpx.get(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart", params={"vs_currency": "usd", "days": 8}, timeout=30).json()
                mkt[cid] = [[int(x[0] / 1000), x[1]] for x in j["prices"]]
                break
            except Exception:
                time.sleep(8 * (attempt + 1))
        else:
            print(f"warning: no {cid} history from CoinGecko; the page will fetch it live")
    closed = []
    for c in cfg.get("closed", []):
        dec = 18
        x = {"name": c["name"], "symbol": c["name"], "chain": c["chain"], "token": c["token"], "qty": 0, "cost": 0, "soldQty": c["qty"],
             "soldProceeds": c["proceeds"], "realised": round(c["proceeds"] - c["cost"], 2), "dec": dec}
        closed.append(x)
        base[c["chain"]].append({"name": c["name"], "token": c["token"], "qty": 0, "cost": 0, "dec": dec, "soldQty": c["qty"],
                                 "soldProceeds": c["proceeds"], "realised": x["realised"]})
    api = {"chains": {}}
    for k, ch in CHAINS.items():
        api["chains"][k] = {**ch, "wallets": wallets[k], "baseBlock": cfg.get("base_blocks", {}).get(k) or int(rpc(ch["rpc"], "eth_blockNumber", []), 16), "baseline": base[k]}
    out = a.out
    (out / "api").mkdir(parents=True, exist_ok=True)
    (out / "api/_config.json").write_text(json.dumps(api))
    shutil.copy(ROOT / "dashboard/lwc.js", out / "lwc.js")
    for f in ("scan.js", "refresh.js"):
        shutil.copy(ROOT / "dashboard/api" / f, out / "api" / f)
    (out / "vercel.json").write_text(json.dumps({
        "functions": {"api/scan.js": {"maxDuration": 60}, "api/refresh.js": {"maxDuration": 15}},
        "headers": [{"source": "/(.*)", "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}, {"key": "Cache-Control", "value": "no-store"}]}]}))
    an = out / "analytics.json"
    an_data = json.loads(an.read_text()) if an.exists() else {"coins": {}}
    logo = "data:image/png;base64," + base64.b64encode((ROOT / "dashboard/logo.png").read_bytes()).decode()
    html = ((ROOT / "dashboard/template.html").read_text().replace("__LOGO__", logo).replace("__DATA__", json.dumps({"built": int(now), "coins": coins, "closed": closed, "mkt": mkt}))
            .replace("__ANALYTICS__", json.dumps(an_data)))
    (out / "index.html").write_text(html)
    print(f"wrote {out / 'index.html'} ({len(html) / 1024:.0f} KB), {len(coins)} coins, built {time.strftime('%H:%M UTC', time.gmtime(now))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
