import hashlib
import json
import os
import re
import sqlite3
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import telebot
from telebot import types

from payment_gateway_system import (
    ALL_METHODS,
    BinancePaymentVerifier,
    build_client_integration_payload,
    make_client_integration_key,
    make_order_id as gateway_make_order_id,
    make_owner_sms_secret as gateway_owner_sms_secret,
    mask_secret,
    normalize_payment_id,
    parse_official_payment_sms,
)


BOT_TOKEN = os.getenv("CENTRAL_PAYMENT_BOT_TOKEN", "8953945650:AAFPLF4yi1VKXdVnmcuwloZtnVlU-9lMkuI").strip()
ADMIN_ID = int(os.getenv("CENTRAL_PAYMENT_ADMIN_ID", "6533209472"))
DB_FILE = os.getenv("CENTRAL_PAYMENT_DB", "central_payment.db")
HTTP_PORT = int(os.getenv("CENTRAL_PAYMENT_PORT", "8790"))

if not BOT_TOKEN or BOT_TOKEN == "PUT_CENTRAL_PAYMENT_BOT_TOKEN_HERE":
    raise RuntimeError("Set CENTRAL_PAYMENT_BOT_TOKEN environment variable or edit central_payment_bot.py")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
pending_order_steps = {}
pending_admin_steps = {}


def db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db():
    conn = db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS clients (
           client_id TEXT PRIMARY KEY,
           name TEXT,
           bot_link TEXT,
           activate_url TEXT,
           secret TEXT,
           integration_key TEXT,
           owner_id INTEGER,
           license_until REAL DEFAULT 0,
           active INTEGER DEFAULT 1,
           created_at REAL DEFAULT 0
        )"""
    )
    client_columns = [row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()]
    if "owner_id" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN owner_id INTEGER")
    if "license_until" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN license_until REAL DEFAULT 0")
    if "integration_key" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN integration_key TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS methods (
           client_id TEXT,
           method TEXT,
           account TEXT,
           active INTEGER DEFAULT 1,
           PRIMARY KEY (client_id, method)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS plans (
           client_id TEXT PRIMARY KEY,
           bdt_amount REAL DEFAULT 120,
           bdt_days INTEGER DEFAULT 1,
           usdt_amount REAL DEFAULT 1,
           usdt_days INTEGER DEFAULT 15
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS client_features (
           client_id TEXT PRIMARY KEY,
           premium_enabled INTEGER DEFAULT 1,
           balance_enabled INTEGER DEFAULT 1,
           product_enabled INTEGER DEFAULT 0,
           product_name TEXT DEFAULT 'Product'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS binance_settings (
           client_id TEXT PRIMARY KEY,
           api_key TEXT DEFAULT '',
           api_secret TEXT DEFAULT '',
           binance_id TEXT DEFAULT '',
           currency TEXT DEFAULT 'USDT',
           window_minutes INTEGER DEFAULT 15,
           enabled INTEGER DEFAULT 1
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders (
           order_id TEXT PRIMARY KEY,
           client_id TEXT,
           user_id INTEGER,
           method TEXT,
           purpose TEXT DEFAULT 'premium',
           product_name TEXT DEFAULT '',
           payment_id TEXT,
           amount REAL DEFAULT 0,
           days INTEGER DEFAULT 0,
           status TEXT DEFAULT 'pending',
           created_at REAL DEFAULT 0,
           decided_at REAL DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
           setting_key TEXT PRIMARY KEY,
           setting_value TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS central_users (
           user_id INTEGER PRIMARY KEY,
           balance REAL DEFAULT 0,
           sms_secret TEXT,
           created_at REAL DEFAULT 0
        )"""
    )
    central_user_columns = [row[1] for row in conn.execute("PRAGMA table_info(central_users)").fetchall()]
    if "sms_secret" not in central_user_columns:
        conn.execute("ALTER TABLE central_users ADD COLUMN sms_secret TEXT")
    secret = hashlib.sha1(f"{BOT_TOKEN}|{ADMIN_ID}|central-sms".encode()).hexdigest()[:24]
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (setting_key, setting_value) VALUES ('central_sms_secret', ?)",
        (secret,)
    )
    conn.execute("INSERT OR IGNORE INTO app_settings (setting_key, setting_value) VALUES ('client_monthly_price', '1000')")
    conn.execute(
        """INSERT OR IGNORE INTO clients
           (client_id, name, bot_link, activate_url, secret, integration_key, owner_id, license_until, active, created_at)
           VALUES ('central', 'Payment System Balance', '', '', '', ?, ?, 0, 1, ?)""",
        (make_client_integration_key(BOT_TOKEN, "central", ADMIN_ID, 1), ADMIN_ID, time.time())
    )
    for client_id, owner_id, created_at, integration_key in conn.execute(
        "SELECT client_id, owner_id, created_at, integration_key FROM clients WHERE integration_key IS NULL OR integration_key=''"
    ).fetchall():
        conn.execute(
            "UPDATE clients SET integration_key=? WHERE client_id=?",
            (make_client_integration_key(BOT_TOKEN, client_id, owner_id, created_at or time.time()), client_id)
        )
    order_columns = [row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "purpose" not in order_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN purpose TEXT DEFAULT 'premium'")
    if "product_name" not in order_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN product_name TEXT DEFAULT ''")
    conn.commit()
    conn.close()


init_db()


def normalize_id(value):
    return normalize_payment_id(value)


def get_setting(key, default=""):
    conn = db()
    row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def get_float_setting(key, default=0):
    try:
        return float(get_setting(key, str(default)))
    except Exception:
        return float(default)


def track_central_user(user_id):
    user_id = int(user_id)
    sms_secret = make_owner_sms_secret(user_id)
    conn = db()
    conn.execute(
        """INSERT OR IGNORE INTO central_users (user_id, balance, sms_secret, created_at)
           VALUES (?, 0, ?, ?)""",
        (user_id, sms_secret, time.time())
    )
    conn.execute(
        "UPDATE central_users SET sms_secret=? WHERE user_id=? AND (sms_secret IS NULL OR sms_secret='')",
        (sms_secret, user_id)
    )
    conn.commit()
    conn.close()


def make_owner_sms_secret(user_id):
    return gateway_owner_sms_secret(BOT_TOKEN, user_id)


def get_owner_sms_secret(user_id):
    track_central_user(user_id)
    conn = db()
    row = conn.execute("SELECT sms_secret FROM central_users WHERE user_id=?", (int(user_id),)).fetchone()
    conn.close()
    return row[0] if row and row[0] else make_owner_sms_secret(user_id)


def get_sms_owner_by_secret(secret):
    if not secret:
        return None
    conn = db()
    row = conn.execute("SELECT user_id FROM central_users WHERE sms_secret=?", (secret,)).fetchone()
    conn.close()
    return int(row[0]) if row else None


def get_central_balance(user_id):
    conn = db()
    row = conn.execute("SELECT COALESCE(balance, 0) FROM central_users WHERE user_id=?", (int(user_id),)).fetchone()
    conn.close()
    return float(row[0] or 0) if row else 0.0


def add_central_balance(user_id, amount):
    amount = float(amount or 0)
    track_central_user(user_id)
    conn = db()
    conn.execute(
        "UPDATE central_users SET balance=COALESCE(balance, 0)+? WHERE user_id=?",
        (amount, int(user_id))
    )
    conn.commit()
    conn.close()


def deduct_central_balance(user_id, amount):
    amount = float(amount or 0)
    balance = get_central_balance(user_id)
    if balance + 0.001 < amount:
        return False
    conn = db()
    conn.execute(
        "UPDATE central_users SET balance=COALESCE(balance, 0)-? WHERE user_id=?",
        (amount, int(user_id))
    )
    conn.commit()
    conn.close()
    return True


def owner_can_manage(client_id, user_id):
    if int(user_id) == ADMIN_ID:
        return True
    conn = db()
    row = conn.execute("SELECT owner_id, license_until FROM clients WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    return bool(
        row
        and int(row[0] or 0) == int(user_id)
        and float(row[1] or 0) > time.time()
    )


def client_is_licensed(client_id):
    if client_id == "central":
        return True
    conn = db()
    row = conn.execute("SELECT license_until FROM clients WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    return bool(row and float(row[0] or 0) > time.time())


def send_admin_panel(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Payment", callback_data="admin|payment"),
        types.InlineKeyboardButton("Clients", callback_data="admin|clients"),
    )
    markup.add(
        types.InlineKeyboardButton("SMS App", callback_data="admin|smsapp"),
        types.InlineKeyboardButton("Pending", callback_data="admin|pending"),
    )
    markup.add(types.InlineKeyboardButton("Set Binance API", callback_data="admin|setbinance"))
    markup.add(types.InlineKeyboardButton("Set Client Price", callback_data="admin|setprice"))
    bot.send_message(chat_id, "<b>Central Payment Admin</b>", parse_mode="HTML", reply_markup=markup)


def send_payment_panel(chat_id, edit_message=None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Add Bot", callback_data="admin|addbot"),
        types.InlineKeyboardButton("Set Method", callback_data="admin|setmethod"),
    )
    markup.add(
        types.InlineKeyboardButton("Set Plan", callback_data="admin|setplan"),
        types.InlineKeyboardButton("Clients", callback_data="admin|clients"),
    )
    markup.add(types.InlineKeyboardButton("Set Features", callback_data="admin|features"))
    markup.add(
        types.InlineKeyboardButton("SMS App", callback_data="admin|smsapp"),
        types.InlineKeyboardButton("Pending", callback_data="admin|pending"),
    )
    markup.add(types.InlineKeyboardButton("Set Client Price", callback_data="admin|setprice"))
    text = (
        "<b>Payment System</b>\n\n"
        "Add each client bot here. Each bot can have separate Bkash/Nagad/Rocket/Binance accounts and rates."
    )
    if edit_message:
        bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def make_order_id(client_id, user_id, method, payment_id):
    return gateway_make_order_id(client_id, user_id, method, payment_id)


def get_client(client_id):
    conn = db()
    row = conn.execute(
        "SELECT client_id, name, bot_link, activate_url, secret, active FROM clients WHERE client_id=? AND active=1",
        (client_id,)
    ).fetchone()
    conn.close()
    return row


def get_client_by_integration_key(integration_key):
    conn = db()
    row = conn.execute(
        """SELECT client_id, name, bot_link, activate_url, secret, active
           FROM clients WHERE integration_key=? AND active=1""",
        (str(integration_key or "").strip(),)
    ).fetchone()
    conn.close()
    return row


def get_client_integration_key(client_id):
    conn = db()
    row = conn.execute(
        "SELECT integration_key, owner_id, created_at FROM clients WHERE client_id=?",
        (client_id,)
    ).fetchone()
    if not row:
        conn.close()
        return ""
    integration_key, owner_id, created_at = row
    if not integration_key:
        integration_key = make_client_integration_key(BOT_TOKEN, client_id, owner_id, created_at or time.time())
        conn.execute("UPDATE clients SET integration_key=? WHERE client_id=?", (integration_key, client_id))
        conn.commit()
    conn.close()
    return integration_key


def get_plan(client_id):
    conn = db()
    row = conn.execute(
        "SELECT bdt_amount, bdt_days, usdt_amount, usdt_days FROM plans WHERE client_id=?",
        (client_id,)
    ).fetchone()
    conn.close()
    return row or (120, 1, 1, 15)


def get_features(client_id):
    conn = db()
    row = conn.execute(
        "SELECT premium_enabled, balance_enabled, product_enabled, product_name FROM client_features WHERE client_id=?",
        (client_id,)
    ).fetchone()
    conn.close()
    return row or (1, 1, 0, "Product")


def get_binance_settings(client_id):
    conn = db()
    row = conn.execute(
        """SELECT api_key, api_secret, binance_id, currency, window_minutes, enabled
           FROM binance_settings WHERE client_id=?""",
        (client_id,)
    ).fetchone()
    conn.close()
    return row or ("", "", "", "USDT", 15, 0)


def save_binance_settings(client_id, api_key, api_secret, binance_id, currency="USDT", window_minutes=15, enabled=1):
    conn = db()
    conn.execute(
        """INSERT INTO binance_settings
           (client_id, api_key, api_secret, binance_id, currency, window_minutes, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(client_id) DO UPDATE SET
             api_key=excluded.api_key,
             api_secret=excluded.api_secret,
             binance_id=excluded.binance_id,
             currency=excluded.currency,
             window_minutes=excluded.window_minutes,
             enabled=excluded.enabled""",
        (client_id, api_key, api_secret, binance_id, str(currency or "USDT").upper(), int(window_minutes or 15), int(enabled))
    )
    conn.commit()
    conn.close()


def get_method_account(client_id, method):
    conn = db()
    row = conn.execute(
        "SELECT account FROM methods WHERE client_id=? AND method=? AND active=1",
        (client_id, method)
    ).fetchone()
    conn.close()
    return row[0] if row else "Not set"


def calc_days(client_id, method, amount):
    bdt_amount, bdt_days, usdt_amount, usdt_days = get_plan(client_id)
    try:
        amount = float(amount or 0)
    except Exception:
        amount = 0
    if method == "binance":
        return int((amount / float(usdt_amount or 1)) * int(usdt_days or 1))
    return int((amount / float(bdt_amount or 1)) * int(bdt_days or 1))


def activate_client_user(client, user_id, days, paid_at=None):
    client_id, name, bot_link, activate_url, secret, active = client
    body = urlencode({
        "secret": secret,
        "user_id": str(user_id),
        "days": str(days),
        "paid_at": str(paid_at or time.time()),
    }).encode("utf-8")
    request = Request(
        activate_url.rstrip("/") + "/client/activate",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "ignore") or "{}")


def add_client_balance(client, user_id, amount):
    client_id, name, bot_link, activate_url, secret, active = client
    body = urlencode({"secret": secret, "user_id": str(user_id), "amount": str(amount)}).encode("utf-8")
    request = Request(activate_url.rstrip("/") + "/client/add_balance", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "ignore") or "{}")


def send_client_product_order(client, user_id, amount, product_name):
    client_id, name, bot_link, activate_url, secret, active = client
    body = urlencode({
        "secret": secret,
        "user_id": str(user_id),
        "amount": str(amount),
        "product": product_name,
    }).encode("utf-8")
    request = Request(activate_url.rstrip("/") + "/client/product_order", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "ignore") or "{}")


def admin_only(message):
    return message.from_user and message.from_user.id == ADMIN_ID


@bot.message_handler(commands=["start"])
def start(message):
    track_central_user(message.from_user.id)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("pay_"):
        payload = parts[1].split("_")
        if len(payload) >= 3:
            client_id = payload[1]
            user_id = int(payload[2])
            show_payment_methods(message.chat.id, client_id, user_id)
            return
        user_id = int(payload[1])
        clients = list_clients()
        if len(clients) == 1:
            show_payment_methods(message.chat.id, clients[0][0], user_id)
            return
        show_client_picker(message.chat.id, user_id)
        return
    if admin_only(message):
        bot.reply_to(
            message,
            "<b>Central Payment Bot</b>\n\n"
            "/addclient CLIENT_ID NAME ACTIVATE_URL SECRET\n"
            "/setmethod CLIENT_ID bkash ACCOUNT\n"
            "/setmethod CLIENT_ID nagad ACCOUNT\n"
            "/setmethod CLIENT_ID rocket ACCOUNT\n"
            "/setmethod CLIENT_ID binance ACCOUNT\n"
            "/setplan CLIENT_ID 120 1 1 15\n"
            "/setbinance CLIENT_ID API_KEY API_SECRET BINANCE_ID USDT 15\n"
            "/clients\n",
            parse_mode="HTML"
        )
        send_admin_panel(message.chat.id)
        return
    send_user_panel(message.chat.id, message.from_user.id)


def send_user_panel(chat_id, user_id):
    balance = get_central_balance(user_id)
    price = get_float_setting("client_monthly_price", 1000)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Add Balance", callback_data="user|topup"),
        types.InlineKeyboardButton("Buy Client System", callback_data="user|buyclient"),
    )
    markup.add(
        types.InlineKeyboardButton("My Clients", callback_data="user|myclients"),
        types.InlineKeyboardButton("App Secret", callback_data="user|appsecret"),
    )
    markup.add(types.InlineKeyboardButton("Help", callback_data="user|help"))
    bot.send_message(
        chat_id,
        (
            "<b>Payment Gateway System</b>\n\n"
            f"Your Balance: <b>{balance:.2f} BDT</b>\n"
            f"Client System Price: <b>{price:.2f} BDT / 30 days</b>\n\n"
            "Buy a client system, connect your bot, then manage payment methods, plans, and features."
        ),
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("user|"))
def user_callback(call):
    track_central_user(call.from_user.id)
    action = call.data.split("|", 1)[1]
    bot.answer_callback_query(call.id)
    if action == "topup":
        show_method_picker(call.message.chat.id, "central", call.from_user.id, "owner_balance")
        return
    if action == "buyclient":
        price = get_float_setting("client_monthly_price", 1000)
        if get_central_balance(call.from_user.id) + 0.001 < price:
            bot.send_message(call.message.chat.id, f"Balance is low. Please add at least {price:.2f} BDT.")
            return
        pending_admin_steps[call.from_user.id] = "owner_buyclient"
        msg = bot.send_message(
            call.message.chat.id,
            "Send your bot info:\n<code>client_id BotName http://YOUR_BOT_SERVER:8787 CLIENT_SECRET</code>\n\nClient secret is shown in your client bot: /admin -> Subscriptions -> Auto Payment.",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, admin_step_handler)
        return
    if action == "myclients":
        send_owner_clients(call.message.chat.id, call.from_user.id)
        return
    if action == "appsecret":
        secret = get_owner_sms_secret(call.from_user.id)
        bot.send_message(
            call.message.chat.id,
            (
                "<b>Your SMS App Login</b>\n\n"
                f"Bridge URL:\n<code>http://SERVER_IP:{HTTP_PORT}/sms</code>\n\n"
                f"App Secret:\n<code>{escape(secret)}</code>\n\n"
                "Use this secret in the Android SMS app. It will only sync payments for your own client systems and your own balance top-ups."
            ),
            parse_mode="HTML"
        )
        return
    if action == "help":
        bot.send_message(
            call.message.chat.id,
            "After buying client system, use:\n/mysetmethod client1 bkash 01XXXXXXXXX\n/mysetplan client1 120 1 1 15\n/myfeatures client1 premium:on balance:on product:off\n/mybinance client1 API_KEY API_SECRET BINANCE_ID USDT 15\n\nYour client dashboard works for 30 days. After expiry, renew the client system to manage it again.",
        )


@bot.message_handler(commands=["panel"])
def panel(message):
    if admin_only(message):
        send_admin_panel(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin|"))
def admin_callback(call):
    if not call.from_user or call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    action = call.data.split("|", 1)[1]
    bot.answer_callback_query(call.id)
    if action == "payment":
        send_payment_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "addbot":
        pending_admin_steps[call.from_user.id] = "addbot"
        msg = bot.send_message(
            call.message.chat.id,
            "Send bot info:\n<code>client_id BotName http://CLIENT_IP:8787 CLIENT_SECRET</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, admin_step_handler)
        return
    if action == "setmethod":
        pending_admin_steps[call.from_user.id] = "setmethod"
        msg = bot.send_message(
            call.message.chat.id,
            "Send method:\n<code>client_id bkash 01XXXXXXXXX</code>\nMethods: bkash, nagad, rocket, binance",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, admin_step_handler)
        return
    if action == "setplan":
        pending_admin_steps[call.from_user.id] = "setplan"
        msg = bot.send_message(
            call.message.chat.id,
            "Send plan:\n<code>client_id 120 1 1 15</code>\nBDT amount/days and USDT amount/days.",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, admin_step_handler)
        return
    if action == "features":
        pending_admin_steps[call.from_user.id] = "features"
        msg = bot.send_message(
            call.message.chat.id,
            "Send features:\n<code>client_id premium:on balance:on product:off product_name</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, admin_step_handler)
        return
    if action == "setbinance":
        pending_admin_steps[call.from_user.id] = "setbinance"
        msg = bot.send_message(
            call.message.chat.id,
            "Send Binance API:\n<code>client_id API_KEY API_SECRET BINANCE_ID USDT 15</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, admin_step_handler)
        return
    if action == "clients":
        send_clients_list(call.message.chat.id)
        return
    if action == "smsapp":
        secret = get_setting("central_sms_secret")
        bot.send_message(
            call.message.chat.id,
            (
                "<b>SMS App Settings</b>\n\n"
                f"Bridge URL:\n<code>http://SERVER_IP:{HTTP_PORT}/sms</code>\n\n"
                f"Secret:\n<code>{escape(secret)}</code>\n\n"
                "Use this in the Android app to sync Bkash/Nagad/Rocket SMS for all client bots."
            ),
            parse_mode="HTML"
        )
        return
    if action == "pending":
        send_pending_orders(call.message.chat.id)
        return
    if action == "setprice":
        pending_admin_steps[call.from_user.id] = "setprice"
        msg = bot.send_message(call.message.chat.id, "Send client monthly price in BDT. Example: <code>1000</code>", parse_mode="HTML")
        bot.register_next_step_handler(msg, admin_step_handler)


def admin_step_handler(message):
    action = pending_admin_steps.pop(message.from_user.id, None)
    if action == "owner_buyclient":
        parts = (message.text or "").split(maxsplit=3)
        if len(parts) < 4:
            bot.reply_to(message, "Invalid. Example: client1 BotName http://CLIENT_IP:8787 SECRET")
            return
        price = get_float_setting("client_monthly_price", 1000)
        if not deduct_central_balance(message.from_user.id, price):
            bot.reply_to(message, "Balance is low. Please add balance first.")
            return
        client_id, name, activate_url, secret = parts
        save_client(client_id, name, activate_url, secret, owner_id=message.from_user.id, license_days=30)
        bot.reply_to(
            message,
            (
                f"Client system activated for 30 days.\n"
                f"Client ID: <code>{escape(client_id)}</code>\n\n"
                "Now set your payment accounts:\n"
                f"<code>/mysetmethod {escape(client_id)} bkash 01XXXXXXXXX</code>"
            ),
            parse_mode="HTML"
        )
        return
    if not admin_only(message):
        return
    if action == "addbot":
        parts = (message.text or "").split(maxsplit=3)
        if len(parts) < 4:
            bot.reply_to(message, "Invalid. Example: client1 BotName http://CLIENT_IP:8787 SECRET")
            return
        client_id, name, activate_url, secret = parts
        save_client(client_id, name, activate_url, secret)
        bot.reply_to(message, "Client bot added.")
        return
    if action == "setmethod":
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "Invalid. Example: client1 bkash 01XXXXXXXXX")
            return
        client_id, method, account = parts
        save_method(client_id, method, account)
        bot.reply_to(message, "Payment method saved.")
        return
    if action == "setplan":
        parts = (message.text or "").split()
        if len(parts) < 5:
            bot.reply_to(message, "Invalid. Example: client1 120 1 1 15")
            return
        client_id, bdt_amount, bdt_days, usdt_amount, usdt_days = parts[:5]
        save_plan(client_id, bdt_amount, bdt_days, usdt_amount, usdt_days)
        bot.reply_to(message, "Plan saved.")
        return
    if action == "features":
        parts = (message.text or "").split(maxsplit=4)
        if len(parts) < 4:
            bot.reply_to(message, "Invalid. Example: client1 premium:on balance:on product:off")
            return
        client_id = parts[0]
        premium_enabled = 1 if "premium:on" in message.text.lower() else 0
        balance_enabled = 1 if "balance:on" in message.text.lower() else 0
        product_enabled = 1 if "product:on" in message.text.lower() else 0
        product_name = parts[4] if len(parts) >= 5 else "Product"
        conn = db()
        conn.execute(
            """INSERT INTO client_features (client_id, premium_enabled, balance_enabled, product_enabled, product_name)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(client_id) DO UPDATE SET
                 premium_enabled=excluded.premium_enabled,
                 balance_enabled=excluded.balance_enabled,
                 product_enabled=excluded.product_enabled,
                 product_name=excluded.product_name""",
            (client_id, premium_enabled, balance_enabled, product_enabled, product_name)
        )
        conn.commit()
        conn.close()
        bot.reply_to(message, "Features saved.")
        return
    if action == "setbinance":
        parts = (message.text or "").split()
        if len(parts) < 6:
            bot.reply_to(message, "Invalid. Example: client1 API_KEY API_SECRET BINANCE_ID USDT 15")
            return
        client_id, api_key, api_secret, binance_id, currency, window_minutes = parts[:6]
        save_binance_settings(client_id, api_key, api_secret, binance_id, currency, window_minutes, 1)
        bot.reply_to(message, "Binance API saved.")
        return
    if action == "setprice":
        try:
            price = float((message.text or "").strip())
        except Exception:
            bot.reply_to(message, "Invalid price.")
            return
        conn = db()
        conn.execute(
            """INSERT INTO app_settings (setting_key, setting_value)
               VALUES ('client_monthly_price', ?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
            (str(price),)
        )
        conn.commit()
        conn.close()
        bot.reply_to(message, f"Client monthly price set to {price:.2f} BDT.")


def list_clients():
    conn = db()
    rows = conn.execute("SELECT client_id, name FROM clients WHERE active=1 ORDER BY client_id").fetchall()
    conn.close()
    return rows


def show_client_picker(chat_id, user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for client_id, name in list_clients():
        markup.add(types.InlineKeyboardButton(name, callback_data=f"client|{client_id}|{user_id}"))
    bot.send_message(chat_id, "Select bot:", reply_markup=markup)


def show_payment_methods(chat_id, client_id, user_id):
    client = get_client(client_id)
    if not client:
        bot.send_message(chat_id, "Client not found.")
        return
    if not client_is_licensed(client_id):
        bot.send_message(chat_id, "This payment system is not active right now. Please contact the bot owner.")
        return
    premium_enabled, balance_enabled, product_enabled, product_name = get_features(client_id)
    purposes = []
    if premium_enabled:
        purposes.append(("Premium", "premium"))
    if balance_enabled:
        purposes.append(("Add Balance", "balance"))
    if product_enabled:
        purposes.append((product_name or "Product", "product"))
    if len(purposes) > 1:
        markup = types.InlineKeyboardMarkup(row_width=1)
        for title, purpose in purposes:
            markup.add(types.InlineKeyboardButton(title, callback_data=f"purpose|{client_id}|{user_id}|{purpose}"))
        bot.send_message(chat_id, f"Payment for {escape(client[1])}\nSelect payment type:", parse_mode="HTML", reply_markup=markup)
        return
    purpose = purposes[0][1] if purposes else "premium"
    show_method_picker(chat_id, client_id, user_id, purpose)


def show_method_picker(chat_id, client_id, user_id, purpose):
    client = get_client(client_id)
    markup = types.InlineKeyboardMarkup(row_width=2)
    for method in ("bkash", "nagad", "rocket", "binance"):
        markup.add(types.InlineKeyboardButton(method.title(), callback_data=f"paymethod|{client_id}|{user_id}|{purpose}|{method}"))
    purpose_text = purpose.replace("_", " ").title()
    bot.send_message(chat_id, f"Payment for {escape(client[1])}\nType: <b>{escape(purpose_text)}</b>", parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("purpose|"))
def purpose_callback(call):
    _prefix, client_id, user_id, purpose = call.data.split("|", 3)
    bot.answer_callback_query(call.id)
    show_method_picker(call.message.chat.id, client_id, int(user_id), purpose)


def save_client(client_id, name, activate_url, secret, owner_id=None, license_days=0):
    license_until = time.time() + int(license_days or 0) * 86400 if int(license_days or 0) > 0 else 0
    integration_key = make_client_integration_key(BOT_TOKEN, client_id, owner_id, time.time())
    conn = db()
    conn.execute(
        """INSERT INTO clients (client_id, name, bot_link, activate_url, secret, integration_key, owner_id, license_until, active, created_at)
           VALUES (?, ?, '', ?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(client_id) DO UPDATE SET
             name=excluded.name,
             activate_url=excluded.activate_url,
             secret=excluded.secret,
             integration_key=COALESCE(NULLIF(integration_key, ''), excluded.integration_key),
             owner_id=COALESCE(excluded.owner_id, owner_id),
             license_until=CASE WHEN excluded.license_until > 0 THEN excluded.license_until ELSE license_until END,
             active=1""",
        (client_id, name, activate_url, secret, integration_key, owner_id, license_until, time.time())
    )
    conn.execute("INSERT OR IGNORE INTO plans (client_id) VALUES (?)", (client_id,))
    conn.commit()
    conn.close()


def save_method(client_id, method, account):
    conn = db()
    conn.execute(
        """INSERT INTO methods (client_id, method, account, active)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(client_id, method) DO UPDATE SET account=excluded.account, active=1""",
        (client_id, method.lower(), account)
    )
    conn.commit()
    conn.close()


def save_plan(client_id, bdt_amount, bdt_days, usdt_amount, usdt_days):
    conn = db()
    conn.execute(
        """INSERT INTO plans (client_id, bdt_amount, bdt_days, usdt_amount, usdt_days)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(client_id) DO UPDATE SET
             bdt_amount=excluded.bdt_amount, bdt_days=excluded.bdt_days,
             usdt_amount=excluded.usdt_amount, usdt_days=excluded.usdt_days""",
        (client_id, float(bdt_amount), int(bdt_days), float(usdt_amount), int(usdt_days))
    )
    conn.commit()
    conn.close()


def send_owner_clients(chat_id, owner_id):
    conn = db()
    rows = conn.execute(
        """SELECT client_id, name, license_until, integration_key FROM clients
           WHERE owner_id=? AND active=1 ORDER BY created_at DESC""",
        (int(owner_id),)
    ).fetchall()
    conn.close()
    if not rows:
        bot.send_message(chat_id, "You have no client system yet.")
        return
    lines = ["<b>My Client Systems</b>"]
    for client_id, name, license_until, integration_key in rows:
        is_active = float(license_until or 0) > time.time()
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(license_until or 0))) if license_until else "Not active"
        status = "Active" if is_active else "Expired"
        if not integration_key:
            integration_key = get_client_integration_key(client_id)
        lines.append(
            f"<code>{escape(client_id)}</code> - {escape(name)}\n"
            f"Status: <b>{status}</b>\n"
            f"Active until: <b>{escape(until)}</b>\n"
            f"Client Key: <code>{escape(integration_key)}</code>\n"
            f"Gateway API: <code>http://SERVER_IP:{HTTP_PORT}</code>"
        )
    bot.send_message(chat_id, "\n\n".join(lines), parse_mode="HTML")


def send_clients_list(chat_id):
    conn = db()
    rows = conn.execute(
        """SELECT c.client_id, c.name, c.activate_url, c.integration_key,
                  COALESCE(p.bdt_amount, 0), COALESCE(p.bdt_days, 0),
                  COALESCE(p.usdt_amount, 0), COALESCE(p.usdt_days, 0),
                  COALESCE(f.premium_enabled, 1), COALESCE(f.balance_enabled, 1),
                  COALESCE(f.product_enabled, 0), COALESCE(f.product_name, 'Product')
           FROM clients c
           LEFT JOIN plans p ON p.client_id=c.client_id
           LEFT JOIN client_features f ON f.client_id=c.client_id
           WHERE c.active=1
           ORDER BY c.client_id"""
    ).fetchall()
    conn.close()
    if not rows:
        bot.send_message(chat_id, "No clients added.")
        return
    lines = ["<b>Client Bots</b>"]
    for client_id, name, activate_url, integration_key, bdt_amount, bdt_days, usdt_amount, usdt_days, premium_on, balance_on, product_on, product_name in rows:
        features = []
        if premium_on:
            features.append("Premium")
        if balance_on:
            features.append("Balance")
        if product_on:
            features.append(f"Product:{product_name}")
        lines.append(
            f"<code>{escape(client_id)}</code> - {escape(name)}\n"
            f"BDT: {bdt_amount}={bdt_days}d | USDT: {usdt_amount}={usdt_days}d\n"
            f"Features: {escape(', '.join(features) or 'None')}\n"
            f"Client Key: <code>{escape(mask_secret(integration_key))}</code>\n"
            f"<code>{escape(activate_url)}</code>"
        )
    bot.send_message(chat_id, "\n\n".join(lines), parse_mode="HTML")


def send_pending_orders(chat_id):
    conn = db()
    rows = conn.execute(
        """SELECT order_id, client_id, user_id, method, purpose, payment_id, amount, days, created_at
           FROM orders WHERE status='pending'
           ORDER BY created_at DESC LIMIT 20"""
    ).fetchall()
    conn.close()
    if not rows:
        bot.send_message(chat_id, "No pending orders.")
        return
    lines = ["<b>Pending Orders</b>"]
    for order_id, client_id, user_id, method, purpose, payment_id, amount, days, created_at in rows:
        created = time.strftime("%m-%d %H:%M", time.localtime(float(created_at or 0)))
        lines.append(
            f"<code>{escape(order_id)}</code> | {escape(client_id)} | <code>{user_id}</code>\n"
            f"{escape(purpose)} / {escape(method)} | <code>{escape(payment_id)}</code> | {amount} | {days}d | {escape(created)}"
        )
    bot.send_message(chat_id, "\n\n".join(lines), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("client|"))
def client_callback(call):
    _prefix, client_id, user_id = call.data.split("|", 2)
    bot.answer_callback_query(call.id)
    show_payment_methods(call.message.chat.id, client_id, int(user_id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("paymethod|"))
def payment_method_callback(call):
    parts = call.data.split("|")
    if len(parts) == 5:
        _prefix, client_id, user_id, purpose, method = parts
    else:
        _prefix, client_id, user_id, method = parts
        purpose = "premium"
    account = get_method_account(client_id, method)
    bdt_amount, bdt_days, usdt_amount, usdt_days = get_plan(client_id)
    if method == "binance":
        rate = f"{usdt_amount} USDT = {usdt_days} days"
        id_label = "Order ID"
    else:
        rate = f"{bdt_amount} BDT = {bdt_days} days"
        id_label = "Transaction ID"
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(f"Submit {id_label}", callback_data=f"submit|{client_id}|{user_id}|{purpose}|{method}"))
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"<b>{method.title()} Payment</b>\n\nAccount: <code>{escape(account)}</code>\nRate: <b>{escape(rate)}</b>",
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("submit|"))
def submit_callback(call):
    parts = call.data.split("|")
    if len(parts) == 5:
        _prefix, client_id, user_id, purpose, method = parts
    else:
        _prefix, client_id, user_id, method = parts
        purpose = "premium"
    pending_order_steps[call.from_user.id] = (client_id, int(user_id), purpose, method)
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Send payment ID and amount. Example: ABC123XYZ 120 or ORDER123 1.5")
    bot.register_next_step_handler(msg, save_order_step)


def create_payment_order(client_id, user_id, purpose, method, payment_id, amount):
    client_id = str(client_id or "").strip()
    purpose = str(purpose or "premium").strip().lower()
    method = str(method or "").strip().lower()
    normalized_payment_id = normalize_id(payment_id)
    if method not in ALL_METHODS:
        return False, "Invalid payment method"
    if purpose not in ("premium", "balance", "product", "owner_balance"):
        return False, "Invalid payment type"
    if purpose == "owner_balance" and client_id != "central":
        return False, "Owner balance top-up is only for the central wallet"
    client = get_client(client_id)
    if not client:
        return False, "Client not found"
    if purpose != "owner_balance" and not client_is_licensed(client_id):
        return False, "This client payment system is not active"
    if len(normalized_payment_id) < 3:
        return False, "Payment ID is invalid"
    try:
        amount = float(amount)
    except Exception:
        return False, "Amount is invalid"
    if amount <= 0:
        return False, "Amount is invalid"
    premium_enabled, balance_enabled, product_enabled, product_name = get_features(client_id)
    if purpose == "premium" and not premium_enabled:
        return False, "Premium payment is OFF"
    if purpose == "balance" and not balance_enabled:
        return False, "Balance payment is OFF"
    if purpose == "product" and not product_enabled:
        return False, "Product payment is OFF"
    conn = db()
    duplicate = conn.execute(
        "SELECT order_id, status FROM orders WHERE method=? AND payment_id=? ORDER BY created_at DESC LIMIT 1",
        (method, normalized_payment_id)
    ).fetchone()
    if duplicate:
        conn.close()
        return False, "This payment ID was already submitted or used"
    days = calc_days(client_id, method, amount) if purpose == "premium" else 0
    if purpose == "premium" and days <= 0:
        conn.close()
        return False, "Amount is too low"
    saved_product_name = product_name if purpose == "product" else ""
    order_id = make_order_id(client_id, user_id, method, normalized_payment_id)
    conn.execute(
        """INSERT INTO orders
           (order_id, client_id, user_id, method, purpose, product_name, payment_id, amount, days, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (order_id, client_id, int(user_id), method, purpose, saved_product_name, normalized_payment_id, amount, days, time.time())
    )
    conn.commit()
    conn.close()
    return True, {
        "order_id": order_id,
        "client_id": client_id,
        "user_id": int(user_id),
        "method": method,
        "purpose": purpose,
        "payment_id": normalized_payment_id,
        "amount": amount,
        "days": days,
    }


def is_payment_id_already_approved(method, payment_id, except_order_id=""):
    conn = db()
    if except_order_id:
        row = conn.execute(
            """SELECT order_id FROM orders
               WHERE method=? AND payment_id=? AND status='approved' AND order_id<>?
               LIMIT 1""",
            (method, normalize_id(payment_id), except_order_id)
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT order_id FROM orders
               WHERE method=? AND payment_id=? AND status='approved'
               LIMIT 1""",
            (method, normalize_id(payment_id))
        ).fetchone()
    conn.close()
    return bool(row)


def save_order_step(message):
    data = pending_order_steps.pop(message.from_user.id, None)
    if not data:
        return
    client_id, user_id, purpose, method = data
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "Send payment ID and amount. Example: ABC123XYZ 120")
        return
    payment_id, amount_text = parts[0], parts[1]
    ok, result = create_payment_order(client_id, user_id, purpose, method, payment_id, amount_text)
    if not ok:
        bot.reply_to(message, str(result))
        return
    order_id = result["order_id"]
    amount = result["amount"]
    days = result["days"]
    detail = f"Days: <b>{days}</b>" if purpose == "premium" else f"Amount: <b>{amount}</b>"
    bot.reply_to(message, f"Payment submitted.\nOrder: <code>{order_id}</code>\nType: <b>{escape(purpose.title())}</b>\n{detail}", parse_mode="HTML")
    bot.send_message(
        ADMIN_ID,
        f"New payment pending\nClient: <code>{client_id}</code>\nUser: <code>{user_id}</code>\nType: <b>{escape(purpose)}</b>\nMethod: <b>{method}</b>\nPayment ID: <code>{escape(payment_id)}</code>\nAmount: <b>{amount}</b>\nDays: <b>{days}</b>",
        parse_mode="HTML"
    )
    if method == "binance":
        auto_ok, auto_result = try_auto_approve_binance_order(order_id)
        if auto_ok:
            bot.reply_to(message, "Binance payment verified automatically.")
        else:
            print(f"Binance auto verify pending for {order_id}: {auto_result}")


@bot.message_handler(commands=["addclient"])
def addclient(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 5:
        bot.reply_to(message, "Usage: /addclient CLIENT_ID NAME ACTIVATE_URL SECRET")
        return
    _cmd, client_id, name, activate_url, secret = parts
    save_client(client_id, name, activate_url, secret)
    bot.reply_to(message, "Client saved.")


@bot.message_handler(commands=["setmethod"])
def setmethod(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        bot.reply_to(message, "Usage: /setmethod CLIENT_ID bkash ACCOUNT")
        return
    _cmd, client_id, method, account = parts
    save_method(client_id, method, account)
    bot.reply_to(message, "Method saved.")


@bot.message_handler(commands=["mysetmethod"])
def mysetmethod(message):
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        bot.reply_to(message, "Usage: /mysetmethod client1 bkash ACCOUNT")
        return
    _cmd, client_id, method, account = parts
    if not owner_can_manage(client_id, message.from_user.id):
        bot.reply_to(message, "You cannot manage this client.")
        return
    save_method(client_id, method, account)
    bot.reply_to(message, "Payment method saved.")


@bot.message_handler(commands=["setplan"])
def setplan(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 6:
        bot.reply_to(message, "Usage: /setplan CLIENT_ID BDT_AMOUNT BDT_DAYS USDT_AMOUNT USDT_DAYS")
        return
    _cmd, client_id, bdt_amount, bdt_days, usdt_amount, usdt_days = parts[:6]
    save_plan(client_id, bdt_amount, bdt_days, usdt_amount, usdt_days)
    bot.reply_to(message, "Plan saved.")


@bot.message_handler(commands=["setbinance"])
def setbinance(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 7:
        bot.reply_to(message, "Usage: /setbinance CLIENT_ID API_KEY API_SECRET BINANCE_ID USDT 15")
        return
    _cmd, client_id, api_key, api_secret, binance_id, currency, window_minutes = parts[:7]
    save_binance_settings(client_id, api_key, api_secret, binance_id, currency, window_minutes, 1)
    bot.reply_to(message, "Binance API saved.")


@bot.message_handler(commands=["mysetplan"])
def mysetplan(message):
    parts = (message.text or "").split()
    if len(parts) < 6:
        bot.reply_to(message, "Usage: /mysetplan client1 120 1 1 15")
        return
    _cmd, client_id, bdt_amount, bdt_days, usdt_amount, usdt_days = parts[:6]
    if not owner_can_manage(client_id, message.from_user.id):
        bot.reply_to(message, "You cannot manage this client.")
        return
    save_plan(client_id, bdt_amount, bdt_days, usdt_amount, usdt_days)
    bot.reply_to(message, "Plan saved.")


@bot.message_handler(commands=["mybinance"])
def mybinance(message):
    parts = (message.text or "").split()
    if len(parts) < 7:
        bot.reply_to(message, "Usage: /mybinance client1 API_KEY API_SECRET BINANCE_ID USDT 15")
        return
    _cmd, client_id, api_key, api_secret, binance_id, currency, window_minutes = parts[:7]
    if not owner_can_manage(client_id, message.from_user.id):
        bot.reply_to(message, "You cannot manage this client.")
        return
    save_binance_settings(client_id, api_key, api_secret, binance_id, currency, window_minutes, 1)
    bot.reply_to(message, "Binance API saved.")


@bot.message_handler(commands=["myfeatures"])
def myfeatures(message):
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 4:
        bot.reply_to(message, "Usage: /myfeatures client1 premium:on balance:on product:off")
        return
    client_id = parts[1]
    if not owner_can_manage(client_id, message.from_user.id):
        bot.reply_to(message, "You cannot manage this client.")
        return
    text = message.text.lower()
    premium_enabled = 1 if "premium:on" in text else 0
    balance_enabled = 1 if "balance:on" in text else 0
    product_enabled = 1 if "product:on" in text else 0
    product_name = parts[4] if len(parts) >= 5 else "Product"
    conn = db()
    conn.execute(
        """INSERT INTO client_features (client_id, premium_enabled, balance_enabled, product_enabled, product_name)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(client_id) DO UPDATE SET
             premium_enabled=excluded.premium_enabled,
             balance_enabled=excluded.balance_enabled,
             product_enabled=excluded.product_enabled,
             product_name=excluded.product_name""",
        (client_id, premium_enabled, balance_enabled, product_enabled, product_name)
    )
    conn.commit()
    conn.close()
    bot.reply_to(message, "Features saved.")


@bot.message_handler(commands=["myclients"])
def myclients(message):
    send_owner_clients(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["clients"])
def clients(message):
    if not admin_only(message):
        return
    send_clients_list(message.chat.id)


@bot.message_handler(commands=["approve"])
def approve_command(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /approve ORDER_ID")
        return
    ok, result = approve_order(parts[1], time.time())
    bot.reply_to(message, f"Approved: {ok}\n<code>{escape(str(result))}</code>", parse_mode="HTML")


@bot.message_handler(commands=["reject"])
def reject_command(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /reject ORDER_ID")
        return
    conn = db()
    conn.execute(
        "UPDATE orders SET status='rejected', decided_at=? WHERE order_id=? AND status='pending'",
        (time.time(), parts[1])
    )
    changed = conn.total_changes
    conn.commit()
    conn.close()
    bot.reply_to(message, "Rejected." if changed else "Order not found or already handled.")


def extract_amount(text):
    patterns = [
        r"(?:tk|bdt|৳)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:tk|bdt|৳)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1).replace(",", ""))
    return 0.0


def extract_txids(text):
    found = set()
    for match in re.finditer(r"(?:trxid|txnid|transaction\s*id|ref\s*id)\s*[:#\-]?\s*([A-Za-z0-9]{5,30})", text, re.I):
        found.add(normalize_id(match.group(1)))
    for token in re.findall(r"\b[A-Za-z0-9]{7,24}\b", text):
        normalized = normalize_id(token)
        if any(c.isdigit() for c in normalized) and any(c.isalpha() for c in normalized):
            found.add(normalized)
    return list(found)


def reject_order(order_id, status="rejected", note=""):
    conn = db()
    row = conn.execute(
        "SELECT client_id, user_id, method, purpose, payment_id, amount FROM orders WHERE order_id=?",
        (order_id,)
    ).fetchone()
    if not row:
        conn.close()
        return False, "Order not found"
    client_id, user_id, method, purpose, payment_id, amount = row
    conn.execute("UPDATE orders SET status=?, decided_at=? WHERE order_id=?", (status, time.time(), order_id))
    conn.commit()
    conn.close()
    try:
        bot.send_message(
            user_id,
            (
                "Payment rejected.\n"
                f"Order: <code>{escape(order_id)}</code>\n"
                f"Reason: <b>{escape(note or status)}</b>\n"
                f"Expected amount: <b>{float(amount or 0):.2f}</b>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass
    try:
        bot.send_message(
            ADMIN_ID,
            (
                "Payment auto rejected\n"
                f"Order: <code>{escape(order_id)}</code>\n"
                f"Client: <code>{escape(client_id)}</code>\n"
                f"User: <code>{user_id}</code>\n"
                f"Type: <b>{escape(purpose or '')}</b> / <b>{escape(method or '')}</b>\n"
                f"Payment ID: <code>{escape(payment_id or '')}</code>\n"
                f"Reason: <b>{escape(note or status)}</b>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass
    return True, status


def approve_order(order_id, paid_at=None):
    conn = db()
    row = conn.execute(
        "SELECT client_id, user_id, days, status, purpose, amount, product_name FROM orders WHERE order_id=?",
        (order_id,)
    ).fetchone()
    if not row:
        conn.close()
        return False, "Order not found"
    client_id, user_id, days, status, purpose, amount, product_name = row
    if status == "approved":
        conn.close()
        return False, "Already approved"
    client = get_client(client_id)
    if purpose == "owner_balance":
        add_central_balance(user_id, amount)
        result = {"ok": True, "balance": get_central_balance(user_id)}
        try:
            bot.send_message(user_id, f"Balance added: <b>{amount:.2f} BDT</b>\nCurrent balance: <b>{get_central_balance(user_id):.2f} BDT</b>", parse_mode="HTML")
        except Exception:
            pass
    elif purpose == "balance":
        result = add_client_balance(client, user_id, amount)
    elif purpose == "product":
        result = send_client_product_order(client, user_id, amount, product_name or "Product")
    else:
        result = activate_client_user(client, user_id, days, paid_at)
    conn.execute("UPDATE orders SET status='approved', decided_at=? WHERE order_id=?", (time.time(), order_id))
    conn.commit()
    conn.close()
    return True, result


def try_auto_approve_binance_order(order_id):
    conn = db()
    row = conn.execute(
        """SELECT client_id, user_id, method, payment_id, amount, status
           FROM orders WHERE order_id=?""",
        (order_id,)
    ).fetchone()
    conn.close()
    if not row:
        return False, "Order not found"
    client_id, _user_id, method, payment_id, expected_amount, status = row
    if status != "pending" or method != "binance":
        return False, "Not a pending Binance order"
    api_key, api_secret, _binance_id, currency, window_minutes, enabled = get_binance_settings(client_id)
    if not enabled or not api_key or not api_secret:
        return False, "Binance API is not set"
    verifier = BinancePaymentVerifier(api_key, api_secret, currency, window_minutes)
    matched, detail, paid_amount = verifier.payment_matches(payment_id)
    if not matched:
        return False, detail
    if is_payment_id_already_approved(method, payment_id, order_id):
        return reject_order(order_id, "duplicate", "This Binance Order ID was already approved")
    if float(paid_amount or 0) + 0.001 < float(expected_amount or 0):
        return reject_order(
            order_id,
            "amount_low",
            f"Received {float(paid_amount or 0):.2f} {currency}, expected {float(expected_amount or 0):.2f} {currency}"
        )
    return approve_order(order_id, time.time())


class SmsWebhook(BaseHTTPRequestHandler):
    def log_message(self, _format, *args):
        return

    def send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def parse_params(self, raw="", parsed=None):
        parsed = parsed or urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        params.update({k: v[-1] for k, v in parse_qs(raw).items()})
        return params

    def handle_api_client_config(self, params):
        client_key = params.get("client_key") or params.get("integration_key") or params.get("key")
        client = get_client_by_integration_key(client_key)
        if not client:
            self.send_json(403, {"ok": False, "message": "bad_client_key"})
            return
        client_id, name, _bot_link, _activate_url, _secret, _active = client
        if not client_is_licensed(client_id):
            self.send_json(403, {"ok": False, "message": "client_expired"})
            return
        methods = {}
        for method in ("bkash", "nagad", "rocket", "binance"):
            methods[method] = get_method_account(client_id, method)
        payload = build_client_integration_payload(
            client_id,
            name,
            f"http://SERVER_IP:{HTTP_PORT}",
            get_client_integration_key(client_id),
            methods,
            get_plan(client_id),
            get_features(client_id),
        )
        api_key, _api_secret, binance_id, currency, window_minutes, enabled = get_binance_settings(client_id)
        payload["binance"] = {
            "enabled": bool(enabled and api_key),
            "binance_id": binance_id,
            "currency": currency,
            "window_minutes": window_minutes,
        }
        self.send_json(200, {"ok": True, "client": payload})

    def handle_api_order_create(self, params):
        client_key = params.get("client_key") or params.get("integration_key") or params.get("key")
        client = get_client_by_integration_key(client_key)
        if not client:
            self.send_json(403, {"ok": False, "message": "bad_client_key"})
            return
        client_id = client[0]
        ok, result = create_payment_order(
            client_id,
            params.get("user_id") or params.get("telegram_user_id") or 0,
            params.get("purpose") or "premium",
            params.get("method") or "",
            params.get("payment_id") or params.get("txid") or params.get("order_id") or "",
            params.get("amount") or 0,
        )
        if not ok:
            self.send_json(400, {"ok": False, "message": result})
            return
        try:
            bot.send_message(
                ADMIN_ID,
                (
                    "New API payment pending\n"
                    f"Client: <code>{escape(client_id)}</code>\n"
                    f"User: <code>{escape(str(result['user_id']))}</code>\n"
                    f"Type: <b>{escape(result['purpose'])}</b>\n"
                    f"Method: <b>{escape(result['method'])}</b>\n"
                    f"Payment ID: <code>{escape(result['payment_id'])}</code>\n"
                    f"Amount: <b>{result['amount']}</b>\n"
                    f"Days: <b>{result['days']}</b>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        auto_verify = None
        if result["method"] == "binance":
            auto_ok, auto_result = try_auto_approve_binance_order(result["order_id"])
            auto_verify = {"ok": auto_ok, "result": auto_result}
        self.send_json(200, {"ok": True, "order": result, "auto_verify": auto_verify})

    def handle_api_order_status(self, params):
        client_key = params.get("client_key") or params.get("integration_key") or params.get("key")
        client = get_client_by_integration_key(client_key)
        if not client:
            self.send_json(403, {"ok": False, "message": "bad_client_key"})
            return
        order_id = str(params.get("order_id") or "").strip()
        conn = db()
        row = conn.execute(
            """SELECT order_id, status, amount, days, purpose, method, payment_id
               FROM orders WHERE order_id=? AND client_id=?""",
            (order_id, client[0])
        ).fetchone()
        conn.close()
        if not row:
            self.send_json(404, {"ok": False, "message": "order_not_found"})
            return
        self.send_json(200, {
            "ok": True,
            "order": {
                "order_id": row[0],
                "status": row[1],
                "amount": row[2],
                "days": row[3],
                "purpose": row[4],
                "method": row[5],
                "payment_id": row[6],
            }
        })

    def route_api(self, parsed, params):
        if parsed.path == "/api/client/config":
            self.handle_api_client_config(params)
            return True
        if parsed.path == "/api/order/create":
            self.handle_api_order_create(params)
            return True
        if parsed.path == "/api/order/status":
            self.handle_api_order_status(params)
            return True
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        params = self.parse_params(parsed=parsed)
        if self.route_api(parsed, params):
            return
        self.send_json(200, {"ok": True, "service": "central_payment_gateway"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        parsed = urlparse(self.path)
        params = self.parse_params(raw, parsed)
        if self.route_api(parsed, params):
            return
        supplied_secret = params.get("secret") or self.headers.get("X-Bridge-Secret", "")
        expected_secret = get_setting("central_sms_secret")
        owner_filter = None
        if expected_secret and supplied_secret == expected_secret:
            owner_filter = None
        else:
            owner_filter = get_sms_owner_by_secret(supplied_secret)
        if expected_secret and supplied_secret != expected_secret and owner_filter is None:
            self.send_json(403, {"ok": False, "message": "bad_secret"})
            return
        sender = params.get("sender") or params.get("from") or params.get("phone") or ""
        text = params.get("text") or params.get("body") or params.get("message") or ""
        sms = parse_official_payment_sms(sender, text)
        if not sms.ok:
            self.send_json(202, {"ok": False, "message": sms.reason})
            return
        amount = sms.amount
        txids = sms.txids
        conn = db()
        if owner_filter:
            rows = conn.execute(
                """SELECT o.order_id, o.payment_id, o.amount, o.method
                   FROM orders o
                   LEFT JOIN clients c ON c.client_id=o.client_id
                   WHERE o.status='pending'
                     AND ((o.client_id='central' AND o.user_id=?) OR c.owner_id=?)""",
                (owner_filter, owner_filter)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT order_id, payment_id, amount, method FROM orders WHERE status='pending'"
            ).fetchall()
        conn.close()
        for order_id, payment_id, expected_amount, order_method in rows:
            if order_method != sms.method:
                continue
            if normalize_id(payment_id) in txids:
                if is_payment_id_already_approved(order_method, payment_id, order_id):
                    ok, result = reject_order(order_id, "duplicate", "This payment ID was already approved")
                    self.send_json(200, {"ok": ok, "result": result, "message": "duplicate"})
                    return
                if amount + 0.001 < float(expected_amount or 0):
                    ok, result = reject_order(
                        order_id,
                        "amount_low",
                        f"Received {amount:.2f}, expected {float(expected_amount or 0):.2f}"
                    )
                    self.send_json(200, {"ok": ok, "result": result, "message": "amount_low"})
                    return
                ok, result = approve_order(order_id, time.time())
                self.send_json(200, {"ok": ok, "result": result})
                return
        self.send_json(202, {"ok": False, "message": "No match"})


def start_http():
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), SmsWebhook)
    print(f"Central payment webhook running on port {HTTP_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    import threading
    threading.Thread(target=start_http, daemon=True).start()
    print("Central Payment Bot started...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
