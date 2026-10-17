#!/usr/bin/env python3
"""Coin Board's Telegram alerts.

    coinboard_alerts.py price    # every 5 minutes (the alerts workflow): a coin moving 10% or more in 6 hours
    coinboard_alerts.py whales   # every hour, after the archive refresh: a whale selling a big part of its holding

The coins come from data/coinboard_held.json (written by the hourly build). What was already sent is kept in
data/alerts_<check>.json, so one move or one whale is reported once, not on every run; an alert counts as sent only
once Telegram has accepted it. The thresholds are the constants below. Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID;
without them it only prints what it would send (and remembers nothing).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import telegram_notify  # noqa: E402

HELD = ROOT / "data/coinboard_held.json"

PRICE_MOVE = 0.10            # a coin's 6-hour change reaching +-10%
PRICE_STEP = 0.10            # ... alerts again if it goes 10 points further the same way (+10% -> +20%)
PRICE_QUIET = 6 * 3600       # ... and otherwise not again for 6 hours, unless it turns the other way

# A whale: a wallet holding at least WHALE_SIZE x the board's whale threshold (0.25% of supply, NET 0.05%), so 0.5%
# (NET 0.1%). It alerts when its sales minus its buys over 24 hours reach WHALE_SOLD of its holding, are at least
# WHALE_SUPPLY of the coin's supply and worth WHALE_USD. Moves between wallets do not count. Measured on the 30 days
# to 16 Oct: about 11 alerts a week across WALLET, NET, FAZE and AF; most of those whales kept selling.
WHALE_SIZE = 2
WHALE_SOLD = 0.25
WHALE_SUPPLY = 0.0025
WHALE_USD = 10_000
WHALE_QUIET = 24 * 3600      # one alert per whale per day

EXPLORER = {"robinhood": "https://robinhoodchain.blockscout.com", "arc": "https://explorer.arc.io"}


# ---------- formatting (the page's conventions: a real minus sign, K / M, prices to 3-4 significant digits) ----------
def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{'+' if x >= 0 else '−'}{abs(x) * 100:.1f}%"


def usd(x: float) -> str:
    a = abs(x)
    s = f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.1f}K" if a >= 1e4 else f"${a:,.0f}"
    return ("−" if x < 0 else "") + s


def price(x: float) -> str:
    return f"${x:,.2f}" if x >= 100 else f"${x:.4g}" if x >= 0.0001 else f"${x:.3g}"


def esc(s: str) -> str:              # token names come from whoever made the token
    return html.escape(str(s))[:40]


# ---------- shared ----------
def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def get_json(url: str):
    req = urllib.request.Request(url, headers={"user-agent": "coinboard-alerts"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main_pair(c: dict) -> dict | None:
    """The coin's chart pair on DexScreener (the one the board shows), else its deepest pair with the coin as base."""
    ps = get_json(f"https://api.dexscreener.com/token-pairs/v1/{c['chain']}/{c['token']}")
    ps = [p for p in (ps if isinstance(ps, list) else []) if p.get("chainId") == c["chain"]
          and (p.get("baseToken") or {}).get("address", "").lower() == c["token"].lower() and p.get("liquidity")]
    return next((p for p in ps if p["pairAddress"].lower() == (c.get("pair") or "").lower()), None) or \
        max(ps, key=lambda p: p["liquidity"].get("usd") or 0, default=None)


def send(alerts: list[tuple[str, str, object]], seen: dict) -> None:
    """Send each (message, key, memory) and remember it only once Telegram has it, so a failed send is tried again."""
    for msg, k, mem in alerts:
        print("alert:", msg.split("\n")[0])
        if not telegram_notify.configured():
            continue
        try:
            telegram_notify.send(msg)
            seen[k] = mem
        except Exception as e:
            print(f"warning: Telegram did not take it ({e}); trying again next run")


# ---------- price: a 10% move in 6 hours ----------
def price_alerts(coins: list[dict], seen: dict, now: float) -> list[tuple[str, str, object]]:
    out = []
    for c in coins:
        try:
            p = main_pair(c)
        except Exception as e:
            print(f"warning: no DexScreener answer for {c['name']} ({e})")
            continue
        ch = ((p or {}).get("priceChange") or {}).get("h6")
        if ch is None or abs(float(ch)) / 100 < PRICE_MOVE:
            continue
        ch = float(ch) / 100
        k, d = f"{c['chain']}:{c['token'].lower()}", 1 if ch > 0 else -1
        last = seen.get(k)
        if last and now - last["t"] < PRICE_QUIET and last["dir"] == d and abs(ch) < abs(last["level"]) + PRICE_STEP:
            continue
        px, pc, vol = float(p["priceUsd"]), p.get("priceChange") or {}, (p.get("volume") or {}).get("h6")
        mc = p.get("fdv") or p.get("marketCap")
        val = c.get("qty", 0) * px
        hold = f"\nYour holding: {usd(val)} ({'+' if ch >= 0 else '−'}{usd(abs(val * ch / (1 + ch)))} in 6 hours)" if val >= 1 else ""
        out.append((f"{'🟢' if ch > 0 else '🔴'} <b>{esc(c['name'])} {pct(ch)} in 6 hours</b>\n\n"
                   f"Price <b>{price(px)}</b>" + (f" · Mcap <b>{usd(mc)}</b>" if mc else "") + "\n"
                   f"1h {pct(pc.get('h1') / 100 if pc.get('h1') is not None else None)} · "
                   f"24h {pct(pc.get('h24') / 100 if pc.get('h24') is not None else None)}"
                   + (f" · 6h volume {usd(vol)}" if vol else "") + hold +
                   f"\n\n<a href=\"https://dexscreener.com/{c['chain']}/{p['pairAddress']}\">Open chart</a>",
                    k, {"t": now, "dir": d, "level": ch}))
    return out


# ---------- whales: a big holder selling a large part of its holding (hourly, from the archives just refreshed) ----------
def whale_alerts(coins: list[dict], seen: dict, now: float) -> list[tuple[str, str, object]]:
    import duckdb
    import dashboard_analytics as an           # the board's own whale threshold and list of pools / protocol addresses
    out, con = [], duckdb.connect()
    for c in coins:
        cfg = an.coin_config(c)
        d = ROOT / cfg["dir"]
        if not (d / "transfers.parquet").exists() or not (d / "ledger.parquet").exists():
            continue
        excl, dec = an.excluded(cfg, d), int(cfg["dec"])
        T, L = f"'{d}/transfers.parquet'", f"'{d}/ledger.parquet'"
        sup = con.execute(f"""select (sum(case when src in ('{an.ZERO}','{an.DEAD}') then cast(value as hugeint) else 0 end)
                                    - sum(case when dst in ('{an.ZERO}','{an.DEAD}') then cast(value as hugeint) else 0 end)) / 1e{dec}
                               from {T}""").fetchone()[0] or 0
        last_ts = con.execute(f"select max(ts) from {L}").fetchone()[0]
        if not sup or not last_ts or now - last_ts > 6 * 3600:     # no supply, or the archive is stale: say nothing
            continue
        t0 = last_ts - 86400
        # wallets whose sales minus buys over the last 24 hours are big enough to matter
        cands = con.execute(f"""select lower(trader), sum(case when side = 'sell' then tokens else -tokens end) q,
                                       sum(case when side = 'sell' then usd else -usd end) u, max(case when side = 'sell' then ts end) last_sell
                                  from {L} where ts > {t0} group by 1
                                 having q >= {WHALE_SUPPLY * sup} and u >= {WHALE_USD}""").fetchall()
        whale = WHALE_SIZE * cfg["th"][0] / 100 * sup
        for addr, q, u, last_sell in cands:
            k = f"{c['chain']}:{c['token'].lower()}:{addr}"
            if addr in excl or now - seen.get(k, -1e18) < WHALE_QUIET:
                continue
            # its holding over the window: the balance when the window opened, then every transfer since (peak = most held)
            b0 = con.execute(f"""select min(block) from {L} where ts > {t0}""").fetchone()[0]
            start = con.execute(f"""select coalesce(sum(case when lower(dst) = '{addr}' then cast(value as hugeint) else 0 end)
                                              - sum(case when lower(src) = '{addr}' then cast(value as hugeint) else 0 end), 0) / 1e{dec}
                                      from {T} where block < {b0} and (lower(src) = '{addr}' or lower(dst) = '{addr}')""").fetchone()[0]
            bal = peak = float(start)
            for src, v in con.execute(f"""select lower(src), cast(value as hugeint) / 1e{dec} from {T}
                                           where block >= {b0} and (lower(src) = '{addr}' or lower(dst) = '{addr}') order by block, log_index""").fetchall():
                bal += -float(v) if src == addr else float(v)
                peak = max(peak, bal)
            if peak < whale or q < WHALE_SOLD * peak:
                continue
            ex = EXPLORER.get(c["chain"], "")
            out.append((f"🐋 <b>Whale selling {esc(c['name'])}: {usd(u)} in 24 hours</b>\n\n"
                        f"A wallet that held {peak / sup:.2%} of the supply sold {min(q / peak, 1):.0%} of it "
                        f"({q / sup:.2%} of the supply), net of any buys.\n"
                        f"It now holds {max(bal, 0) / sup:.2%}. Last sale {time.strftime('%H:%M UTC', time.gmtime(last_sell))}.\n\n"
                        + (f"<a href=\"{ex}/address/{addr}\">The wallet</a> · " if ex else "")
                        + f"<a href=\"https://dexscreener.com/{c['chain']}/{c.get('pair') or c['token']}\">Open chart</a>",
                        k, now))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=["price", "whales"])
    a = ap.parse_args()
    coins = load(HELD, [])
    if not coins:
        print("no holdings list yet (data/coinboard_held.json comes from the hourly update)")
        return 0
    path, now = ROOT / f"data/alerts_{a.check}.json", time.time()
    seen = load(path, {})
    before = json.dumps(seen, sort_keys=True)
    try:
        alerts = price_alerts(coins, seen, now) if a.check == "price" else whale_alerts(coins, seen, now)
        print(f"{a.check}: {len(coins)} coins checked, {len(alerts)} alert(s)")
        send(alerts, seen)
    except Exception as e:                       # an alert hiccup must never fail the run (or the hourly update)
        print(f"warning: {a.check} alerts failed ({e})")
    if a.check == "whales":                      # forget whales after a week
        seen = {k: t for k, t in seen.items() if now - t < 7 * 86400}
    changed = json.dumps(seen, sort_keys=True) != before
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"changed={int(changed)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
