#!/usr/bin/env python3
"""Coin Board's Telegram alerts.

    coinboard_alerts.py tick     # every 5 minutes (the alerts workflow): price moves, liquidity pulls, the daily summary
    coinboard_alerts.py hourly   # every hour, after the archive refresh: whales selling or buying

  price      a held coin, or one on the page's watchlist, moving 10% or more over 6, 12 or 24 hours: one message per coin
             with all three changes. A window alerts again only after its own length, or 10 points further the same way,
             or when the move turns. The watchlist is read from the page's database when the GitHub secrets
             KV_REST_API_URL and KV_REST_API_READ_ONLY_TOKEN are set (copied from the Vercel project's Upstash database).
  liquidity  all of a coin's pools together losing 30% or more of their dollar liquidity within an hour, seen on two
             checks in a row (a rug warning); again after 6 hours or a further 30% drop.
  summary    once a day at 08:00 Bangkok time: the portfolio and each coin over 24 hours, whale alerts, holder changes.
  whales     selling: a wallet holding at least 0.5% of supply (NET 0.1%) whose sales minus buys over 24 hours reach a
             quarter of its holding, 0.25% of supply and $10,000. Buying: a wallet whose buys minus sales over 24 hours
             reach 0.5% of supply and $10,000. Moves between wallets don't count. One alert per wallet and side per day.

The holdings come from data/coinboard_held.json (written by the hourly build); data/coinboard_facts.json (the day's
whale alerts and holder counts, for the summary) is written by the hourly check. Both reach the 5-minute check through
a small cache. What was sent is kept in data/alerts_<check>.json; an alert counts as sent only once Telegram has it.
Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; without them it prints what it would send and remembers no alerts.
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
FACTS = ROOT / "data/coinboard_facts.json"

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

SUMMARY_HOUR = 8             # the daily summary at 08:00 ...
TZ_HOURS = 7                 # ... Bangkok time (UTC+7, no daylight saving), the time every alert shows
EXPLORER = {"robinhood": "https://robinhoodchain.blockscout.com", "arc": "https://explorer.arc.io"}


# ---------- formatting (the page's conventions: a real minus sign, K / M, prices to 4 significant digits) ----------
def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{'+' if x >= 0 else '−'}{abs(x) * 100:.1f}%"


def usd(x: float) -> str:
    a = abs(x)
    s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.1f}K" if a >= 1e4 else f"${a:,.0f}"
    return ("−" if x < 0 else "") + s


def price(x: float) -> str:
    return f"${x:,.2f}" if x >= 100 else f"${x:.4g}" if x >= 0.0001 else f"${x:.3g}"


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


def watchlist() -> list[dict]:
    """The coins on the page's watchlist (its Upstash database, read-only), as coin entries; [] without the secrets."""
    url, tok = os.environ.get("KV_REST_API_URL", "").strip(), os.environ.get("KV_REST_API_READ_ONLY_TOKEN", "").strip()
    if not url or not tok:
        return []
    req = urllib.request.Request(url, data=json.dumps(["GET", "coinboard:watch"]).encode(),
                                 headers={"authorization": f"Bearer {tok}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        v = json.loads(r.read()).get("result")
    items = json.loads(v) if v else []
    return [{"name": x.get("symbol") or "?", "chain": str(x["chain"]), "token": str(x["token"]), "pair": str(x.get("pair") or ""),
             "watch": True} for x in items if isinstance(x, dict) and x.get("chain") and x.get("token")][:30]


def send(alerts: list[tuple[str, dict, dict | None]], state: dict) -> None:
    """Send each (message, memory, log entry); keep its memory only once Telegram has it, so a failed send is tried again."""
    for msg, mem, log in alerts:
        print("alert:", "a watchlist coin moved" if "· watchlist" in msg.split("\n")[0] else msg.split("\n")[0])   # public log
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
        out.append((f"{'🟢' if ch > 0 else '🔴'} <b>{esc(c['name'])} {pct(ch)} in {h} hours</b>{' · watchlist' if c.get('watch') else ''}\n\n"
                    + " · ".join(f"{w}h {pct(x)}" for w, x in chs.items()) + "\n"
                    f"Price <b>{price(px)}</b>" + (f" · Mcap <b>{usd(mc)}</b>" if mc else "")
                    + (f" · 1h {pct(float(pc['h1']) / 100)}" if pc.get("h1") is not None else "") + hold
                    + "\n\n" + chart(c, p["pairAddress"]),
                    {f"p:{key(c)}:{w}": {"t": now, "dir": 1 if chs[w] > 0 else -1, "level": chs[w]} for w in fired}, None))
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
        out.append((f"🚨 <b>{esc(c['name'])} liquidity down {1 - total / peak[1]:.0%} in {mins} minutes</b>\n\n"
                    f"Its pools hold {usd(total)}, from {usd(peak[1])} at {bkk(peak[0])}.\n"
                    f"Price in the same time: {pct(px / peak[2] - 1)}.\n\n"
                    "A sudden drop like this can mean liquidity is being pulled (a rug), which makes the coin hard to sell.\n\n"
                    + chart(c, ps[0]["pairAddress"]),
                    {f"l:{key(c)}": {"t": now, "level": total}}, None))
    return out


# ---------- the daily summary ----------
def summary(coins: list[dict], pairs: dict, state: dict, now: float) -> list:
    local = now + TZ_HOURS * 3600
    day = time.strftime("%Y-%m-%d", time.gmtime(local))
    if time.gmtime(local).tm_hour < SUMMARY_HOUR or state.get("sent", {}).get("summary", {}).get("day") == day:
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
    w = max(len(r[0][:10]) for r in rows)
    table = "\n".join(f"{esc(n[:10]).ljust(w)} {usd(v):>8}  {pct(ch):>7}" for n, v, ch in rows)
    facts = load(FACTS, {})
    wl = [x for x in facts.get("whales", []) if now - x["t"] < 86400]
    def whales(side: str) -> str:
        xs = [x for x in wl if x["side"] == side]
        named = ", ".join(esc(x["coin"]) + " " + usd(x["usd"]) for x in xs[:4])
        return f"{len(xs)} {side}{'s' if len(xs) != 1 else ''}" + (f" ({named})" if xs else "")
    hold = facts.get("holders", {})
    holders = " · ".join(f"{esc(n)} {'+' if v[0] >= v[1] else '−'}{abs(v[0] - v[1]):,}" for n, v in hold.items() if len(v) == 2)
    move = max(rows, key=lambda r: abs(r[2] or 0))
    return [(f"☀️ <b>Daily summary</b> · {time.strftime('%a %d %b', time.gmtime(local))}\n\n"
             f"Portfolio <b>{usd(total)}</b>  {pct(total / before - 1 if before else None)} "
             f"({'+' if total >= before else '−'}{usd(abs(total - before))}) in 24h\n"
             f"<pre>{table}</pre>\n"
             f"Biggest move: {esc(move[0])} {pct(move[2])}\n"
             f"Whale alerts, 24h: {whales('sell')} · {whales('buy')}"
             + (f"\nHolders, 24h: {holders}" if holders else ""),
             {"summary": {"t": now, "day": day}}, None)]


def tick(coins: list[dict], state: dict, now: float) -> list:
    try:
        held = {key(c) for c in coins}
        watch = [w for w in watchlist() if key(w) not in held]
    except Exception as e:
        print(f"warning: could not read the watchlist ({e})")
        watch = []
    print(f"watchlist: {len(watch)} coin(s) not also held")
    pairs = {}
    for c in coins + watch:
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
    return price_alerts(coins + watch, pairs, state, now) + liquidity_alerts(coins, pairs, state, now) + summary(coins, pairs, state, now)


# ---------- whales: selling or buying a lot within a day (hourly, from the archives just refreshed) ----------
def hourly(coins: list[dict], state: dict, now: float) -> list:
    import duckdb
    import dashboard_analytics as an           # the board's own whale threshold and list of pools / protocol addresses
    out, con, sent = [], duckdb.connect(), state.get("sent", {})
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
        # wallets whose sales minus buys (or buys minus sales) over the last 24 hours are big enough to matter
        cands = con.execute(f"""select lower(trader), sum(case when side = 'sell' then tokens else -tokens end) q,
                                       sum(case when side = 'sell' then usd else -usd end) u, max(ts) last_trade
                                  from {L} where ts > {t0} group by 1
                                 having (q >= {WHALE_SUPPLY * sup} and u >= {WHALE_USD})
                                     or (q <= {-WHALE_BUY_SUPPLY * sup} and u <= {-WHALE_USD})""").fetchall()
        whale = WHALE_SIZE * cfg["th"][0] / 100 * sup
        b0 = con.execute(f"select min(block) from {L} where ts > {t0}").fetchone()[0]
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
            ex = EXPLORER.get(c["chain"], "")
            wallet = f"<a href=\"{ex}/address/{addr}\">The wallet</a> · " if ex else ""
            when = bkk(last_trade)
            if side == "sell":
                msg = (f"🐋 <b>Whale selling {esc(c['name'])}: {usd(u)} in 24 hours</b>\n\n"
                       f"A wallet that held {peak / sup:.2%} of the supply sold {min(q / peak, 1):.0%} of it "
                       f"({q / sup:.2%} of the supply), net of any buys.\nIt now holds {max(bal, 0) / sup:.2%}. Last sale {when}.\n\n")
            else:
                msg = (f"🐳 <b>Whale buying {esc(c['name'])}: {usd(-u)} in 24 hours</b>\n\n"
                       f"A wallet bought {-q / sup:.2%} of the supply, net of any sales.\n"
                       f"It now holds {max(bal, 0) / sup:.2%} (it held {max(start, 0) / sup:.2%} a day ago). Last buy {when}.\n\n")
            out.append((msg + wallet + chart(c), {k: {"t": now}}, {"t": now, "coin": c["name"], "side": side, "usd": abs(u)}))
    return out


def facts(state: dict, now: float) -> None:
    """For the daily summary (made by the 5-minute check): the last day's whale alerts and each coin's holder count."""
    an = load(ROOT / "dashboard/site/analytics.json", {}).get("coins", {})
    holders = {n: [x["rows"][-1]["holders"], x["rows"][-2]["holders"]] for n, x in an.items() if len(x.get("rows", [])) >= 2}
    FACTS.write_text(json.dumps({"t": now, "whales": [x for x in state.get("log", []) if now - x["t"] < 86400], "holders": holders}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=["tick", "hourly"])
    a = ap.parse_args()
    coins = load(HELD, [])
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
    state["sent"] = {k: m for k, m in state.get("sent", {}).items() if now - m.get("t", 0) < 7 * 86400}
    state["log"] = [x for x in state.get("log", []) if now - x["t"] < 2 * 86400]
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
