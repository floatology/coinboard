#!/usr/bin/env python3
"""Send Coin Board messages to the owner's Telegram bot.

The bot token and the chat to write to come from TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (GitHub secrets of the coinboard repo).
Nothing here prints them: the logs of a public repo are public.

    python scripts/telegram_notify.py --test     # a test message plus the bot's profile picture (dashboard/telegram_avatar.png)
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
    a = ap.parse_args()
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k, "").strip()]
    if missing:
        print("missing secret(s): " + ", ".join(missing) + " (repo Settings > Secrets and variables > Actions)")
        return 1
    if not a.test:
        ap.print_help()
        return 0
    try:
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
