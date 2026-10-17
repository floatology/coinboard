#!/usr/bin/env python3
"""Coin Board's Telegram alerts.

    coinboard_alerts.py tick     # every 5 minutes (the alerts workflow): price moves, liquidity pulls, the daily summary
    coinboard_alerts.py hourly   # every hour, after the archive refresh: whales selling or buying, exit liquidity

  price      a held coin moving 10% or more over 6, 12 or 24 hours: one message per coin with all three changes. A window
             alerts again only after its own length, or 10 points further the same way, or when the move turns.
  liquidity  all of a coin's pools together losing 30% or more of their dollar liquidity within an hour, seen on two
             checks in a row (a rug warning); again after 6 hours or a further 30% drop.
  summary    once a day at 08:00 Bangkok time: the portfolio and each coin over 24 hours, whale alerts, holder changes.
  report     Sundays after the summary: the week's alerts by type, and where the price went in the 24 hours after each
             (its price 24 hours on comes from the 5-minute check's own readings, so coins no longer held or watched
             have none).
  whales     selling: a wallet holding at least 0.5% of supply (NET 0.1%) whose sales minus buys over 24 hours reach a
             quarter of its holding, 0.25% of supply and $10,000. Buying: a wallet whose buys minus sales over 24 hours
             reach 0.5% of supply and $10,000. Moves between wallets don't count. One alert per wallet and side per day.
  team       wallets that got a whale-sized amount in a coin's first 30 seconds of trading (often the team or its
             snipers), its creator, and wallets they have passed tokens to: any of them selling or moving 0.25% of supply
             and $1,000 within 24 hours. One message per coin listing them; one alert per wallet per day.
  levels     the price alerts set on the page (tap a coin's price): once, when a 5-minute reading reaches the level.
  exit       selling a whole holding at once would move the price 10%, 20% or 35% (live swap quotes taken by the hourly
             build): once per mark, again after the impact has eased to under 70% of that mark. Holdings only.

The page's watchlist gets every alert too, each message starting "Watchlist" (whales only for coins on Robinhood Chain
and Arc, whose trades the hourly update archives). It is read from the page's database when the GitHub secrets
KV_REST_API_URL and KV_REST_API_READ_ONLY_TOKEN are set (copied from the Vercel project's Upstash database).

The holdings come from data/coinboard_held.json (written by the hourly build); data/coinboard_facts.json (the day's
whale alerts and holder counts, for the summary) is written by the hourly check. Both reach the 5-minute check through
a small cache. What was sent is kept in data/alerts_<check>.json; an alert counts as sent only once Telegram has it.
Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; without them it prints what it would send and remembers no alerts.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import telegram_notify  # noqa: E402

HELD = ROOT / "data/coinboard_held.json"
FACTS = ROOT / "data/coinboard_facts.json"
EXITS = ROOT / "data/coinboard_exit.json"
TEAMS = ROOT / "data/coinboard_team.json"

PRICE_WINDOWS = (6, 12, 24)  # hours
PRICE_MOVE = 0.10            # a change of +-10% over a window alerts
PRICE_STEP = 0.10            # ... again 10 points further the same way (+10% -> +20%), or after the window's own length

LIQ_DROP = 0.30              # all pools losing 30% of their dollar liquidity ...
LIQ_WINDOW = 3600            # ... within an hour
LIQ_QUIET = 6 * 3600

# Whales, measured on the 30 days to 16 Oct: selling about 11 alerts a week across WALLET, NET, FAZE and AF (most of
# those whales kept selling), buying about 11 a week.
WHALE_SIZE = 2               # x the board's whale threshold (0.25% of supply, NET 0.05%)
WHALE_SOLD = 0.25            # share of its holding
WHALE_SUPPLY = 0.0025        # share of supply sold
WHALE_BUY_SUPPLY = 0.005     # share of supply bought
WHALE_USD = 10_000
WHALE_QUIET = 24 * 3600

# Launch wallets, measured on the 30 days to 17 Oct: FAZE's still held 49% of supply (5.6 alerts a week; 23h later the
# price was down in 78% of cases, median -17%, against 58% and -5.6% for any hour), AF's 5% (4.9 a week, no edge),
# WALLET's and NET's nothing.
TEAM_LAUNCH = 30             # seconds after the first buy
TEAM_MOVE = 0.0025           # share of supply a wallet sells or moves within 24 hours ...
TEAM_USD = 1_000             # ... and its value
TEAM_LINK = 0.2              # a plain transfer of this share of the whale threshold to a new wallet links that wallet

EXIT_MARKS = (0.10, 0.20, 0.35)   # price impact of selling a whole holding at once

REPORT_DAY = 6               # the weekly report card: Sundays (Monday = 0), with the daily summary
REPORT_KEEP = 9 * 86400      # alerts remembered for it

SUMMARY_HOUR = 8             # the daily summary at 08:00 ...
TZ_HOURS = 7                 # ... Bangkok time (UTC+7, no daylight saving), the time every alert shows
EXPLORER = {"robinhood": "https://robinhoodchain.blockscout.com", "arc": "https://explorer.arc.io"}
RPC = {"robinhood": "https://rpc.mainnet.chain.robinhood.com", "arc": "https://rpc.mainnet.arc.io"}
WATCH = "<b>Watchlist</b> · "   # how a watchlist coin's message starts (shows in the notification)


def tag(c: dict) -> str:
    return WATCH if c.get("watch") else ""


# ---------- formatting (the page's conventions: a real minus sign, K / M, prices to 4 significant digits) ----------
def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{'+' if x >= 0 else '−'}{abs(x) * 100:.1f}%"


def usd(x: float) -> str:
    a = abs(x)
    s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.1f}K" if a >= 1e4 else f"${a:,.0f}"
    return ("−" if x < 0 else "") + s


def price(x: float) -> str:
    if x >= 100:
        return f"${x:,.2f}"
    if x >= 0.0001 or x <= 0:
        return f"${x:.4g}"
    return f"${x:.{2 - math.floor(math.log10(x))}f}"   # tiny prices in full: $0.00000399, not $3.99e-06


def bkk(t: float) -> str:
    return time.strftime("%H:%M", time.gmtime(t + TZ_HOURS * 3600)) + " Bangkok time"


def esc(s: str) -> str:              # token names come from whoever made the token
    return html.escape(str(s))[:40]


def chart(c: dict, pair: str = "") -> str:
    return f"<a href=\"https://dexscreener.com/{c['chain']}/{pair or c.get('pair') or c['token']}\">Open chart</a>"


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


def key(c: dict) -> str:
    return f"{c['chain']}:{c['token'].lower()}"


def dex_pairs(c: dict) -> list[dict]:
    """The coin's pairs on its chain (DexScreener), the chart pair first, else the deepest with the coin as base."""
    ps = get_json(f"https://api.dexscreener.com/token-pairs/v1/{c['chain']}/{c['token']}")
    ps = [p for p in (ps if isinstance(ps, list) else []) if p.get("chainId") == c["chain"] and p.get("liquidity")]
    base = sorted((p for p in ps if (p.get("baseToken") or {}).get("address", "").lower() == c["token"].lower()),
                  key=lambda p: -(p["liquidity"].get("usd") or 0))
    main = next((p for p in base if p["pairAddress"].lower() == (c.get("pair") or "").lower()), base[0] if base else None)
    return ([main] if main else []) + [p for p in ps if p is not main]


def kv_settings() -> tuple[str, str]:
    """The database's REST address and read-only token from the secrets. Each may hold just the value, or the whole
    two-line snippet Upstash's copy button gives (UPSTASH_REDIS_REST_URL="..." / UPSTASH_REDIS_REST_TOKEN="...")."""
    import re
    raw_url, raw_tok = os.environ.get("KV_REST_API_URL", ""), os.environ.get("KV_REST_API_READ_ONLY_TOKEN", "")
    m = re.search(r"https://[^\s\"']+", raw_url)
    url = m.group(0) if m else ""
    m = re.search(r"TOKEN\s*=\s*[\"']?([^\s\"']+)", raw_tok)
    tok = m.group(1) if m else raw_tok.strip().strip("\"'")
    return url, ("" if set(tok) <= {"*"} else tok)


def kv_get(name: str) -> list:
    """A list the page keeps in its Upstash database (read-only); [] without the secrets."""
    url, tok = kv_settings()
    if not url or not tok:
        if os.environ.get("KV_REST_API_URL") or os.environ.get("KV_REST_API_READ_ONLY_TOKEN"):
            print("warning: the watchlist secrets are set but no address or token could be read from them (a token copied "
                  "while hidden is only stars: copy it again with the eye icon on)")
        return []
    req = urllib.request.Request(url, data=json.dumps(["GET", name]).encode(),
                                 headers={"authorization": f"Bearer {tok}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        v = json.loads(r.read()).get("result")
    items = json.loads(v) if v else []
    return items if isinstance(items, list) else []


def watchlist() -> list[dict]:
    """The coins on the page's watchlist, as coin entries."""
    items = kv_get("coinboard:watch")
    return [{"name": x.get("symbol") or "?", "chain": str(x["chain"]), "token": str(x["token"]), "pair": str(x.get("pair") or ""),
             "watch": True} for x in items if isinstance(x, dict) and x.get("chain") and x.get("token")][:30]


def record(kind: str, c: dict, now: float, px: float | None, d: int = 0, **extra) -> dict:
    """An alert as the weekly report card sees it: d is the way it points the price (+1 up, -1 down, 0 neither)."""
    return {"t": now, "kind": kind, "coin": c["name"], "key": key(c), "watch": bool(c.get("watch")), "px": px, "dir": d, **extra}


def send(alerts: list[tuple[str, dict, dict | None]], state: dict) -> None:
    """Send each (message, memory, log entry); keep its memory only once Telegram has it, so a failed send is tried again."""
    for msg, mem, log in alerts:
        first = msg.split("\n")[0]                     # the log is public: no watchlist coins, no price alert levels
        print("alert:", "a watchlist alert" if msg.startswith(WATCH) else "a price alert" if "(your price alert)" in first else first)
        if not telegram_notify.configured():
            continue
        try:
            telegram_notify.send(msg)
        except Exception as e:
            print(f"warning: Telegram did not take it ({e}); trying again next run")
            continue
        state.setdefault("sent", {}).update(mem)
        if log:
            state.setdefault("log", []).append(log)


# ---------- price: 10% over 6, 12 or 24 hours ----------
def changes(c: dict, p: dict, now: float, hist: list) -> dict[int, float]:
    pc, out = p.get("priceChange") or {}, {}
    for h, f in ((6, "h6"), (24, "h24")):
        if pc.get(f) is not None:
            out[h] = float(pc[f]) / 100
    # DexScreener has no 12-hour figure: the price 12 hours ago comes from the hourly history the build passes on (held
    # coins) or from this check's own readings (watchlist coins, once it has watched them for 12 hours)
    ref = None
    for ts, px in hist:
        if ts <= now - 12 * 3600:
            ref = px
    if ref and hist[0][0] <= now - 12 * 3600 - 1800:
        out[12] = float(p["priceUsd"]) / ref - 1
    return dict(sorted(out.items()))


def price_alerts(coins: list[dict], pairs: dict, state: dict, now: float) -> list:
    out, sent = [], state.get("sent", {})
    for c in coins:
        ps = pairs.get(key(c))
        if not ps:
            continue
        p = ps[0]
        chs = changes(c, p, now, c.get("hist") or state.get("px", {}).get(key(c), []))
        fired = []
        for h, ch in chs.items():
            last, d = sent.get(f"p:{key(c)}:{h}"), 1 if ch > 0 else -1
            if abs(ch) < PRICE_MOVE or (last and now - last["t"] < h * 3600 and last["dir"] == d
                                        and abs(ch) < abs(last["level"]) + PRICE_STEP):
                continue
            fired.append(h)
        if not fired:
            continue
        h = max(fired, key=lambda w: abs(chs[w]))
        ch, px, pc = chs[h], float(p["priceUsd"]), p.get("priceChange") or {}
        mc, val = p.get("fdv") or p.get("marketCap"), c.get("qty", 0) * px
        hold = f"\nYour holding: {usd(val)} ({'+' if ch >= 0 else '−'}{usd(abs(val * ch / (1 + ch)))} in {h} hours)" if val >= 1 else ""
        out.append((f"{tag(c)}{'🟢' if ch > 0 else '🔴'} <b>{esc(c['name'])} {pct(ch)} in {h} hours</b>\n\n"
                    + " · ".join(([f"1h {pct(float(pc['h1']) / 100)}"] if pc.get("h1") is not None else [])
                                 + [f"{w}h {pct(x)}" for w, x in chs.items()]) + "\n"
                    f"Price <b>{price(px)}</b>" + (f" · Mcap <b>{usd(mc)}</b>" if mc else "") + hold
                    + "\n\n" + chart(c, p["pairAddress"]),
                    {f"p:{key(c)}:{w}": {"t": now, "dir": 1 if chs[w] > 0 else -1, "level": chs[w]} for w in fired},
                    record("price", c, now, px, 1 if ch > 0 else -1)))
    return out


# ---------- liquidity: all pools losing 30% within an hour ----------
def liquidity_alerts(coins: list[dict], pairs: dict, state: dict, now: float) -> list:
    out, sent, hist = [], state.get("sent", {}), state.setdefault("liq", {})
    for c in coins:
        ps = pairs.get(key(c))
        if not ps:
            continue
        total = sum(p["liquidity"].get("usd") or 0 for p in ps)
        px = float(ps[0]["priceUsd"])
        h = [s for s in hist.get(key(c), []) if now - s[0] < 2 * LIQ_WINDOW]
        prev = h[-1] if h else None
        h.append([now, total, px])
        hist[key(c)] = h
        # the highest level in the hour before (samples at least 10 minutes old), and the drop seen on two checks in a row
        window = [s for s in h if now - LIQ_WINDOW - 300 <= s[0] <= now - 600]
        if not window or not prev or now - prev[0] > 15 * 60:
            continue
        peak = max(window, key=lambda s: s[1])
        if peak[1] < 1000 or 1 - total / peak[1] < LIQ_DROP or 1 - prev[1] / peak[1] < LIQ_DROP * 0.8:
            continue
        last = sent.get(f"l:{key(c)}")
        if last and now - last["t"] < LIQ_QUIET and total > last["level"] * (1 - LIQ_DROP):
            continue
        mins = round((now - peak[0]) / 60)
        out.append((f"{tag(c)}🚨 <b>{esc(c['name'])} liquidity down {1 - total / peak[1]:.0%} in {mins} minutes</b>\n\n"
                    f"Its pools hold {usd(total)}, from {usd(peak[1])} at {bkk(peak[0])}.\n"
                    f"Price in the same time: {pct(px / peak[2] - 1)}.\n\n"
                    "A sudden drop like this can mean liquidity is being pulled (a rug), which makes the coin hard to sell.\n\n"
                    + chart(c, ps[0]["pairAddress"]),
                    {f"l:{key(c)}": {"t": now, "level": total}}, record("liquidity", c, now, px, -1)))
    return out


# ---------- the daily summary ----------
def summary(coins: list[dict], pairs: dict, state: dict, now: float, watch: list[dict] = (), force: bool = False) -> list:
    local = now + TZ_HOURS * 3600
    day = time.strftime("%Y-%m-%d", time.gmtime(local))
    if not force and (time.gmtime(local).tm_hour < SUMMARY_HOUR or state.get("sent", {}).get("summary", {}).get("day") == day):
        return []
    rows, total, before = [], 0.0, 0.0
    for c in coins:
        ps = pairs.get(key(c))
        if not ps:
            continue
        px, h24 = float(ps[0]["priceUsd"]), (ps[0].get("priceChange") or {}).get("h24")
        v = c.get("qty", 0) * px
        ch = float(h24) / 100 if h24 is not None else None
        total += v
        before += v / (1 + ch) if ch is not None else v
        rows.append((c["name"], v, ch))
    if not rows:
        return []
    rows.sort(key=lambda r: -r[1])
    table = "\n".join(f"{esc(n)} {usd(v)} · {pct(ch)}" for n, v, ch in rows)
    facts = load(FACTS, {})
    wl = [x for x in facts.get("whales", []) if now - x["t"] < 86400]
    def whales(side: str) -> str:
        xs = [x for x in wl if x["side"] == side]
        named = ", ".join(esc(x["coin"]) + (" (watchlist)" if x.get("watch") else "") + " " + usd(x["usd"]) for x in xs[:4])
        return f"{len(xs)} {side}{'s' if len(xs) != 1 else ''}" + (f" ({named})" if xs else "")
    hold = facts.get("holders", {})
    holders = "\n".join(f"{esc(n)} {'+' if v[0] >= v[1] else '−'}{abs(v[0] / v[1] - 1) * 100:.2f}%" for n, v in hold.items() if len(v) == 2 and v[1])
    move = max(rows, key=lambda r: abs(r[2] or 0))
    wrows = []
    for c in watch:
        ps = pairs.get(key(c))
        if ps:
            h24 = (ps[0].get("priceChange") or {}).get("h24")
            wrows.append((c["name"], float(ps[0]["priceUsd"]), float(h24) / 100 if h24 is not None else None))
    wtable = "\n".join(f"{esc(n)} {price(px)} · {pct(ch)}" for n, px, ch in wrows)
    return [(f"☀️ <b>Daily summary</b> · {time.strftime('%a %d %b', time.gmtime(local))}\n\n"
             f"Portfolio <b>{usd(total)}</b>  {pct(total / before - 1 if before else None)} "
             f"({'+' if total >= before else '−'}{usd(abs(total - before))}) in 24h\n"
             f"{table}\n\n"
             f"Biggest move: {esc(move[0])} {pct(move[2])}\n"
             + (f"\nWatchlist, 24h:\n{wtable}\n\n" if wrows else "") +
             f"Whale alerts, 24h: {whales('sell')} · {whales('buy')}"
             + (f"\n\nHolders, 24h:\n{holders}" if holders else ""),
             {"summary": {"t": now, "day": day}}, None)]


# ---------- the weekly report card: how many alerts fired, and what the price did in the 24 hours after ----------
REPORT_KINDS = (("price", 1, "📈 Price jumps"), ("price", -1, "📉 Price drops"), ("whale", -1, "🐋 Whales selling"),
                ("whale", 1, "🐳 Whales buying"), ("team", -1, "👀 Launch wallets"), ("liquidity", -1, "🚨 Liquidity pulls"),
                ("exit", 0, "🚪 Exit liquidity"), ("level", 0, "🎯 Your price levels"))


def track(state: dict, pairs: dict, now: float) -> None:
    """Bring the hourly check's alerts (passed on in the facts file) into this check's log, and note each alert's price
    when it fired (if the hourly check had none) and 24 hours later, from this check's own price readings."""
    log = state.setdefault("log", [])
    ident = lambda x: (x["t"], x.get("kind"), x.get("key"), x.get("side"), x.get("usd"))
    seen = {ident(x) for x in log}
    log += [dict(x) for x in load(FACTS, {}).get("alerts", []) if x.get("kind") and ident(x) not in seen]
    for x in log:
        ps = pairs.get(x.get("key"))
        if ps:
            p = float(ps[0]["priceUsd"])
            if x.get("px") is None and now - x["t"] < 2 * 3600:
                x["px"] = p
            if x.get("px24") is None and 86400 <= now - x["t"] < 86400 + 3 * 3600:
                x["px24"] = p
    log.sort(key=lambda x: x["t"])


def report(state: dict, now: float, force: bool = False) -> list:
    local = now + TZ_HOURS * 3600
    day = time.strftime("%Y-%m-%d", time.gmtime(local))
    if not force and (time.gmtime(local).tm_wday != REPORT_DAY or time.gmtime(local).tm_hour < SUMMARY_HOUR
                      or state.get("sent", {}).get("report", {}).get("day") == day):
        return []
    xs = [x for x in state.get("log", []) if now - x["t"] < 7 * 86400 and x.get("kind")]
    lines, calls = [], []
    for kind, d, label in REPORT_KINDS:
        g = [x for x in xs if x["kind"] == kind and (d == 0 or x.get("dir") == d)]
        if not g:
            continue
        r = [(x["px24"] / x["px"] - 1, x) for x in g if x.get("px") and x.get("px24")]
        line = f"{label}: <b>{len(g)}</b>"
        if d and r:
            moves = sorted(v for v, _ in r)
            line += (f" · 24h later: median {pct(statistics.median(moves))}, "
                     f"the alert's way {sum(1 for v in moves if v * d > 0)} of {len(moves)}")
            calls += [(v * d, v, label, x) for v, x in r]
        lines.append(line)
    when = lambda t: time.strftime("%a %d %b", time.gmtime(t + TZ_HOURS * 3600))
    msg = f"📋 <b>Weekly alert report</b> · {when(now - 6 * 86400)} to {when(now)}\n\n"
    if not xs:
        msg += "No alerts in the last 7 days."
    else:
        held = sum(1 for x in xs if not x.get("watch"))
        msg += (f"{len(xs)} alert{'s' if len(xs) != 1 else ''} ({held} on your holdings, {len(xs) - held} on the watchlist)\n\n"
                + "\n".join(lines))
        if calls:
            calls.sort(key=lambda z: z[0])
            name = lambda z: f"{z[2].split(' ', 1)[1]} {esc(z[3]['coin'])}{' (watchlist)' if z[3].get('watch') else ''}, {when(z[3]['t'])}"
            msg += f"\n\nBest call: {name(calls[-1])}, {pct(calls[-1][1])} in 24h"
            if len(calls) > 1:
                msg += f"\nWorst call: {name(calls[0])}, {pct(calls[0][1])} in 24h"
        msg += ("\n\n<i>The alert's way: the price moved where the alert pointed over the next 24 hours (further up after a "
                "jump; down after a drop, whales selling, launch wallets or a liquidity pull; up after whales buying).</i>")
    return [(msg, {"report": {"t": now, "day": day}}, None)]


# ---------- price levels set on the page (tap a price) ----------
def price_levels() -> list[dict] | None:
    """The page's price alerts; None when the database couldn't be read (so nothing is forgotten)."""
    try:
        items = kv_get("coinboard:levels")
    except Exception as e:
        print(f"warning: could not read the price alerts ({e})")
        return None
    out = []
    for x in items:
        try:
            out.append({"chain": str(x["chain"]), "token": str(x["token"]), "name": str(x.get("symbol") or "?"),
                        "pair": str(x.get("pair") or ""), "price": float(x["price"]), "dir": 1 if x.get("dir", 1) > 0 else -1,
                        "at": int(x.get("at") or 0), "ref": float(x.get("ref") or 0)})
        except (KeyError, TypeError, ValueError):
            pass
    return out


def level_alerts(levels: list[dict] | None, coins: list[dict], watch: list[dict], pairs: dict, state: dict, now: float) -> list:
    """A level alerts once, when a 5-minute reading is at or beyond it; it stays remembered while it is on the page."""
    if levels is None:
        return []
    sent, out = state.setdefault("sent", {}), []
    ids = {f"v:{key(l)}:{l['price']}:{l['at']}" for l in levels}
    for k in [k for k in sent if k.startswith("v:") and k not in ids]:
        del sent[k]                                   # removed on the page
    known = {key(c): c for c in watch + coins}        # a coin both held and watched counts as held
    for l in levels:
        k, ps = f"v:{key(l)}:{l['price']}:{l['at']}", pairs.get(key(l))
        if k in sent or not ps:
            continue
        px = float(ps[0]["priceUsd"])
        if (px - l["price"]) * l["dir"] < 0:
            continue
        c = known.get(key(l), l)                      # (neither held nor watched any more: no Watchlist tag)
        mc, val = ps[0].get("fdv") or ps[0].get("marketCap"), c.get("qty", 0) * px
        out.append((f"{tag(c)}🎯 <b>{esc(c['name'])} {'up' if l['dir'] > 0 else 'down'} to {price(l['price'])}</b> (your price alert)\n\n"
                    f"Price <b>{price(px)}</b>" + (f" · Mcap <b>{usd(mc)}</b>" if mc else "")
                    + (f"\n{pct(px / l['ref'] - 1)} since you set it at {price(l['ref'])} on "
                       f"{time.strftime('%d %b', time.gmtime(l['at'] + TZ_HOURS * 3600))}" if l["ref"] and l["at"] else "")
                    + (f"\nYour holding: {usd(val)}" if val >= 1 else "") + "\n\n"
                    + chart(c, ps[0]["pairAddress"]), {k: {"t": now}}, record("level", c, now, px)))
    return out


def tick(coins: list[dict], state: dict, now: float) -> list:
    try:
        held = {key(c) for c in coins}
        watch = [w for w in watchlist() if key(w) not in held]
    except Exception as e:
        print(f"warning: could not read the watchlist ({e})")
        watch = []
    print(f"watchlist: {len(watch)} coin(s) not also held")
    levels = price_levels()
    print(f"price alerts set: {len(levels) if levels is not None else 'unknown'}")
    extra = {key(l): {**l, "watch": True} for l in levels or [] if key(l) not in held | {key(w) for w in watch}}
    pairs = {}
    for c in coins + watch + list(extra.values()):
        try:
            pairs[key(c)] = dex_pairs(c)
        except Exception as e:
            print(f"warning: no DexScreener answer for {'a watchlist coin' if c.get('watch') else c['name']} ({e})")
    # watchlist coins have no hourly history from the build: keep a reading every 10 minutes for their 12-hour change
    px = state.setdefault("px", {})
    for k in [k for k in px if k not in {key(w) for w in watch}]:
        del px[k]
    for w in watch:
        ps = pairs.get(key(w))
        h = [x for x in px.get(key(w), []) if now - x[0] < 25 * 3600]
        if ps and (not h or now - h[-1][0] >= 600):
            h.append([now, float(ps[0]["priceUsd"])])
        px[key(w)] = h
    track(state, pairs, now)
    return (price_alerts(coins + watch, pairs, state, now) + liquidity_alerts(coins + watch, pairs, state, now)
            + level_alerts(levels, coins, watch, pairs, state, now) + summary(coins, pairs, state, now, watch) + report(state, now))


# ---------- whales: selling or buying a lot within a day (hourly, from the archives just refreshed) ----------
def rpc(chain: str, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC[chain], data=body, headers={"content-type": "application/json", "user-agent": "coinboard"})
    with urllib.request.urlopen(req, timeout=20) as r:          # (Robinhood Chain's RPC refuses Python's default user agent)
        return json.loads(r.read())["result"]


def token_decimals(c: dict) -> int:
    e = load(ROOT / "data/tokens/tokens.json", {}).get(c["token"].lower()) if c["chain"] == "robinhood" else None
    if e and e.get("decimals") is not None:
        return int(e["decimals"])
    return int(rpc(c["chain"], "eth_call", [{"to": c["token"], "data": "0x313ce567"}, "latest"]), 16)


def watch_archived(held: list[dict]) -> list[dict]:
    """The watchlist's coins on Robinhood Chain and Arc (the chains the hourly update archives) that aren't held."""
    keys = {key(c) for c in held}
    out = []
    for w in watchlist():
        if w["chain"] in RPC and key(w) not in keys:
            try:
                out.append({**w, "dec": token_decimals(w)})
            except Exception:
                pass                                # no decimals, no amounts: skipped this hour
    return out


def hourly(coins: list[dict], state: dict, now: float) -> list:
    import duckdb
    import dashboard_analytics as an           # the board's own whale threshold and list of pools / protocol addresses
    out, con, sent, team = [], duckdb.connect(), state.get("sent", {}), load(TEAMS, {})
    try:
        watch = watch_archived(coins)
    except Exception as e:
        print(f"warning: could not read the watchlist ({e})")
        watch = []
    for c in coins + watch:
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
        # wallets whose sales minus buys (or buys minus sales) over the last 24 hours are big enough to matter
        cands = con.execute(f"""select lower(trader), sum(case when side = 'sell' then tokens else -tokens end) q,
                                       sum(case when side = 'sell' then usd else -usd end) u, max(ts) last_trade
                                  from {L} where ts > {t0} group by 1
                                 having (q >= {WHALE_SUPPLY * sup} and u >= {WHALE_USD})
                                     or (q <= {-WHALE_BUY_SUPPLY * sup} and u <= {-WHALE_USD})""").fetchall()
        whale = WHALE_SIZE * cfg["th"][0] / 100 * sup
        b0 = con.execute(f"select min(block) from {L} where ts > {t0}").fetchone()[0]
        px_now = (con.execute(f"select price_usd from {L} where usd >= 5 order by block desc, log_index desc limit 1").fetchone() or [None])[0]
        try:
            out += team_alerts(c, cfg, d, con, excl, sup, b0, team, sent, now)
        except Exception as e:
            print(f"warning: no team check for {'a watchlist coin' if c.get('watch') else c['name']} ({e})")
        for addr, q, u, last_trade in cands:
            side = "sell" if q > 0 else "buy"
            k = f"w:{key(c)}:{addr}:{side}"
            if addr in excl or now - sent.get(k, {}).get("t", -1e18) < WHALE_QUIET:
                continue
            # its holding over the window: the balance when the window opened, then every transfer since (peak = most held)
            start = float(con.execute(f"""select coalesce(sum(case when lower(dst) = '{addr}' then cast(value as hugeint) else 0 end)
                                                    - sum(case when lower(src) = '{addr}' then cast(value as hugeint) else 0 end), 0) / 1e{dec}
                                            from {T} where block < {b0} and (lower(src) = '{addr}' or lower(dst) = '{addr}')""").fetchone()[0])
            bal = peak = start
            for src, v in con.execute(f"""select lower(src), cast(value as hugeint) / 1e{dec} from {T}
                                           where block >= {b0} and (lower(src) = '{addr}' or lower(dst) = '{addr}') order by block, log_index""").fetchall():
                bal += -float(v) if src == addr else float(v)
                peak = max(peak, bal)
            if side == "sell" and (peak < whale or q < WHALE_SOLD * peak):
                continue
            out.append((whale_message(c, side, q, u, sup, peak, start, bal, last_trade, addr), {k: {"t": now}},
                        record("whale", c, now, px_now, -1 if side == "sell" else 1, side=side, usd=abs(u))))
    try:
        TEAMS.write_text(json.dumps(team))
    except Exception as e:
        print(f"warning: could not save the team wallets ({e})")
    return out + exit_alerts(coins, state, now)


# ---------- team: the launch wallets selling or moving ----------
def is_wallet(chain: str, addr: str, known: dict) -> bool | None:
    """A plain wallet, not a contract (a wallet delegating to code under EIP-7702 counts as a wallet); None when the
    chain didn't answer (asked again next hour)."""
    if addr not in known:
        try:
            code = rpc(chain, "eth_getCode", [addr, "latest"]) or "0x"
        except Exception:
            return None
        known[addr] = code == "0x" or code.startswith("0xef0100")
    return known[addr]


def team_wallets(c: dict, d: Path, con, excl: set, dec: int, th: float, team: dict) -> dict | None:
    """The coin's team as far as the chain shows: its creator, the wallets (not contracts) that got at least the whale
    threshold in the first 30 seconds of trading, and wallets any of them later sent at least a fifth of that to in a
    plain transfer. Worked out once, then extended each hour; kept in data/coinboard_team.json."""
    T, ch = f"'{d}/transfers.parquet'", c["chain"]
    e = team.get(key(c))
    if e is None:
        rows = con.execute(f"""select block, tx_hash, lower(src), lower(dst), cast(value as hugeint) / 1e{dec} from {T}
                                order by block, log_index limit 20000""").fetchall()
        if not rows or rows[0][2] != "0x" + "0" * 40:
            return None                                  # the archive doesn't start at the coin's creation
        b1 = next((r[0] for r in rows if r[2] in excl and r[2] != rows[0][2] and r[3] not in excl), None)
        if b1 is None:
            return None                                  # no trading yet
        when = lambda b: int(rpc(ch, "eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)
        t1, lo, step = when(b1), b1, 64                  # the last block within 30 seconds of the first buy
        while when(lo + step) <= t1 + TEAM_LAUNCH:
            lo, step = lo + step, step * 2
        hi = lo + step
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if when(mid) <= t1 + TEAM_LAUNCH else (lo, mid)
        got: dict[str, float] = {}
        for b, tx, s, dd, v in rows:
            if b <= lo and dd not in excl:
                got[dd] = got.get(dd, 0) + float(v)
        known: dict[str, bool] = {}
        launch = [a for a, q in got.items() if q >= th and is_wallet(ch, a, known) is not False]
        if any(known.get(a) is None for a in launch):
            return None                                  # the chain didn't answer: try again next hour
        creator = rpc(ch, "eth_getTransactionReceipt", [rows[0][1]])["from"].lower()
        e = team[key(c)] = {"creator": creator, "launch": launch, "linked": [], "block": lo, "wallet": known}
    members = {e["creator"], *e["launch"], *e["linked"]}
    for b, s, dd in con.execute(f"""select block, lower(src), lower(dst) from {T} where block > {e['block']}
                                   and cast(value as hugeint) >= {TEAM_LINK * th} * 1e{dec} order by block, log_index""").fetchall():
        if s in members and dd not in members and dd not in excl:
            w = is_wallet(ch, dd, e["wallet"])
            if w is None:
                e["block"] = b - 1                       # carry on from this block next hour
                break
            if w:
                members.add(dd)
                e["linked"].append(dd)
        e["block"] = b
    return e


def team_alerts(c: dict, cfg: dict, d: Path, con, excl: set, sup: float, b0: int, team: dict, sent: dict, now: float) -> list:
    dec, th = int(cfg["dec"]), cfg["th"][0] / 100 * sup
    e = team_wallets(c, d, con, excl, dec, th, team)
    if not e:
        return []
    members = sorted({e["creator"], *e["launch"], *e["linked"]})
    T, L, ins = f"'{d}/transfers.parquet'", f"'{d}/ledger.parquet'", ",".join(f"'{a}'" for a in members)
    sells = {r[0] for r in con.execute(f"select tx_hash from {L} where side = 'sell' and block >= {b0}").fetchall()}
    px = con.execute(f"select price_usd from {L} where usd >= 5 order by block desc, log_index desc limit 1").fetchone()
    px = float(px[0]) if px else 0.0
    flows: dict[str, list[float]] = {}                   # wallet -> [sold, moved] over the last 24 hours
    for tx, s, dd, v in con.execute(f"""select tx_hash, lower(src), lower(dst), cast(value as hugeint) / 1e{dec} from {T}
                                        where block >= {b0} and lower(src) in ({ins}) and lower(src) != lower(dst)""").fetchall():
        sold = tx in sells or dd in excl or e["wallet"].get(dd) is False
        flows.setdefault(s, [0.0, 0.0])[0 if sold else 1] += float(v)
    fired = [w for w, (so, mv) in flows.items() if so + mv >= TEAM_MOVE * sup and (so + mv) * px >= TEAM_USD
             and now - sent.get(f"t:{key(c)}:{w}", {}).get("t", -1e18) >= WHALE_QUIET]
    if not fired:
        return []
    held = con.execute(f"""select sum(case when lower(dst) in ({ins}) then cast(value as hugeint) else 0 end)
                                - sum(case when lower(src) in ({ins}) then cast(value as hugeint) else 0 end) from {T}
                           where lower(src) in ({ins}) or lower(dst) in ({ins})""").fetchone()[0] or 0
    sold, moved = sum(flows[w][0] for w in fired), sum(flows[w][1] for w in fired)
    top = max(fired, key=lambda w: sum(flows[w]))
    return [(team_message(c, len(fired), e["creator"] in fired, sold, moved, float(held) / 10 ** dec, sup, px, top),
             {f"t:{key(c)}:{w}": {"t": now} for w in fired}, record("team", c, now, px or None, -1))]


def team_message(c: dict, n: int, creator: bool, sold: float, moved: float, held: float, sup: float, px: float, top: str) -> str:
    ex = EXPLORER.get(c["chain"], "")
    what = " and ".join(x for x in (f"sold {sold / sup:.2%} of the supply ({usd(sold * px)})" if sold else "",
                                    f"moved {moved / sup:.2%} ({usd(moved * px)}) to other wallets" if moved else "") if x)
    who = f"{n} of its launch wallets" if n > 1 else "One of its launch wallets"
    return (f"{tag(c)}👀 <b>{esc(c['name'])} launch wallets {'selling' if sold >= moved else 'moving tokens'}: "
            f"{(sold + moved) / sup:.2%} of supply in 24 hours</b>\n\n"
            f"{who}{' (the creator among them)' if creator else ''} {what}.\n"
            f"Launch wallets are the coin's creator, the wallets that bought big in its first 30 seconds of trading (often "
            f"the team or its snipers) and wallets they have passed tokens to. Together they hold {max(held, 0) / sup:.1%} "
            f"of the supply.\n\n" + (f"<a href=\"{ex}/address/{top}\">The biggest one</a> · " if ex else "") + chart(c))


# ---------- exit liquidity: a whole holding no longer sells without moving the price a lot ----------
def exit_alerts(coins: list[dict], state: dict, now: float) -> list:
    ex, out, sent = load(EXITS, {}), [], state.setdefault("sent", {})
    for c in coins:
        x, k = ex.get(key(c)), f"x:{key(c)}"
        if not x or now - x["t"] > 2 * 3600:          # quotes from this hour's build only
            continue
        last = sent.get(k, {}).get("level", 0)
        eased = max((m for m in EXIT_MARKS if x["impact"] >= 0.7 * m), default=0)
        if eased < last:                              # eased off: the mark can alert again
            sent[k] = {"t": now, "level": eased}
            last = eased
        mark = max((m for m in EXIT_MARKS if x["impact"] >= m), default=0)
        if mark > last:
            out.append((exit_message(c, x), {k: {"t": now, "level": mark}}, record("exit", c, now, None)))
    return out


def exit_message(c: dict, x: dict) -> str:
    part = (f"About {x['sell5']:.0%} of it sells before the price moves 5%." if x["sell5"] < 1 else "")
    return (f"🚪 <b>{esc(c['name'])}: selling all of it now would move the price {pct(-x['impact'])}</b>\n\n"
            f"Your whole holding would fetch about {usd(x['usd'])} in one sale, {x['impact']:.0%} under today's price. {part}\n"
            "The coin's liquidity is thin for the size of your holding: a sale would need to be split up.\n\n" + chart(c))


def whale_message(c: dict, side: str, q: float, u: float, sup: float, peak: float, start: float, bal: float,
                  last_trade: float, addr: str) -> str:
    """q and u: tokens and dollars sold (negative: bought) over 24 hours; peak / start / bal: the wallet's holding at its
    highest, when the 24 hours began and now."""
    ex = EXPLORER.get(c["chain"], "")
    wallet = f"<a href=\"{ex}/address/{addr}\">The wallet</a> · " if ex else ""
    when = bkk(last_trade)
    if side == "sell":
        msg = (f"{tag(c)}🐋 <b>Whale selling {esc(c['name'])}: {usd(u)} in 24 hours</b>\n\n"
               f"A wallet that held {peak / sup:.2%} of the supply sold {min(q / peak, 1):.0%} of it "
               f"({q / sup:.2%} of the supply), net of any buys.\nIt now holds {max(bal, 0) / sup:.2%}. Last sale {when}.\n\n")
    else:
        msg = (f"{tag(c)}🐳 <b>Whale buying {esc(c['name'])}: {usd(-u)} in 24 hours</b>\n\n"
               f"A wallet bought {-q / sup:.2%} of the supply, net of any sales.\n"
               f"It now holds {max(bal, 0) / sup:.2%} (it held {max(start, 0) / sup:.2%} a day ago). Last buy {when}.\n\n")
    return msg + wallet + chart(c)


def facts(state: dict, now: float) -> None:
    """For the daily summary (made by the 5-minute check): the last day's whale alerts and each coin's holder count."""
    an = load(ROOT / "dashboard/site/analytics.json", {}).get("coins", {})
    holders = {n: [x["rows"][-1]["holders"], x["rows"][-2]["holders"]] for n, x in an.items() if len(x.get("rows", [])) >= 2}
    log = state.get("log", [])
    FACTS.write_text(json.dumps({"t": now, "whales": [x for x in log if x.get("kind", "whale") == "whale" and now - x["t"] < 86400],
                                 "alerts": [x for x in log if now - x["t"] < REPORT_KEEP], "holders": holders}))


def demo(coins: list[dict], now: float) -> list[str]:
    """One example of every alert, made by the code that makes the real ones. Prices, holdings and the summary are
    today's; the triggers are made up (a forced price move, an earlier liquidity reading, whales' wallets and amounts)."""
    global PRICE_MOVE
    import coinboard_tick
    held = {key(c) for c in coins}
    try:
        watch = [w for w in watchlist() if key(w) not in held]
    except Exception:
        watch = []
    pairs = {}
    for c in coins + watch:
        try:
            pairs[key(c)] = dex_pairs(c)
        except Exception:
            pass
    coins = [c for c in coins if pairs.get(key(c))]
    watch = [w for w in watch if pairs.get(key(w))]
    out, keep = [], PRICE_MOVE
    PRICE_MOVE = 0.0                              # every coin "moved": show one held coin and one watchlist coin
    out += [m for m, _, _ in price_alerts(coins[:1] + watch[:1], pairs, {}, now)]
    PRICE_MOVE = keep
    for c in coins[1:2] + watch[:1]:              # liquidity as if the pools had held 1.6x today's half an hour ago
        ps = pairs[key(c)]
        total, px = sum(p["liquidity"].get("usd") or 0 for p in ps), float(ps[0]["priceUsd"])
        st = {"liq": {key(c): [[now - 1800, total * 1.6, px * 1.05], [now - 300, total * 1.02, px]]}}
        out += [m for m, _, _ in liquidity_alerts([c], pairs, st, now)]
    c, sup, zero = coins[-1], 1e9, "0x" + "0" * 40
    out.append(whale_message(c, "sell", 0.31 * 0.0081 * sup, 12_400, sup, 0.0081 * sup, 0.0081 * sup, 0.69 * 0.0081 * sup, now - 1800, zero))
    out.append(whale_message(c, "buy", -0.0062 * sup, -15_800, sup, 0.0072 * sup, 0.001 * sup, 0.0072 * sup, now - 2700, zero))
    mc = pairs[key(c)][0].get("fdv") or pairs[key(c)][0].get("marketCap") or 1e6
    out.append(team_message(c, 3, False, 0.0121 * sup, 0.0042 * sup, 0.49 * sup, sup, mc / sup, zero))
    p0 = float(pairs[key(coins[0])][0]["priceUsd"])        # a price alert set a day ago 10% below today's price
    lv = [{**coins[0], "price": float(f"{p0 * 0.99:.3g}"), "dir": 1, "at": int(now - 86400), "ref": p0 / 1.1}]
    out += [m for m, _, _ in level_alerts(lv, coins, watch, pairs, {}, now)]
    out.append(exit_message(c, {"impact": 0.14, "usd": 0.86 * c.get("qty", 0) * float(pairs[key(c)][0]["priceUsd"]), "sell5": 0.31}))
    out += [m for m, _, _ in summary(coins, pairs, {}, now, watch, force=True)]
    fake = [("price", 1, 0.12), ("price", -1, -0.06), ("price", -1, 0.03), ("whale", -1, -0.09), ("whale", -1, 0.02),
            ("whale", 1, 0.05), ("team", -1, -0.17), ("exit", 0, 0.0)]
    log = [{**record(k, (coins + watch)[i % len(coins + watch)], now - (6 - i * 0.7) * 86400, 1.0, d), "px24": 1 + r}
           for i, (k, d, r) in enumerate(fake)]
    out += [m for m, _, _ in report({"log": log}, now, force=True)]
    out.append(coinboard_tick.stale_text(2, "Refresh, analyse, build, deploy", "https://github.com/floatology/coinboard/actions"))
    out.append(coinboard_tick.CRON_DOWN)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=["tick", "hourly", "demo"])
    a = ap.parse_args()
    coins = load(HELD, [])
    if a.check == "demo":                        # test messages: sent as real ones look, marked at the end, nothing kept
        msgs = demo(coins, time.time()) if coins else []
        for m in msgs:
            print("test:", "a watchlist alert" if m.startswith(WATCH) else m.split("\n")[0])
            if telegram_notify.configured():
                telegram_notify.send(m + "\n\n<i>🧪 Test message</i>")
        print(f"{len(msgs)} test message(s)" + ("" if coins else ": no holdings list yet"))
        return 0
    path, now = ROOT / f"data/alerts_{a.check}.json", time.time()
    state = load(path, {})
    before = json.dumps(state, sort_keys=True)
    if coins:
        try:
            alerts = tick(coins, state, now) if a.check == "tick" else hourly(coins, state, now)
            print(f"{a.check}: {len(coins)} coins checked, {len(alerts)} alert(s)")
            send(alerts, state)
        except Exception as e:                   # an alert hiccup must never fail the run (or the hourly update)
            print(f"warning: {a.check} alerts failed ({e})")
    else:
        print("no holdings list yet (data/coinboard_held.json comes from the hourly update)")
    state["sent"] = {k: m for k, m in state.get("sent", {}).items() if now - m.get("t", 0) < 7 * 86400 or k.startswith("v:")}
    state["log"] = [x for x in state.get("log", []) if now - x["t"] < REPORT_KEEP]
    if a.check == "hourly":
        try:
            facts(state, now)
        except Exception as e:
            print(f"warning: no facts for the daily summary ({e})")
    changed = json.dumps(state, sort_keys=True) != before
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"changed={int(changed)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
