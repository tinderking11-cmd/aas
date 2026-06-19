import os
import json
import sqlite3
from html import escape
from pathlib import Path

import requests

from otp_matcher import candidate_match_score
from live_actions import ACTION_CUSTOM_EMOJI_IDS, ACTION_EMOJI_ALTS
from live_countries import COUNTRY_CUSTOM_EMOJI_IDS, COUNTRY_DISPLAY_NAMES
from live_services import SERVICE_CUSTOM_EMOJI_IDS, SERVICE_DISPLAY_NAMES
from live_emoji_alts import ACTION_EMOJI_EXACT_ALTS, COUNTRY_EMOJI_ALTS, SERVICE_EMOJI_ALTS
from panel_live_assets import build_sms_card, normalize_country_name, normalize_service_name
from panel_live_assets import otp_button_rows


NUMBER_BOT_TOKEN = os.getenv(
    "NUMBER_BOT_TOKEN",
    "8927209172:AAHzhWLI9jwMneO3g-c3RfuaP92uuIXX_Ws",
).strip()
DB_PATH = Path(__file__).with_name("bot_database_pro.db")


def _emoji_html(emoji_id, fallback):
    emoji_id = str(emoji_id or "").strip()
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _action(key):
    return _emoji_html(ACTION_CUSTOM_EMOJI_IDS.get(key), ACTION_EMOJI_EXACT_ALTS.get(key, ACTION_EMOJI_ALTS.get(key, "\U0001F539")))


def _service(service_name):
    key = normalize_service_name(service_name)
    return _emoji_html(SERVICE_CUSTOM_EMOJI_IDS.get(key), SERVICE_EMOJI_ALTS.get(key, "\U0001F4F1"))


def _country(country_name=None, country_code=None):
    key = normalize_country_name(country_name, country_code)
    return _emoji_html(COUNTRY_CUSTOM_EMOJI_IDS.get(key), COUNTRY_EMOJI_ALTS.get(key, "\U0001F3F3\uFE0F"))


def _display_service(service_name):
    key = normalize_service_name(service_name)
    return SERVICE_DISPLAY_NAMES.get(key, str(service_name or "OTP").strip() or "OTP")


def _display_country(country_name=None, country_code=None):
    key = normalize_country_name(country_name, country_code)
    return COUNTRY_DISPLAY_NAMES.get(key, str(country_name or country_code or "Unknown").strip() or "Unknown")


def _matching_taken_rows(number):
    if not DB_PATH.exists():
        return []
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    if not digits:
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, phone, user_id
           FROM numbers
           WHERE status='taken' AND user_id IS NOT NULL"""
    ).fetchall()
    conn.close()
    matched = []
    for row in rows:
        if candidate_match_score(str(number), str(row["phone"])) > 0 or candidate_match_score(str(row["phone"]), digits) > 0:
            matched.append(row)
    return matched


def _post(method, data, files=None):
    if not NUMBER_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{NUMBER_BOT_TOKEN}/{method}"
    try:
        response = requests.post(url, data=data, files=files, timeout=20)
        if response.ok:
            return response.json().get("result")
    except Exception:
        return None
    return None


def _bump_counter(user_id, voice=False):
    if not DB_PATH.exists():
        return
    column = "total_voice_otps" if voice else "total_text_otps"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f"""INSERT INTO users (user_id, {column})
            VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET {column}={column}+1""",
        (user_id,),
    )
    conn.commit()
    conn.close()


def build_user_text_card(number, service_name, country_name, country_code, otp_code=None):
    return build_sms_card(number, service_name, country_name, country_code, otp_code)


def deliver_text_otp(number, service_name, country_name, country_code, otp_code=None, raw_message=None):
    delivered = []
    for row in _matching_taken_rows(number):
        user_id = row["user_id"]
        result = _post(
            "sendMessage",
            {
                "chat_id": user_id,
                "text": build_user_text_card(number, service_name, country_name, country_code, otp_code),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": json.dumps(otp_button_rows(otp_code, raw_message or otp_code)),
            },
        )
        if result:
            _bump_counter(user_id, voice=False)
            delivered.append(user_id)
    return delivered


def deliver_voice_otp(number, country_name, country_code, audio_bytes, caption, filename="voice.mp3"):
    delivered = []
    for row in _matching_taken_rows(number):
        user_id = row["user_id"]
        result = _post(
            "sendVoice",
            {
                "chat_id": user_id,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"voice": (filename, audio_bytes, "audio/mpeg")},
        )
        if not result:
            result = _post(
                "sendAudio",
                {
                    "chat_id": user_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"audio": (filename, audio_bytes, "audio/mpeg")},
            )
        if not result:
            result = _post(
                "sendDocument",
                {
                    "chat_id": user_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"document": (filename, audio_bytes, "audio/ogg")},
            )
        if result:
            _bump_counter(user_id, voice=True)
            delivered.append(user_id)
    return delivered
