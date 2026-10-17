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
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import duckdb
import httpx

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_LOG = os.environ.get("GITHUB_ACTIONS") == "true"     # the coinboard repo is public, and so are its run logs


def say(msg: str) -> None:
    """Print a line that may describe the owner's holdings. In GitHub's logs (public) amounts and addresses are hidden;
    anywhere, characters that could pass for log commands or line breaks (token symbols are set by anyone) become '?'."""
    if PUBLIC_LOG:
        msg = re.sub(r"0x[0-9a-fA-F]{6,}", "(address)", msg)
        msg = re.sub(r"\d[\d,]*(?:\.\d+)?(?:e[-+]?\d+)?", "#", msg)
    print(re.sub(r"[^\x20-\x7e…→×—]", "?", msg).replace("::", ":"), flush=True)
CHAIN = {"robinhood": "robinhood", "arc": "arc"}


def best_pair(chain: str, token: str) -> str:
    """The token's deepest DexScreener pair (for its chart). Retries; "" if DexScreener does not answer, so a hiccup there
    costs one hour's chart for that coin instead of the whole build."""
    for attempt in range(4):
        try:
            r = httpx.get(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{token}", timeout=30).json()
            ps = [p for p in (r if isinstance(r, list) else []) if p["chainId"] == chain and p["baseToken"]["address"].lower() == token.lower() and p.get("liquidity")]
            if ps:
                return max(ps, key=lambda p: p["liquidity"]["usd"])["pairAddress"]
        except Exception:
            pass
        time.sleep(3 * (attempt + 1))
    say(f"warning: no DexScreener pair for {token} this hour")
    return ""


def series(ledger: Path, now: float, hours: int = 8 * 24) -> list[list[float]]:
    if not ledger.exists():
        return []
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


STATE = ROOT / "data/coinboard_replay_state.json"   # kept with the archives (the workflow's cache)
VALUES = ROOT / "data/coinboard_value_history.json"  # the portfolio's value, recorded by every build (private: not backed up)


def _ts(s: str | None) -> float | None:
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%MZ")) - time.timezone if s else None
    except Exception:
        return None


def value_history(coins: list[dict], cfg: dict, now: float) -> list[list[float]]:
    """Every build appends [time, value, cost] for the holdings it shows. The first build backfills hourly from the last
    trade in the positions file (holdings were unchanged since, so value = quantities x the hourly price history)."""
    def price_at(c: dict, t: float) -> float | None:
        p = None
        for ts, px in c.get("hist") or []:
            if ts <= t:
                p = px
            else:
                break
        return p
    try:
        hist = json.loads(VALUES.read_text()) if VALUES.exists() else []
    except Exception:
        hist = []
    if not hist:
        trades = [_ts(p.get("bought")) for p in cfg.get("positions", [])] + [_ts(x.get("sold_at")) for x in cfg.get("partial_sales", []) + cfg.get("closed", [])]
        trades = [t for t in trades if t]
        if trades:
            t = (int(max(trades)) // 3600 + 1) * 3600
            while t < now - 600:
                pxs = [price_at(c, t) for c in coins]
                if all(p is not None for p in pxs):
                    hist.append([t, round(sum(c["qty"] * p for c, p in zip(coins, pxs)), 2), round(sum(c["cost"] for c in coins), 2)])
                t += 3600
    pxs = [price_at(c, now) for c in coins]
    if coins and all(p is not None for p in pxs):
        point = [int(now), round(sum(c["qty"] * p for c, p in zip(coins, pxs)), 2), round(sum(c["cost"] for c in coins), 2)]
        if hist and now - hist[-1][0] < 1800:
            hist[-1] = point
        else:
            hist.append(point)
    try:
        VALUES.write_text(json.dumps(hist))
    except Exception as e:
        print(f"warning: could not save the value history ({e})")
    # all of it for the page's timeframes (all time, 30, 7, 1 days): the last 7 days hourly, older points thinned to about 300
    cut = now - 7 * 86400
    old, recent = [h for h in hist if h[0] < cut], [h for h in hist if h[0] >= cut]
    step = max(1, len(old) // 300)
    return [h[:2] for h in old[::step] + recent]


def _replay(cfg_file: Path, env: dict) -> dict | None:
    try:
        r = subprocess.run(["node", str(ROOT / "scripts/advance_baseline.js"), str(cfg_file)], capture_output=True, text=True,
                           timeout=900, env=env, check=True)
        return json.loads(r.stdout)
    except Exception as e:
        say(f"warning: wallet replay failed ({(getattr(e, 'stderr', '') or str(e)).strip()[:200]})")
        return None


def advance(cfg_file: Path, coins: list[dict], closed: list[dict], now: float, chain_qty: dict, pos_hash: str) -> tuple[list[dict], list[dict]]:
    """Replay the wallets (scripts/advance_baseline.js, the Scan button's code) and move the baseline to the current block.
    Starts from the point the last run reached (STATE, valid while the positions file is unchanged), else from the positions
    file's own baseline. The result must agree with the balances this build read itself, else the positions file is shown
    as it is. Returns the holdings to show: replayed quantities, costs and sold stats; coins bought since then are added
    (if DexScreener lists them and they are worth at least $0.50), coins sold out move to `closed`."""
    env = dict(os.environ)
    if env.get("HTTPS_PROXY"):                       # local runs behind the session proxy; CI has no proxy
        env.setdefault("NODE_USE_ENV_PROXY", "1")
        if Path("/root/.ccr/ca-bundle.crt").exists():
            env.setdefault("NODE_EXTRA_CA_CERTS", "/root/.ccr/ca-bundle.crt")
    from_positions = cfg_file.read_text()

    def agrees(res: dict) -> bool:
        rq = {(h["chain"], h["token"].lower()): h["qty"] for h in res["holdings"]}
        bad = [f"{c['name']} replay {rq.get(k, 0):,.4f} vs chain {q:,.4f}" for c in coins
               for k in [(c["chain"], c["token"].lower())] for q in [chain_qty.get(k)]
               if q is not None and abs(rq.get(k, 0) - q) > max(0.01, 0.005 * q)]
        if bad:
            say("warning: replay disagrees with on-chain balances (" + "; ".join(bad) + ")")
        held = {(c["chain"], c["token"].lower()) for c in coins}
        fixed, dust = [], []
        for h in res["holdings"] + res["closed"]:
            if h.get("corrected"):                   # the replay missed transfers of this token
                was = h["corrected"]
                msg = f"{h['chain']}: {h.get('symbol', '?')} chain {h.get('qty', 0):,.6g} vs replay {was.get('qty', 0):,.6g}"
                money = (h["chain"], h["token"].lower()) in held or was.get("paid", was.get("cost", 0) > 0) or h.get("soldQty", 0) > 0
                (fixed if money else dust).append(msg)
        if dust:                                     # tokens nobody paid for here (airdrops, transfers in): costed at market
            say("replay: corrected free tokens (" + "; ".join(dust) + ")")
        if fixed:                                    # a coin with money in it: do not trust the replay's costs
            say("warning: replay needed balance corrections (" + "; ".join(fixed) + ")")
        return not bad and not fixed

    res = None
    try:
        st = json.loads(STATE.read_text()) if STATE.exists() else None
    except Exception:
        st = None
    if st and st.get("positions") == pos_hash:
        cfg = json.loads(from_positions)
        for k, ch in st["chains"].items():
            if k in cfg["chains"] and ch["baseBlock"] >= cfg["chains"][k]["baseBlock"]:
                cfg["chains"][k]["baseBlock"], cfg["chains"][k]["baseline"] = ch["baseBlock"], ch["baseline"]
        cfg_file.write_text(json.dumps(cfg))
        res = _replay(cfg_file, env)
        if res is not None and not agrees(res):
            res = None
        if res is None:
            print("replay from the saved point failed; replaying from the positions file instead")
    if res is None:
        cfg_file.write_text(from_positions)
        res = _replay(cfg_file, env)
        if res is not None and not agrees(res):
            res = None
    if res is None:
        cfg_file.write_text(from_positions)
        print("warning: showing the positions file as it is")
        return coins, closed
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"positions": pos_hash, "chains": {k: {"baseBlock": ch["baseBlock"], "baseline": ch["baseline"]}
                                                                       for k, ch in json.loads(cfg_file.read_text())["chains"].items()}}))
    except Exception as e:
        print(f"warning: could not save the replay point ({e})")
    for n in res.get("notes", []):
        say("replay note: " + n)
    by = {(c["chain"], c["token"].lower()): c for c in coins}
    shown, gone = [], []
    for h in res["holdings"]:
        k = (h["chain"], h["token"].lower())
        c = by.get(k)
        if c is None:                                 # bought since the baseline
            pair = best_pair(h["chain"], h["token"])
            try:
                px = float(next(p for p in httpx.get(f"https://api.dexscreener.com/latest/dex/pairs/{h['chain']}/{pair}", timeout=30).json()["pairs"])["priceUsd"]) if pair else 0
            except Exception:
                px = 0
            if not pair or h["qty"] * px < 0.5:
                continue                              # unlisted or dust: hidden, as on the page
            led = ROOT / f"data/{'arc' if h['chain'] == 'arc' else 'tokens'}/{h['token'].lower()}/ledger.parquet"
            c = {"name": h["symbol"], "symbol": h["symbol"], "chain": h["chain"], "token": h["token"], "pair": pair,
                 "hist": series(led, now), "bought": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(now))}
            say(f"replay: new holding {h['symbol']} {h['qty']:,.4f}")
        c = {**c, "qty": h["qty"], "cost": round(h["cost"], 2), "soldQty": h["soldQty"], "soldProceeds": h["soldProceeds"],
             "realised": round(h["realised"], 2), "dec": h["dec"]}
        shown.append(c)
    order = {(c["chain"], c["token"].lower()): i for i, c in enumerate(coins)}
    shown.sort(key=lambda c: order.get((c["chain"], c["token"].lower()), len(order)))
    held = {(c["chain"], c["token"].lower()) for c in shown}
    for c in coins:
        if (c["chain"], c["token"].lower()) not in held:
            gone.append(c["name"])
    closed2 = [{"name": h["symbol"], "symbol": h["symbol"], "chain": h["chain"], "token": h["token"], "qty": 0, "cost": 0,
                "soldQty": h["soldQty"], "soldProceeds": h["soldProceeds"], "realised": round(h["realised"], 2), "dec": h["dec"]}
               for h in res["closed"] if h.get("soldQty", 0) > 0]
    if gone:
        say("replay: no longer held: " + ", ".join(gone))
    print(f"replay: baseline moved to " + ", ".join(f"{k} block {v:,}" for k, v in res["heads"].items()))
    return shown, closed2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "dashboard/site")
    ap.add_argument("--positions", default="positions.json")
    a = ap.parse_args()
    cfg = json.loads(Path(a.positions).read_text())
    now = time.time()
    wallets = {"arc": [cfg["wallet_arc"]], "robinhood": [cfg["wallet_robinhood"], *cfg.get("wallets_robinhood_extra", [])]}
    sold = {x["name"]: x for x in cfg.get("partial_sales", [])}
    coins, base, chain_qty = [], {k: [] for k in CHAINS}, {}
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
            say(f"{p['name']:7} on-chain check skipped (RPC busy)")
            continue
        chain_qty[(p["chain"], p["token"].lower())] = oc
        flag = "" if abs(oc - p["qty"]) <= max(0.01, p["qty"] * 0.002) else "   <-- MISMATCH, fix positions.json before deploying"
        say(f"{p['name']:7} positions {p['qty']:>16,.4f}  on-chain {oc:>16,.4f}{flag}")
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
    coins, closed = advance(out / "api/_config.json", coins, closed, now, chain_qty, hashlib.sha256(Path(a.positions).read_bytes()).hexdigest())
    vhist = value_history(coins, cfg, now)
    try:
        (ROOT / "data/coinboard_shown.json").write_text(json.dumps([{"name": c["name"], "chain": c["chain"], "token": c["token"], "dec": c.get("dec", 18)} for c in coins]))
    except Exception as e:
        print(f"warning: could not save the shown holdings ({e})")
    for f in ("scan.js", "refresh.js", "watch.js"):
        shutil.copy(ROOT / "dashboard/api" / f, out / "api" / f)
    (out / "vercel.json").write_text(json.dumps({
        "functions": {"api/scan.js": {"maxDuration": 60}, "api/refresh.js": {"maxDuration": 15}, "api/watch.js": {"maxDuration": 15}},
        "headers": [{"source": "/(.*)", "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}, {"key": "Cache-Control", "value": "no-store"}]}]}))
    # the functions' Node.js line, stated here rather than left to the project setting (which Vercel retires with the version)
    (out / "package.json").write_text(json.dumps({"private": True, "engines": {"node": "24.x"}}))
    an = out / "analytics.json"
    an_data = json.loads(an.read_text()) if an.exists() else {"coins": {}}
    logo = "data:image/png;base64," + base64.b64encode((ROOT / "dashboard/logo.png").read_bytes()).decode()
    html = ((ROOT / "dashboard/template.html").read_text().replace("__LOGO__", logo).replace("__DATA__", json.dumps({"built": int(now), "coins": coins, "closed": closed, "mkt": mkt, "vhist": vhist}))
            .replace("__ANALYTICS__", json.dumps(an_data)))
    (out / "index.html").write_text(html)
    print(f"wrote {out / 'index.html'} ({len(html) / 1024:.0f} KB), {len(coins)} coins, built {time.strftime('%H:%M UTC', time.gmtime(now))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
