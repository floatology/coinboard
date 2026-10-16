#!/usr/bin/env python3
"""Turn an Arc token's raw logs into a trade ledger.

    arc_ledger.py 0xTOKEN

Reads data/arc/<token>/{transfers,swaps}.parquet (from arc_archive.py) and writes
    ledger.parquet   one row per swap: ts, block, log_index, tx_hash, pool, trader,
                     side, tokens, usd, price_usd, plus the trader's running
                     position, average cost and realised profit from trades
    blocks.json      block -> unix time anchors used for timestamps

Rules (they come from what went wrong on Robinhood Chain):
- Uniswap v4 Swap amounts are the swapper's delta: a positive token amount means
  the swapper RECEIVED tokens, i.e. a buy. Quote value comes from the USDC leg (the native token, 18 decimals).
- The swap's "sender" is a router, so the trader is found from the token's own
  Transfer logs in the same transaction: the wallet whose balance actually moved
  by the swap amount, ignoring the PoolManager and anything that nets to zero.
- Only USDC-quoted pools are priced; others (EURC) are counted but not valued.
- Block times are interpolated between anchors taken from the chain every 20,000
  blocks (Arc blocks run about 0.5 s).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arc_archive import rpc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ANCHOR_EVERY = 20_000
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"


def anchors(d: Path, lo: int, hi: int) -> list[list[int]]:
    p = d / "blocks.json"
    have = json.loads(p.read_text()) if p.exists() else []
    seen = {b for b, _ in have}
    for b in list(range(lo, hi, ANCHOR_EVERY)) + [hi]:
        if b not in seen:
            blk = rpc("eth_getBlockByNumber", [hex(b), False])
            have.append([b, int(blk["timestamp"], 16)])
            seen.add(b)
    have.sort()
    p.write_text(json.dumps(have))
    return have


def stamp(a: list[list[int]]):
    import bisect
    bs = [x[0] for x in a]

    def f(b: int) -> int:
        i = max(1, min(bisect.bisect_left(bs, b), len(a) - 1))
        (b0, t0), (b1, t1) = a[i - 1], a[i]
        return int(t0 + (b - b0) * (t1 - t0) / max(b1 - b0, 1))
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("token")
    ap.add_argument("--out", type=Path, default=ROOT / "data/arc")
    args = ap.parse_args()
    token = args.token.lower()
    d = args.out / token
    meta = json.loads((d / "meta.json").read_text())
    pm = meta.get("pool_manager", "").lower()
    pools = meta["pools"]
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    tr = con.execute(f"select block, log_index, tx_hash, src, dst, cast(value as hugeint)/1e18 v "
                     f"from '{d}/transfers.parquet'").fetchall()
    sw = con.execute(f"select block, log_index, tx_hash, pool, cast(amount0 as hugeint) a0, cast(amount1 as hugeint) a1 "
                     f"from '{d}/swaps.parquet' order by block, log_index").fetchall()
    v3 = meta.get("v3_pools", {})
    pairs = set(meta.get("pair_addrs", [])) | set(v3)
    sw3 = []
    if v3 and (d / "swaps_v3.parquet").exists():
        sw3 = con.execute(f"select block, log_index, tx_hash, pool, cast(amount0 as hugeint) a0, cast(amount1 as hugeint) a1 "
                          f"from '{d}/swaps_v3.parquet' order by block, log_index").fetchall()
    by_tx: dict[str, list] = defaultdict(list)
    for b, li, h, s, t, v in tr:
        by_tx[h].append((s, t, float(v)))
    lo, hi = meta["deploy_block"], meta["scanned_to"]
    ts = stamp(anchors(d, lo, hi))

    rows = []
    skipped = 0
    for b, li, h, pool, a0, a1 in sw:
        p = pools.get(pool)
        if not p or p["quote_symbol"] not in ("USDC",):
            skipped += 1
            continue
        tok_is_c0 = int(token, 16) < int(p["quote"], 16)
        tok_d, q_d = (a0, a1) if tok_is_c0 else (a1, a0)
        tokens = abs(tok_d) / 1e18
        # the native USDC (quote 0x0000...) has 18 decimals; the ERC-20 USDC interface (0x3600...) has 6
        usd = abs(q_d) / (1e18 if int(p["quote"], 16) == 0 else 1e6)
        if tokens == 0:
            continue
        side = "buy" if tok_d > 0 else "sell"
        # trader: biggest net token mover in this tx, ignoring the pool manager
        net: dict[str, float] = defaultdict(float)
        for s, t, v in by_tx.get(h, []):
            net[s] -= v
            net[t] += v
        net.pop(pm, None)
        for a in pairs:
            net.pop(a, None)
        net.pop(ZERO, None)
        want = 1 if side == "buy" else -1
        cands = {a: x * want for a, x in net.items() if x * want > 0}
        trader = max(cands, key=cands.get) if cands else None
        if trader and cands[trader] < 0.5 * tokens:   # no single wallet accounts for the swap
            trader = None
        rows.append((ts(b), b, li, h, pool, trader, side, tokens, usd, usd / tokens))

    # v3 pools: amounts are the POOL's delta, so a positive token amount means the pool received tokens = a sell
    for b, li, h, pool, a0, a1 in sw3:
        info = v3[pool]
        tok_is_c0 = int(token, 16) < int(info["quote"], 16)
        tok_d, q_d = (a0, a1) if tok_is_c0 else (a1, a0)
        tokens = abs(tok_d) / 1e18
        usd = abs(q_d) / 1e6        # ERC-20 USDC interface: 6 decimals
        if tokens == 0:
            continue
        side = "sell" if tok_d > 0 else "buy"
        net: dict[str, float] = defaultdict(float)
        for s_, t_, v in by_tx.get(h, []):
            net[s_] -= v
            net[t_] += v
        net.pop(pm, None)
        for a in pairs:
            net.pop(a, None)
        net.pop(ZERO, None)
        want = 1 if side == "buy" else -1
        cands = {a: x * want for a, x in net.items() if x * want > 0}
        trader = max(cands, key=cands.get) if cands else None
        if trader and cands[trader] < 0.5 * tokens:
            trader = None
        rows.append((ts(b), b, li, h, pool, trader, side, tokens, usd, usd / tokens))
    rows.sort(key=lambda r: (r[1], r[2]))

    # running position / average cost / realised profit per trader, from trades only
    pos, cost = defaultdict(float), defaultdict(float)
    out = []
    for r in rows:
        t, side, tokens, usd = r[5], r[6], r[7], r[8]
        realised = 0.0
        if t:
            if side == "buy":
                pos[t] += tokens
                cost[t] += usd
            else:
                avg = cost[t] / pos[t] if pos[t] > 1e-9 else 0.0
                basis = avg * min(tokens, max(pos[t], 0.0))
                realised = usd - basis
                pos[t] -= tokens
                cost[t] = max(cost[t] - basis, 0.0) if pos[t] > 1e-9 else 0.0
        out.append(r + (pos[t] if t else None, (cost[t] / pos[t]) if t and pos[t] > 1e-9 else None, realised))
    cols = ["ts", "block", "log_index", "tx_hash", "pool", "trader", "side", "tokens", "usd", "price_usd",
            "pos_after", "avg_cost", "realised_usd"]
    con.execute("create table l(ts bigint, block bigint, log_index bigint, tx_hash varchar, pool varchar, trader varchar, "
                "side varchar, tokens double, usd double, price_usd double, pos_after double, avg_cost double, realised_usd double)")
    con.executemany("insert into l values (?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    con.execute(f"copy l to '{d}/ledger.parquet' (format parquet)")
    n_unattr = sum(1 for r in rows if r[5] is None)
    print(f"{meta['symbol']}: {len(out):,} swaps ledgered ({n_unattr:,} without a single trader), "
          f"{skipped:,} in non-USDC pools skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
