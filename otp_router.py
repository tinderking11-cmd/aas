import json
import time
from pathlib import Path

import requests

from panel_live_assets import button_row, otp_button_rows
from sender_bot_config import LIVE_RELAY_BOT_TOKEN, OTP_TARGET_CHAT_ID
from sender_delivery import deliver_text_otp, deliver_voice_otp


HEARTBEAT_FILE = Path(__file__).with_name("number_bot.heartbeat")


def number_bot_is_running(max_age_seconds=15):
    try:
        return time.time() - HEARTBEAT_FILE.stat().st_mtime <= max_age_seconds
    except FileNotFoundError:
        return False


def _post(method, data, files=None):
    response = requests.post(
        f"https://api.telegram.org/bot{LIVE_RELAY_BOT_TOKEN}/{method}",
        data=data,
        files=files,
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("result") if payload.get("ok") else None


def send_group_text_live(text, short_text=None, full_text=None):
    return _post(
        "sendMessage",
        {
            "chat_id": OTP_TARGET_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": json.dumps(otp_button_rows(short_text, full_text)),
        },
    )


def send_group_voice_live(audio_bytes, caption, filename="voice.mp3"):
    return _post(
        "sendVoice",
        {
            "chat_id": OTP_TARGET_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML",
        },
        files={"voice": (filename, audio_bytes, "audio/mpeg")},
    )


def route_text_otp(number, service_name, country_name, country_code, otp_code, group_text, raw_message=None):
    if number_bot_is_running():
        deliver_text_otp(number, service_name, country_name, country_code, otp_code, raw_message)
    return send_group_text_live(group_text, otp_code, raw_message)


def route_voice_otp(number, country_name, country_code, audio_bytes, caption, filename="voice.mp3"):
    if number_bot_is_running():
        deliver_voice_otp(number, country_name, country_code, audio_bytes, caption, filename)
    return send_group_voice_live(audio_bytes, caption, filename)
