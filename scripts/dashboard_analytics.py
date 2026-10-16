#!/usr/bin/env python3
"""Per-coin analytics series for the dashboard's Analytics tab (daily, from the local archives).

    dashboard_analytics.py [--out dashboard/site/analytics.json] [--days 45]

For each holding: daily price close, buy and sell volume, trades, holders, and the share of supply held by whale / medium /
small / new wallets over time (pools, curves, protocol contracts and burn addresses excluded), plus summary numbers and a
short templated read. Needs fresh archives (balances.py --refresh).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
PM_RH = "0x8366a39cc670b4001a1121b8f6a443a643e40951"      # Uniswap v4 PoolManager (same address on both chains)
NET_PROTOCOL = {"0xb078cc304a0b264c5f3680dc0488954accd02e87", "0x04822ea321a0dee6f40656172f29312104855d66", "0x3bb7a23316f82c0e984fa2e784846d8928a35f42",
                "0x575b7b7c97ef3e21c82daeb427899d583e1e913f", "0xff32a969a0c567129eecd926d04657728e1980c1", "0x92166e94eea5b7799b761653881692f881dfc4c9",
                "0x346e1a31171a0f7ac73909010b5435768d3b5462", "0x79e71f8a8a2912e40687a8820b2dc0fdd2f686b3", "0x086c58400b8708ef993f256e12e752dcf0ac918e",
                "0x59f95461e68e0c77605299791e1449f175165b54", "0x99e70a5b06" }
# per coin: archive dir, decimals, supply, bucket thresholds (% of supply: whale, medium), extra excluded addresses
COINS = {
    "WALLET": dict(dir="data/tokens/0x0339f5459fc690ac85f1782e15782a151b4a9e1b", dec=18, supply=1e9, th=(0.25, 0.02), excl={PM_RH, "0x9501a20bedb8bea0798fe5d4c411f5e270965d49"}),
    "FAZE": dict(dir="data/arc/0x394d38f807ee0027a182216f5e67a15ae441fa2e", dec=18, supply=1e9, th=(0.25, 0.02), excl={PM_RH, "0x6a62919ccbf0c19e0c4e084f986b582b4492dda4"}),
    "AF": dict(dir="data/arc/0x75d658f8101fbe6dc217fbba7e20a0312af5fa2e", dec=18, supply=1e9, th=(0.25, 0.02), excl={PM_RH, "0x6a62919ccbf0c19e0c4e084f986b582b4492dda4"}),
    "NET": dict(dir="data/tokens/0xca9c78dd337a67f6e0077f65f5e9218719d30edf", dec=9, supply=154828.4, th=(0.05, 0.01), excl=NET_PROTOCOL | {PM_RH}),
}


def analyse(name: str, cfg: dict, days: int) -> dict:
    d = ROOT / cfg["dir"]
    con = duckdb.connect()
    led = con.execute(f"select ts, block, side, usd, price_usd from '{d}/ledger.parquet' order by block, log_index").fetchall()
    lt = np.array([r[0] for r in led], dtype=float); lb = np.array([r[1] for r in led], dtype=float)
    now = float(lt.max())
    # all-time high: the highest hourly closing price over the coin's whole history (trades of $5+, so one-off wicks don't count)
    hourly = {}
    for ts, b, side, usd, px in led:
        if usd >= 5:
            hourly[int(ts // 3600)] = px
    ath_h, ath = max(hourly.items(), key=lambda kv: kv[1]) if hourly else (0, 0)
    day0 = int(now // 86400) - days + 1
    # pair/pool addresses from the archive metadata (v3 pools, DexScreener pairs)
    excl = set(cfg["excl"]) | {ZERO, DEAD}
    meta_p = d / "meta.json"
    if meta_p.exists():
        m = json.loads(meta_p.read_text())
        excl |= {a.lower() for a in m.get("pair_addrs", [])} | {a.lower() for a in m.get("v3_pools", {})}
    # daily trade stats
    daily = {}
    for ts, b, side, usd, px in led:
        k = int(ts // 86400)
        if k < day0:
            continue
        x = daily.setdefault(k, {"buy": 0.0, "sell": 0.0, "n": 0, "close": None})
        x["buy" if side == "buy" else "sell"] += usd; x["n"] += 1
        if usd >= 5:
            x["close"] = px
    # balances over time
    tr = con.execute(f"select block, src, dst, cast(value as hugeint)/1e{cfg['dec']} v from '{d}/transfers.parquet' order by block, log_index").fetchall()
    bal, first = {}, {}
    snaps = []
    ds = list(range(day0, int(now // 86400) + 1))
    nxt = 0
    sup = cfg["supply"]
    wh, md = cfg["th"][0] / 100 * sup, cfg["th"][1] / 100 * sup
    holder_floor = 1e-6 * sup

    def snapshot(day_end_ts: float):
        tot = {"whale": 0.0, "medium": 0.0, "small": 0.0, "new": 0.0}; cnt = {"whale": 0, "medium": 0, "small": 0}; holders = 0; top = []
        for a, v in bal.items():
            if a in excl or v <= 1e-9:
                continue
            if v >= holder_floor:
                holders += 1
            fa = first.get(a, 0)
            if day_end_ts - fa < 7 * 86400:
                tot["new"] += v          # overlaps the size buckets: wallets first seen in the last 7 days
            if v >= wh:
                tot["whale"] += v; cnt["whale"] += 1
            elif v >= md:
                tot["medium"] += v; cnt["medium"] += 1
            else:
                tot["small"] += v; cnt["small"] += 1
            top.append(v)
        top.sort(reverse=True)
        return {k: v / sup * 100 for k, v in tot.items()}, holders, sum(top[:10]) / sup * 100, cnt

    blocks_ts = np.interp([r[0] for r in tr], lb, lt)
    supply, sup_ath = 0.0, None
    for (b, s, t, v), ts in zip(tr, blocks_ts):
        while nxt < len(ds) and ts >= (ds[nxt] + 1) * 86400:
            snaps.append((ds[nxt],) + snapshot((ds[nxt] + 1) * 86400)); nxt += 1
        v = float(v)
        if sup_ath is None and ts >= (ath_h + 1) * 3600:
            sup_ath = supply
        if s == ZERO or s == DEAD:
            supply += v
        if t == ZERO or t == DEAD:
            supply -= v
        bal[s] = bal.get(s, 0.0) - v; bal[t] = bal.get(t, 0.0) + v
        if t not in first:
            first[t] = ts
    while nxt < len(ds):
        snaps.append((ds[nxt],) + snapshot(now if ds[nxt] == int(now // 86400) else (ds[nxt] + 1) * 86400)); nxt += 1
    rows = []
    last_close = None
    snapmap = {k: (sh, h, t10, c) for k, sh, h, t10, c in snaps}
    for k in ds:
        x = daily.get(k, {"buy": 0.0, "sell": 0.0, "n": 0, "close": None})
        if x["close"] is not None:
            last_close = x["close"]
        sh, h, t10, c = snapmap.get(k, ({"whale": 0, "medium": 0, "small": 0, "new": 0}, 0, 0, {"whale": 0, "medium": 0, "small": 0}))
        rows.append({"d": k * 86400, "close": last_close, "buy": round(x["buy"]), "sell": round(x["sell"]), "n": x["n"], "holders": h, "top10": round(t10, 2),
                     "nw": c["whale"], "whale": round(sh["whale"], 3), "medium": round(sh["medium"], 3), "small": round(sh["small"], 3), "new": round(sh["new"], 3)})
    # summary + templated read
    r = rows
    def pick(i):
        return r[max(0, len(r) + i)]
    w7 = r[-1]["whale"] - pick(-8)["whale"]; wcount = r[-1]["nw"]; n7 = r[-1]["new"] - pick(-8)["new"]; m7 = r[-1]["medium"] - pick(-8)["medium"]
    vol7 = sum(x["buy"] + x["sell"] for x in r[-7:]); volp = sum(x["buy"] + x["sell"] for x in r[-14:-7]) or 1
    bs7 = sum(x["buy"] for x in r[-7:]) / max(1, sum(x["sell"] for x in r[-7:]))
    px_now = next((x["close"] for x in reversed(r) if x["close"]), 0)
    read = []
    read.append(f"{wcount} whale wallets hold {r[-1]['whale']:.1f}% of supply ({w7:+.2f}% in 7 days); wallets under 7 days old hold {r[-1]['new']:.1f}% ({n7:+.2f}%).")
    read.append(f"Volume is {'up' if vol7 > volp else 'down'} {abs(vol7 / volp - 1) * 100:.0f}% on the prior week; buys are {bs7:.2f}x sells.")
    read.append(f"Holders {r[-1]['holders']:,} ({r[-1]['holders'] - pick(-8)['holders']:+,} in 7 days); top 10 hold {r[-1]['top10']:.1f}% of supply.")
    return {"rows": r, "ath": ath, "athT": ath_h * 3600, "supply": round(supply, 4), "athMcap": round(ath * (sup_ath if sup_ath is not None else supply)), "px": px_now, "w7": round(w7, 3), "n7": round(n7, 3), "m7": round(m7, 3), "vol7": round(vol7), "volPrev": round(volp),
            "read": read, "note": "Unstaked NET only; 91% of NET is staked and not shown." if name == "NET" else ""}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "dashboard/site/analytics.json")
    ap.add_argument("--days", type=int, default=45)
    a = ap.parse_args()
    out = {"built": int(time.time()), "coins": {}}
    for n, c in COINS.items():
        t = time.time()
        out["coins"][n] = analyse(n, c, a.days)
        print(n, f"{time.time() - t:.0f}s", out["coins"][n]["read"][0])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, separators=(",", ":")))
    print("wrote", a.out, a.out.stat().st_size // 1024, "KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
