#!/usr/bin/env python3
"""How much of a holding could be sold right now, from live swap quotes (KyberSwap's aggregator routes a sale across
every pool, so this is what a sale would really get): the price impact of selling all of it, what it would fetch, and
the share of it that sells before the price moves 5%. Impact is measured against a tiny sale, so pool fees don't count.

    exit_liquidity.py CHAIN TOKEN QTY [DECIMALS]     # one holding, printed as JSON
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

KYBER = "https://aggregator-api.kyberswap.com/{chain}/api/v1/routes?"
SELL_INTO = {"robinhood": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",   # WETH: the deepest pairs on Robinhood Chain
             "arc": "0x3600000000000000000000000000000000000000"}         # USDC on Arc
STEPS = (1 / 16, 1 / 8, 1 / 4, 1 / 2, 1)        # shares of the holding quoted


def quote(chain: str, token: str, amount: int) -> tuple[int, float]:
    """(amount out, its dollar value) for selling `amount` (raw units) of token."""
    q = urllib.parse.urlencode({"tokenIn": token, "tokenOut": SELL_INTO[chain], "amountIn": str(amount)})
    req = urllib.request.Request(KYBER.format(chain=chain) + q, headers={"x-client-id": "coinboard", "user-agent": "coinboard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        s = json.loads(r.read())["data"]["routeSummary"]
    return int(s["amountOut"]), float(s.get("amountOutUsd") or 0)


def exit_liquidity(chain: str, token: str, qty: float, dec: int = 18) -> dict | None:
    if chain not in SELL_INTO or qty <= 0:
        return None
    raw = int(qty * 10 ** dec)
    small = max(raw // 2000, 1)
    rate0 = quote(chain, token, small)[0] / small
    if rate0 <= 0:
        return None
    pts, got = [], 0.0
    for f in STEPS:
        a = max(int(raw * f), 1)
        out, usd = quote(chain, token, a)
        pts.append((f, max(0.0, 1 - (out / a) / rate0)))
        got = usd
    impact = pts[-1][1]
    # the share that sells before a 5% impact, between the two quotes either side of it
    sell5 = 1.0
    if impact > 0.05:
        prev = (0.0, 0.0)
        for f, i in pts:
            if i > 0.05:
                sell5 = prev[0] + (f - prev[0]) * (0.05 - prev[1]) / (i - prev[1]) if i > prev[1] else prev[0]
                break
            prev = (f, i)
    return {"impact": round(impact, 4), "usd": round(got, 2), "sell5": round(sell5, 3)}


if __name__ == "__main__":
    a = sys.argv[1:]
    print(json.dumps(exit_liquidity(a[0], a[1], float(a[2]), int(a[3]) if len(a) > 3 else 18)))
