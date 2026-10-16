#!/usr/bin/env python3
"""Full history of a token on Arc mainnet (chain 5042): every Transfer and every
Uniswap v4 swap in the token's pools, from deployment to the chain head.

    arc_archive.py 0xTOKEN            # first run: from deployment; later runs: resume

Writes data/arc/<token>/ with
    transfers.parquet   block, log_index, tx_hash, src, dst, value (raw, string)
    swaps.parquet       block, log_index, tx_hash, pool, sender, amount0, amount1,
                        sqrt_price, liquidity, tick, fee   (v4 Swap events)
    meta.json           symbol, pools, quote tokens, PoolManager, deployment and
                        last scanned block

Arc's official public RPC (rpc.mainnet.arc.io) answers eth_getLogs for ranges up
to about 9,900 blocks and refuses larger ones, so history is read in 9,000-block
chunks (0.5 s blocks: about 4.7M blocks a month) and a chunk is halved if the node
objects. Progress is saved as it goes, so a killed run resumes where it stopped.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
RPC = "https://rpc.mainnet.arc.io"
CHUNK = 9_000
WORKERS = 3
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
T_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"   # Uniswap v3 pool Swap

_client = httpx.Client(timeout=90)


def rpc(method: str, params: list, tries: int = 10):
    last = ""
    for i in range(tries):
        try:
            r = _client.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if r.status_code in (429, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(min(2 + 3 * i, 30))
                continue
            j = r.json()
            if "error" in j:
                msg = str(j["error"])
                if any(k in msg.lower() for k in ("range", "too large", "max results", "too many", "exceed")):
                    raise RangeError(msg)
                last = msg[:150]
                time.sleep(min(1 + 2 * i, 20))
                continue
            return j["result"]
        except RangeError:
            raise
        except (httpx.HTTPError, ValueError) as e:
            last = type(e).__name__
            time.sleep(min(1 + 2 * i, 20))
    raise RuntimeError(f"{method} failed after {tries} tries: {last}")


class RangeError(RuntimeError):
    pass


def logs(flt: dict, a: int, b: int) -> list[dict]:
    """All logs for flt in [a, b], splitting the range when the node refuses it."""
    try:
        return rpc("eth_getLogs", [{**flt, "fromBlock": hex(a), "toBlock": hex(b)}])
    except RangeError:
        if b - a < 50:
            raise
        m = (a + b) // 2
        return logs(flt, a, m) + logs(flt, m + 1, b)


def scan(flt: dict, start: int, end: int) -> list[dict]:
    spans = [(a, min(a + CHUNK - 1, end)) for a in range(start, end + 1, CHUNK)]
    out: list[dict] = []
    with ThreadPoolExecutor(WORKERS) as ex:
        for part in ex.map(lambda s: logs(flt, *s), spans):
            out += part
    return out


def code_at(addr: str, block: int) -> int:
    return len(rpc("eth_getCode", [addr, hex(block)])) // 2 - 1


def deployment_block(addr: str, head: int) -> int:
    lo, hi = 1, head
    while lo < hi:
        mid = (lo + hi) // 2
        if code_at(addr, mid) > 0:
            hi = mid
        else:
            lo = mid + 1
    return lo


def signed(word: str, bits: int = 256) -> int:
    v = int(word, 16)
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def dexscreener_pools(token: str) -> list[dict]:
    r = httpx.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=30).json()
    return [p for p in r.get("pairs") or []
            if p.get("chainId") == "arc" and p["baseToken"]["address"].lower() == token.lower()
            or p.get("chainId") == "arc" and p["quoteToken"]["address"].lower() == token.lower()]


def v3_pools(token: str, meta: dict, head: int) -> None:
    """Find Uniswap-v3-style pools (42-char DexScreener pairs) that pair the token with USDC and scan their
    Swap logs into swaps_v3.parquet. v3 amounts are the POOL's delta (positive = pool received = a sell)."""
    d = ROOT / "data/arc" / token
    found = meta.setdefault("v3_pools", {})
    for a in meta.get("other_pairs", []):
        a = a.lower()
        if a in found:
            continue
        try:
            t0 = "0x" + rpc("eth_call", [{"to": a, "data": "0x0dfe1681"}, "latest"], tries=2)[-40:]
            t1 = "0x" + rpc("eth_call", [{"to": a, "data": "0xd21220a7"}, "latest"], tries=2)[-40:]
            rpc("eth_call", [{"to": a, "data": "0x3850c7bd"}, "latest"], tries=2)     # slot0: v3 only
        except RuntimeError:
            continue
        if token not in (t0, t1):
            continue
        q = t1 if t0 == token else t0
        if q != "0x3600000000000000000000000000000000000000":     # only USDC-quoted pools are valued
            continue
        found[a] = {"token0": t0, "token1": t1, "quote": q, "deploy_block": deployment_block(a, head)}
        print(f"v3 pool {a[:10]}… found (quote USDC), deployed at block {found[a]['deploy_block']:,}")
    for a, info in found.items():
        lo = max(info.get("scanned_to", info["deploy_block"] - 1) + 1, info["deploy_block"])
        if lo > head:
            continue
        rows = []
        for x in range(lo, head + 1, 500_000):
            y = min(x + 499_999, head)
            for l in scan({"address": a, "topics": [T_V3_SWAP]}, x, y):
                w = [l["data"][2 + 64 * i: 66 + 64 * i] for i in range(5)]
                rows.append((int(l["blockNumber"], 16), int(l["logIndex"], 16), l["transactionHash"], a,
                             "0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:], str(signed(w[0])), str(signed(w[1])),
                             str(int(w[2], 16)), str(int(w[3], 16)), signed(w[4][-6:], 24)))
        cols = ["block", "log_index", "tx_hash", "pool", "sender", "recipient", "amount0", "amount1", "sqrt_price", "liquidity", "tick"]
        types = {"block": pa.int64(), "log_index": pa.int64(), "tick": pa.int64()}
        t = pa.table({c: pa.array([r[i] for r in rows], types.get(c, pa.string())) for i, c in enumerate(cols)})
        path = d / "swaps_v3.parquet"
        if path.exists():
            t = pa.concat_tables([pq.read_table(path), t])
        seen, keep = set(), []
        for i, k in enumerate(zip(t["pool"].to_pylist(), t["block"].to_pylist(), t["log_index"].to_pylist())):
            if k not in seen:
                seen.add(k)
                keep.append(i)
        pq.write_table(t.take(keep), path)
        info["scanned_to"] = head
        print(f"  v3 pool {a[:10]}…: {len(rows):,} swaps to block {head:,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("token")
    ap.add_argument("--out", type=Path, default=ROOT / "data/arc")
    args = ap.parse_args()
    token = args.token.lower()
    d = args.out / token
    d.mkdir(parents=True, exist_ok=True)
    meta_p = d / "meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}

    head = int(rpc("eth_blockNumber", []), 16) - 5      # stay clear of reorg-prone tip
    if "deploy_block" not in meta:
        meta["deploy_block"] = deployment_block(token, head)
        print(f"deployed at block {meta['deploy_block']:,}")
    # pools: DexScreener names them; v4 pool ids are the 32-byte pair addresses
    pools = {p["pairAddress"].lower(): p for p in dexscreener_pools(token) if len(p["pairAddress"]) == 66}
    meta["pools"] = {**meta.get("pools", {}), **{
        k: {"quote": p["quoteToken"]["address"].lower(), "quote_symbol": p["quoteToken"]["symbol"],
            "base": p["baseToken"]["address"].lower(), "created_ms": p.get("pairCreatedAt")}
        for k, p in pools.items()}}
    meta["other_pairs"] = [p["pairAddress"] for p in dexscreener_pools(token) if len(p["pairAddress"]) != 66]
    meta["symbol"] = meta.get("symbol") or next((p["baseToken"]["symbol"] for p in pools.values()
                                                 if p["baseToken"]["address"].lower() == token), "?")
    meta["name"] = meta.get("name") or next((p["baseToken"]["name"] for p in pools.values()
                                             if p["baseToken"]["address"].lower() == token), "?")
    meta['pair_addrs'] = [a.lower() for a in meta.get('other_pairs', [])]
    v3_pools(token, meta, head)
    meta_p.write_text(json.dumps(meta, indent=1))
    start = max(meta.get("scanned_to", meta["deploy_block"] - 1) + 1, meta["deploy_block"])
    if start > head:
        print("already up to date")
        return 0
    print(f"{meta['symbol']}: scanning blocks {start:,}-{head:,} ({head - start + 1:,}), {len(meta['pools'])} v4 pools")

    def rows_t(ls):
        for l in ls:
            if len(l["topics"]) < 3:
                continue
            yield (int(l["blockNumber"], 16), int(l["logIndex"], 16), l["transactionHash"],
                   "0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:], str(int(l["data"], 16)))

    def rows_s(ls):
        for l in ls:
            w = [l["data"][2 + 64 * i: 66 + 64 * i] for i in range(6)]
            yield (int(l["blockNumber"], 16), int(l["logIndex"], 16), l["transactionHash"], l["topics"][1],
                   "0x" + l["topics"][2][-40:], str(signed(w[0])), str(signed(w[1])), str(int(w[2], 16)),
                   str(int(w[3], 16)), signed(w[4][-6:], 24), int(w[5][-6:], 16))

    def merge(path: Path, cols: list[str], new_rows: list[tuple]):
        types = {"block": pa.int64(), "log_index": pa.int64(), "tick": pa.int64(), "fee": pa.int64()}
        t = pa.table({c: pa.array([r[i] for r in new_rows], types.get(c, pa.string())) for i, c in enumerate(cols)})
        if path.exists():
            t = pa.concat_tables([pq.read_table(path), t])
        seen, keep = set(), []
        for i, k in enumerate(zip(t["block"].to_pylist(), t["log_index"].to_pylist())):
            if k not in seen:
                seen.add(k)
                keep.append(i)
        pq.write_table(t.take(keep), path)

    t0 = time.time()
    SEG = 500_000
    ids = list(meta["pools"])
    n_t = n_s = 0
    for a in range(start, head + 1, SEG):
        b = min(a + SEG - 1, head)
        tr = scan({"address": token, "topics": [T_TRANSFER]}, a, b)
        sw = scan({"topics": [T_V4_SWAP, ids]}, a, b) if ids else []
        if sw and "pool_manager" not in meta:
            meta["pool_manager"] = sw[0]["address"].lower()
        merge(d / "transfers.parquet", ["block", "log_index", "tx_hash", "src", "dst", "value"], list(rows_t(tr)))
        if sw:
            merge(d / "swaps.parquet", ["block", "log_index", "tx_hash", "pool", "sender", "amount0", "amount1",
                                        "sqrt_price", "liquidity", "tick", "fee"], list(rows_s(sw)))
        n_t += len(tr)
        n_s += len(sw)
        meta["scanned_to"] = b
        meta["updated"] = int(time.time())
        meta_p.write_text(json.dumps(meta, indent=1))
        print(f"  to block {b:,}: {n_t:,} transfers, {n_s:,} swaps so far ({time.time() - t0:.0f}s)", flush=True)
    print(f"saved to {d.relative_to(ROOT)}; scanned to block {head:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
