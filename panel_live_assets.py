import re
from html import escape

from live_actions import ACTION_CUSTOM_EMOJI_IDS, ACTION_EMOJI_ALTS
from live_countries import COUNTRY_CUSTOM_EMOJI_IDS, COUNTRY_DISPLAY_NAMES
from live_services import SERVICE_CUSTOM_EMOJI_IDS, SERVICE_DISPLAY_NAMES
from live_emoji_alts import ACTION_EMOJI_EXACT_ALTS, COUNTRY_EMOJI_ALTS, SERVICE_EMOJI_ALTS

try:
    from phonenumbers import geocoder as _phone_geocoder
except Exception:
    _phone_geocoder = None


TEXT_PLACEHOLDER = "🔹"
BUTTON_EMOJI_IDS = {
    "buy_proxy": "5440660757194744323",
    "buy_vpn": "5276484837736205333",
    "masked_gap": "5215204871422093648",
    "copy_short": "5215372534060428125",
    "copy_full": "5215538577496090960",
    "empty_service": "5215680783863261658",
}

COUNTRY_CODE_ALIASES = {
    "AE": "united arab emirates",
    "BD": "bangladesh",
    "GB": "united kingdom",
    "IN": "india",
    "SA": "saudi arabia",
    "US": "united states",
    "VN": "vietnam",
    "RS": "serbia",
}

SERVICE_ALIASES = {
    "fb": "facebook",
    "facebook": "facebook",
    "wa": "whatsapp",
    "whatsapp": "whatsapp",
    "tg": "telegram",
    "telegram": "telegram",
    "google": "google chrome",
    "gmail": "google chrome",
    "ig": "instagram",
    "instagram": "instagram",
    "tt": "tiktok",
    "tiktok": "tiktok",
    "twitter": "x twitter",
    "x": "x twitter",
    "paypal": "paypal",
    "apple": "apple",
    "microsoft": "microsoft copilot",
    "netflix": "netflix",
    "spotify": "spotify",
    "imo": "imo",
    "viber": "viber",
    "snapchat": "snapchat",
    "discord": "discord",
    "line": "line",
    "wechat": "wechat",
    "steam": "steam",
    "chatgpt": "chatgpt",
    "openai": "chatgpt",
    "daraz": "daraz",
    "foodpanda": "foodpanda",
}


def normalize_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _emoji_html(emoji_id, placeholder=TEXT_PLACEHOLDER):
    emoji_id = str(emoji_id or "").strip()
    if not emoji_id:
        return ""
    return f'<tg-emoji emoji-id="{emoji_id}">{placeholder}</tg-emoji>'


def normalize_country_name(country_name=None, country_code=None):
    code = str(country_code or "").strip().upper()
    if code in COUNTRY_CODE_ALIASES:
        return COUNTRY_CODE_ALIASES[code]
    if code and _phone_geocoder is not None:
        try:
            display_name = _phone_geocoder._region_display_name(code, "en")
            display_key = normalize_key(display_name)
            if display_key in COUNTRY_CUSTOM_EMOJI_IDS:
                return display_key
        except Exception:
            pass

    name_key = normalize_key(country_name)
    if name_key in COUNTRY_CUSTOM_EMOJI_IDS:
        return name_key

    for key, display_name in COUNTRY_DISPLAY_NAMES.items():
        if normalize_key(display_name) == name_key:
            return key

    return name_key


def country_live_html(country_name=None, country_code=None):
    key = normalize_country_name(country_name, country_code)
    return _emoji_html(COUNTRY_CUSTOM_EMOJI_IDS.get(key), COUNTRY_EMOJI_ALTS.get(key, "\U0001F3F3\uFE0F"))


def normalize_service_name(service_name):
    key = normalize_key(service_name)
    return SERVICE_ALIASES.get(key, key)


def service_live_html(service_name):
    key = normalize_service_name(service_name)
    if not key or key not in SERVICE_CUSTOM_EMOJI_IDS:
        return _emoji_html(BUTTON_EMOJI_IDS["empty_service"], "❌")
    return _emoji_html(SERVICE_CUSTOM_EMOJI_IDS.get(key), SERVICE_EMOJI_ALTS.get(key, "\U0001F4F1"))


def action_live_html(action_key):
    key = str(action_key or "").strip().lower()
    return _emoji_html(ACTION_CUSTOM_EMOJI_IDS.get(key), ACTION_EMOJI_EXACT_ALTS.get(key, ACTION_EMOJI_ALTS.get(key, TEXT_PLACEHOLDER)))


def looks_like_phone_number(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return len(digits) >= 5 and digits not in {"0", "00", "000", "0000"}


def looks_like_otp_code(value):
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) >= 4 and digits not in {"0", "00", "000", "0000"}


def button_row():
    return {
        "inline_keyboard": [[
            {
                "text": "Buy Proxy",
                "url": "https://t.me/ProxyHub_BD_BOT",
                "icon_custom_emoji_id": BUTTON_EMOJI_IDS["buy_proxy"],
                "style": "primary",
            },
            {
                "text": "Buy VPN",
                "url": "https://t.me/SOHAG_BD_SHOP_BOT",
                "icon_custom_emoji_id": BUTTON_EMOJI_IDS["buy_vpn"],
                "style": "success",
            },
        ]]
    }


def otp_button_rows(short_text=None, full_text=None):
    rows = []
    if short_text:
        rows.append([{
            "text": "Copy Your Key",
            "copy_text": {"text": str(short_text)},
            "icon_custom_emoji_id": BUTTON_EMOJI_IDS["copy_short"],
            "style": "success",
        }])
    if full_text:
        rows.append([{
            "text": "Full Message",
            "copy_text": {"text": str(full_text)},
            "icon_custom_emoji_id": BUTTON_EMOJI_IDS["copy_full"],
            "style": "primary",
        }])
    return {"inline_keyboard": rows}


def masked_number_html(number):
    text = escape(str(number))
    gap = _emoji_html(BUTTON_EMOJI_IDS["masked_gap"], "❌")
    return re.sub(r"(x+|X+|\*+|[A-Za-z]{2,})", gap, text, count=1)


def build_sms_card(number, service_name, country_name, country_code, otp_code, raw_message=None):
    short_code = str(country_code or "UN").upper()
    service_display = SERVICE_DISPLAY_NAMES.get(
        normalize_service_name(service_name),
        str(service_name or "OTP").strip() or "OTP",
    )
    return (
        f"{country_live_html(country_name, short_code)} "
        f"<b>#{escape(short_code)}</b> "
        f"{service_live_html(service_display)} "
        f"{masked_number_html(number)}"
    )


def build_voice_card(number, country_name, country_code, include_recording=False):
    short_code = str(country_code or "UN").upper()
    lines = [
        f"{action_live_html('voice_otp')} <b>Voice Message Received</b>",
        f"{country_live_html(country_name, short_code)} {masked_number_html(number)}",
    ]
    if include_recording:
        lines.append(f"{action_live_html('waiting_otp')} <b>Status:</b> Recording...")
    return "\n".join(lines)
