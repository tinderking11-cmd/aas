import json
import os
import time
from html import escape
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import telebot

from payment_system import generate_activation_key, init_payment_system, list_activation_keys


LICENSE_BOT_TOKEN = os.getenv("LICENSE_BOT_TOKEN", "PUT_LICENSE_BOT_TOKEN_HERE").strip()
ADMIN_ID = int(os.getenv("LICENSE_ADMIN_ID", "6533209472"))
DB_FILE = os.getenv("LICENSE_DB_FILE", "payment_license_master.db")

if not LICENSE_BOT_TOKEN or LICENSE_BOT_TOKEN == "PUT_LICENSE_BOT_TOKEN_HERE":
    raise RuntimeError("Set LICENSE_BOT_TOKEN environment variable or edit payment_license_bot.py")

init_payment_system(DB_FILE)
bot = telebot.TeleBot(LICENSE_BOT_TOKEN, threaded=True)


def admin_only(message):
    return message.from_user and message.from_user.id == ADMIN_ID


def post_client_activation(client_base_url, secret, key):
    base = client_base_url.rstrip("/")
    url = f"{base}/license/redeem"
    body = urlencode({"secret": secret, "key": key}).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "ignore") or "{}")


@bot.message_handler(commands=["start", "panel"])
def start(message):
    if not admin_only(message):
        return
    bot.reply_to(
        message,
        (
            "<b>Payment License Bot</b>\n\n"
            "/genkey 30 - create a 30 day key\n"
            "/keys - list latest keys\n"
            "/activate CLIENT_URL SECRET KEY - activate a client bot\n"
            "/quickactivate CLIENT_URL SECRET - generate 30 day key and activate client"
        ),
        parse_mode="HTML"
    )


@bot.message_handler(commands=["genkey"])
def genkey(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    days = 30
    if len(parts) >= 2 and parts[1].isdigit():
        days = int(parts[1])
    key = generate_activation_key(DB_FILE, days)
    bot.reply_to(
        message,
        f"<b>{days} Day Activation Key</b>\n\n<code>{escape(key)}</code>",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["keys"])
def keys(message):
    if not admin_only(message):
        return
    rows = list_activation_keys(DB_FILE, 20)
    if not rows:
        bot.reply_to(message, "No keys found.")
        return
    lines = ["<b>Latest Keys</b>"]
    for key, days, status, created_at, redeemed_at, redeemed_by in rows:
        created = time.strftime("%m-%d %H:%M", time.localtime(float(created_at or 0)))
        lines.append(f"<code>{escape(key)}</code> | {days}d | {escape(status)} | {escape(created)}")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=["activate"])
def activate(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        bot.reply_to(message, "Usage: /activate http://CLIENT_IP:8787 SECRET KEY")
        return
    _cmd, client_url, secret, key = parts
    try:
        result = post_client_activation(client_url, secret, key)
    except Exception as e:
        bot.reply_to(message, f"Activation failed: <code>{escape(str(e))}</code>", parse_mode="HTML")
        return
    bot.reply_to(message, f"Client response:\n<code>{escape(json.dumps(result, indent=2))}</code>", parse_mode="HTML")


@bot.message_handler(commands=["quickactivate"])
def quickactivate(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Usage: /quickactivate http://CLIENT_IP:8787 SECRET")
        return
    _cmd, client_url, secret = parts
    key = generate_activation_key(DB_FILE, 30)
    try:
        result = post_client_activation(client_url, secret, key)
    except Exception as e:
        bot.reply_to(
            message,
            f"Generated key but activation failed:\n<code>{escape(key)}</code>\n<code>{escape(str(e))}</code>",
            parse_mode="HTML"
        )
        return
    bot.reply_to(
        message,
        f"Generated and sent 30 day key:\n<code>{escape(key)}</code>\n\nClient response:\n<code>{escape(json.dumps(result, indent=2))}</code>",
        parse_mode="HTML"
    )


if __name__ == "__main__":
    print("Payment License Bot started...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
