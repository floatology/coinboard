#!/usr/bin/env python3
"""Send Coin Board messages to the owner's Telegram bot.

The bot token and the chat to write to come from TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (GitHub secrets of the coinboard repo).
Nothing here prints them: the logs of a public repo are public.

    python scripts/telegram_notify.py --test     # a test message plus the bot's profile picture (dashboard/telegram_avatar.png)
    python scripts/telegram_notify.py --sample [N ...]   # made-up alerts (all, or numbers 1-4), to show the layout
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AVATAR = ROOT / "dashboard/telegram_avatar.png"
# made-up numbers (not the owner's holdings), only to show the layout of real alerts
SAMPLES = [
    """🟢 <b>WALLET +18.4% in 1 hour</b>  <i>sample</i>

Price <b>$0.0254</b> · Mcap <b>$2.54M</b>
24h +31.2% · 1h volume $84.1K (4.2× usual)
Your holding: $1,240 (+$193 this hour)

<a href="https://dexscreener.com/robinhood/0x0339f5459fc690ac85f1782e15782a151b4a9e1b">Open chart</a>""",
    """🐋 <b>Whale sold FAZE: $8,960</b>  <i>sample</i>

Sold 412,000 FAZE, price impact −6.1%
Wallet <code>0x7a3f…c91e</code> now holds 1.9% of supply (was 4.6%)
Top 10 holders: 61.2% → 58.5%
Price <b>$0.0212</b> · Mcap <b>$2.12M</b>

<a href="https://dexscreener.com/arc/0x394d38f807ee0027a182216f5e67a15ae441fa2e">Open chart</a>""",
    """☀️ <b>Daily summary</b> · Sat 17 Oct  <i>sample</i>

Portfolio <b>$3,412</b>  +4.8% (+$156) in 24h
<pre>WALLET  $1,240  +12.3%
FAZE      $980   −2.1%
AF        $701   +0.4%
NET       $491   +3.0%</pre>
Whales: 2 buys, 1 sell over $2K
Holders: WALLET +34 · FAZE +12 · AF −3 · NET +51""",
    """📊 <b>AF volume spike: $42.6K in 1 hour</b>  <i>sample</i>

5.1× its usual hourly volume
Buys $31.2K · Sells $11.4K (73% buys)
Biggest buy: $6,800 by <code>0x51c2…9a07</code> (new wallet)
Price <b>$0.00184</b> (+9.6% 1h) · Mcap <b>$1.84M</b>

<a href="https://dexscreener.com/arc/0x75d658f8101fbe6dc217fbba7e20a0312af5fa2e">Open chart</a>""",
]
HINTS = {401: "the bot token is wrong (TELEGRAM_BOT_TOKEN): copy it again from @BotFather",
         400: "check TELEGRAM_CHAT_ID, and that you pressed Start in the chat with your bot",
         403: "the bot cannot write to you: open the chat with your bot in Telegram and press Start"}


def _call(method: str, fields: dict, files: dict | None = None) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if files:                                        # multipart, for photos
        b = uuid.uuid4().hex
        body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
        for k, (name, data, ctype) in files.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n"
        body += f"--{b}--\r\n".encode()
        ctype = f"multipart/form-data; boundary={b}"
    else:
        body, ctype = json.dumps(fields).encode(), "application/json"
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body, headers={"content-type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:              # Telegram's description never contains the token
        try:
            desc = json.loads(e.read()).get("description", "")
        except Exception:
            desc = ""
        hint = HINTS.get(e.code, "")
        raise RuntimeError(f"Telegram refused {method}: HTTP {e.code} {desc}" + (f" ({hint})" if hint else "")) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach Telegram ({e.reason})") from None


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def send(text: str) -> None:
    """Send an HTML-formatted message (escape any outside text with html.escape)."""
    _call("sendMessage", {"chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(), "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True})


def send_photo(path: Path, caption: str = "") -> None:
    _call("sendPhoto", {"chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(), "caption": caption, "parse_mode": "HTML"},
          {"photo": (path.name, path.read_bytes(), "image/png")})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="send a test message and the bot's profile picture")
    ap.add_argument("--sample", nargs="*", type=int, metavar="N", help="send made-up alerts: all, or these numbers (1-%d)" % len(SAMPLES))
    a = ap.parse_args()
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k, "").strip()]
    if missing:
        print("missing secret(s): " + ", ".join(missing) + " (repo Settings > Secrets and variables > Actions)")
        return 1
    if not a.test and a.sample is None:
        ap.print_help()
        return 0
    try:
        if a.sample is not None:
            picked = [SAMPLES[n - 1] for n in a.sample if 1 <= n <= len(SAMPLES)] if a.sample else SAMPLES
            for m in picked:
                send(m)
            print(f"{len(picked)} sample alert(s) sent")
            return 0
        me = _call("getMe", {})["result"]
        print(f"bot: @{me.get('username')}")
        send("✅ <b>Coin Board alerts are connected</b>\n\nThis test came from GitHub, with no Claude involved. "
             "Real alerts will arrive here once you choose what they should watch.")
        print("test message sent")
        if AVATAR.exists():
            send_photo(AVATAR, "Profile picture for this bot: open @BotFather, send /setuserpic, choose this bot, "
                               "then send this image (forward it, or save it and pick it from your photos).")
            print("profile picture sent")
    except RuntimeError as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
