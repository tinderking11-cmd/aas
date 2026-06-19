import telebot
import time
import threading
import re
import sqlite3
import hashlib
import hmac
import json
import os
import io
import sys
import tempfile
import openpyxl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from telebot import types

from api_integrations import DEFAULT_AGENT_BASE_URL, DEFAULT_FASTX_API_KEY, DEFAULT_FASTX_BASE_URL, ApiRequestError, OtpApiClient, env_default
from otp_matcher import get_message_search_text, extract_phone_candidates, candidate_match_score
from panel_live_assets import build_sms_card, masked_number_html, otp_button_rows, BUTTON_EMOJI_IDS
from payment_system import (
    generate_activation_key,
    get_license_status,
    init_payment_system,
    is_payment_system_active,
    list_activation_keys,
    redeem_activation_key,
)

APP_RUNTIME_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__)

# =================  LIVE EMOJI DATA FILES =================
# Keep these files in the same folder as this script:
# live_services.py, live_countries.py, live_actions.py
try:
    from live_services import SERVICE_CUSTOM_EMOJI_IDS as _SERVICE_CUSTOM_EMOJI_IDS_FILE, SERVICE_DISPLAY_NAMES as _SERVICE_DISPLAY_NAMES_FILE, SERVICE_OPTIONS as _SERVICE_OPTIONS_FILE
    from live_countries import COUNTRY_CUSTOM_EMOJI_IDS as _COUNTRY_CUSTOM_EMOJI_IDS_FILE, COUNTRY_DISPLAY_NAMES as _COUNTRY_DISPLAY_NAMES_FILE
    from live_actions import ACTION_CUSTOM_EMOJI_IDS as _ACTION_CUSTOM_EMOJI_IDS_FILE, ACTION_EMOJI_ALTS as _ACTION_EMOJI_ALTS_FILE
    from live_emoji_alts import SERVICE_EMOJI_ALTS as _SERVICE_EMOJI_ALTS_FILE, COUNTRY_EMOJI_ALTS as _COUNTRY_EMOJI_ALTS_FILE, ACTION_EMOJI_EXACT_ALTS as _ACTION_EMOJI_EXACT_ALTS_FILE
except Exception as _live_import_error:
    _SERVICE_CUSTOM_EMOJI_IDS_FILE = None
    _SERVICE_DISPLAY_NAMES_FILE = None
    _SERVICE_OPTIONS_FILE = None
    _COUNTRY_CUSTOM_EMOJI_IDS_FILE = None
    _COUNTRY_DISPLAY_NAMES_FILE = None
    _ACTION_CUSTOM_EMOJI_IDS_FILE = None
    _ACTION_EMOJI_ALTS_FILE = None
    _SERVICE_EMOJI_ALTS_FILE = None
    _COUNTRY_EMOJI_ALTS_FILE = None
    _ACTION_EMOJI_EXACT_ALTS_FILE = None
    print(f"Live emoji split files not loaded: {_live_import_error}")

LIVE_ONLY_NO_NORMAL_EMOJI = True
LIVE_TEXT_FALLBACK = "🔹"

# =================  CONFIGURATION =================
def load_bot_token():
    token_file = os.path.join(APP_RUNTIME_DIR, "BOT_TOKEN.txt")
    if os.path.exists(token_file):
        with open(token_file, "r", encoding="utf-8") as token_handle:
            return token_handle.read().strip()
    return os.getenv("BOT_TOKEN", "8927209172:AAHzhWLI9jwMneO3g-c3RfuaP92uuIXX_Ws").strip()


API_TOKEN = load_bot_token()
if not API_TOKEN or API_TOKEN == 'PASTE_YOUR_BOT_TOKEN_HERE':
    raise RuntimeError('BOT_TOKEN is missing. Put your bot token in API_TOKEN or set BOT_TOKEN environment variable.')
bot = telebot.TeleBot(API_TOKEN, threaded=True)


# ================= SAFE TELEGRAM SEND PATCH =================
# Telegram's <tg-emoji> HTML entity must contain a valid emoji placeholder.
# Some old clients/custom emoji IDs can still trigger ENTITY_TEXT_INVALID.
# These wrappers keep the bot running: first try live emoji, then retry by removing
# only the <tg-emoji> tags if Telegram rejects the entity.
CUSTOM_EMOJI_TAG_RE = re.compile(r'<tg-emoji\s+emoji-id="\d+">.*?</tg-emoji>', re.DOTALL)


def strip_custom_emoji_tags(text):
    return CUSTOM_EMOJI_TAG_RE.sub('', str(text or ''))


def is_entity_text_invalid_error(error):
    return 'ENTITY_TEXT_INVALID' in str(error) or 'entity text invalid' in str(error).lower()


try:
    from telebot.apihelper import ApiTelegramException
except Exception:
    ApiTelegramException = Exception

_original_send_message = bot.send_message
_original_reply_to = bot.reply_to
_original_edit_message_text = bot.edit_message_text


def safe_send_message(chat_id, text, *args, **kwargs):
    try:
        return _original_send_message(chat_id, text, *args, **kwargs)
    except ApiTelegramException as e:
        if is_entity_text_invalid_error(e):
            safe_text = strip_custom_emoji_tags(text)
            return _original_send_message(chat_id, safe_text, *args, **kwargs)
        raise


def safe_reply_to(message, text, *args, **kwargs):
    try:
        return _original_reply_to(message, text, *args, **kwargs)
    except ApiTelegramException as e:
        if is_entity_text_invalid_error(e):
            safe_text = strip_custom_emoji_tags(text)
            return _original_reply_to(message, safe_text, *args, **kwargs)
        raise


def safe_edit_message_text(text, *args, **kwargs):
    try:
        return _original_edit_message_text(text, *args, **kwargs)
    except ApiTelegramException as e:
        if is_entity_text_invalid_error(e):
            safe_text = strip_custom_emoji_tags(text)
            return _original_edit_message_text(safe_text, *args, **kwargs)
        raise

bot.send_message = safe_send_message
bot.reply_to = safe_reply_to
bot.edit_message_text = safe_edit_message_text


def verify_bot_token_or_exit():
    try:
        bot_info = bot.get_me()
        print(f"Telegram connected: @{bot_info.username}")
        return True
    except Exception as exc:
        print("")
        print("Telegram bot token is not working.")
        print("Open BOT_TOKEN.txt beside Number_Bot.exe and paste a valid BotFather token.")
        print(f"Telegram error: {exc}")
        return False


def acquire_single_instance_lock():
    global single_instance_lock_handle, global_single_instance_lock_handle
    try:
        import msvcrt

        def lock_file(path, label):
            handle = open(path, "a+", encoding="utf-8")
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                print("")
                print(f"Another {label} copy of this bot is already running.")
                print("Close the old bot window/process first, then start this one again.")
                return None
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            return handle

        global_single_instance_lock_handle = lock_file(GLOBAL_SINGLE_INSTANCE_LOCK_FILE, "same-token")
        if not global_single_instance_lock_handle:
            return False
        single_instance_lock_handle = lock_file(SINGLE_INSTANCE_LOCK_FILE, "local")
        if not single_instance_lock_handle:
            try:
                global_single_instance_lock_handle.close()
            except Exception:
                pass
            global_single_instance_lock_handle = None
            return False
        return True
    except Exception as exc:
        print(f"Single-instance lock warning: {exc}")
        return True


def is_get_updates_conflict_error(error):
    text = str(error or "").lower()
    return "409" in text and "conflict" in text and "getupdates" in text

ADMIN_ID = 6533209472
DEFAULT_TARGETS = [
    ("live_stream_group", "Live Stream Group", "-1003446661141", "https://t.me/BDXTOPOTP", 1, 0),
    ("otp_group", "OTP Group", "-1003861839967", "https://t.me/BDXTOPOTP", 1, 1),
    ("method_group", "Method Group", "@WATools1", "https://t.me/WATools1", 1, 0),
    ("main_channel", "Main Channel", "@SH_5620", "https://t.me/SH_5620", 1, 0),
]

db_lock = threading.RLock()
DB_FILE_NUMBER = os.path.join(APP_RUNTIME_DIR, "bot_database_pro.db")
pending_uploads = {} 
user_cooldowns = {} 
pending_group_broadcasts = {}
pending_delivery_notices = {}
pending_target_updates = {}
pending_manual_broadcasts = set()
pending_prefix_inputs = set()
active_prefix_filters = {}
pending_shop_link_updates = set()
pending_subscription_actions = {}
pending_number_setting_actions = {}
pending_api_setting_actions = {}
pending_api_provider_actions = {}
live_traffic_range_cache = {}
live_traffic_auto_messages = {}
pending_payment_setting_updates = {}
pending_payment_approvals = {}
pending_add_premium_days = {}
pending_binance_api_updates = {}
pending_payment_license_updates = {}
bot_username_cache = None
bot_id_cache = None
bot_admin_check_cache = {}
bot_admin_error_log_cache = {}
HEARTBEAT_FILE = os.path.join(APP_RUNTIME_DIR, "number_bot.heartbeat")
SINGLE_INSTANCE_LOCK_FILE = os.path.join(APP_RUNTIME_DIR, "number_bot.lock")
GLOBAL_SINGLE_INSTANCE_LOCK_FILE = os.path.join(
    tempfile.gettempdir(),
    "bdx_top_number_bot_" + hashlib.sha1(API_TOKEN.encode("utf-8")).hexdigest()[:16] + ".lock",
)
single_instance_lock_handle = None
global_single_instance_lock_handle = None
SUBSCRIPTION_FEATURES_ENABLED = False

NUMBERS_PER_ASSIGNMENT = 5
FREE_NUMBERS_PER_ASSIGNMENT = 2
FREE_CHANGE_NUMBER_COOLDOWN = 60
BDT_PER_USD = 120
BOT_ADMIN_CHECK_CACHE_SECONDS = 300
BOT_ADMIN_ERROR_LOG_SECONDS = 600
AUTO_PAYMENT_BRIDGE_PORT = 8787
API_SYNC_FILENAME_PREFIX = "API"
MAX_EXTRA_API_PROVIDERS = 100
EXTRA_API_PROVIDERS_PAGE_SIZE = 8
OTP_CONTENT_TYPES = [
    'text', 'photo', 'voice', 'audio', 'document', 'video', 'animation', 'video_note'
]

PAYMENT_METHODS = {
    "bkash": {"label": "Bkash", "setting": "payment_bkash_number", "id_label": "Transaction ID"},
    "nagad": {"label": "Nagad", "setting": "payment_nagad_number", "id_label": "Transaction ID"},
    "rocket": {"label": "Rocket", "setting": "payment_rocket_number", "id_label": "Transaction ID"},
    "binance": {"label": "Binance", "setting": "payment_binance_number", "id_label": "Order ID"},
}

country_flags = {}

SERVICE_OPTIONS = [
    "Badoo",
    "Bigo Live",
    "Botim",
    "Bumble",
    "Discord",
    "Facebook",
    "FourSquare",
    "Goodgame Studios",
    "Hinge",
    "Imo",
    "Instagram",
    "KakaoTalk",
    "Kwai",
    "Likee",
    "LINE",
    "Medium",
    "Messenger",
    "Meta",
    "Odnoklassniki (OK)",
    "Pure",
    "QQ",
    "Quora",
    "Reddit",
    "Signal",
    "Skype",
    "Snapchat",
    "Telegram",
    "Threads",
    "TikTok",
    "Tinder",
    "Triller",
    "Tumblr",
    "Twitch",
    "Viber",
    "Vimeo",
    "VK",
    "YouTube Music",
    "WeChat",
    "Weibo",
    "WolframAlpha",
    "WhatsApp",
    "X (Twitter)",
    "Xiaohongshu",
    "YouTube",
    "YouTube Shorts",
    "Zalo",
    "Zoom",
    "Amazon",
    "Apple",
    "Disney+",
    "Hulu",
    "IMDb",
    "Kanopy",
    "Letterboxd",
    "Max (HBO)",
    "Netflix",
    "Paramount+",
    "Plex",
    "Rotten Tomatoes",
    "TED",
    "Apple Music",
    "Apple Podcasts",
    "Shazam",
    "SoundCloud",
    "Spotify",
    "Suno",
    "ArtStation",
    "Behance",
    "Bandlab",
    "DeviantArt",
    "Dribbble",
    "Flickr",
    "FreePik",
    "GitHub",
    "GitLab",
    "Kickstarter",
    "LinkedIn",
    "OnlyFans",
    "Patreon",
    "Pinterest",
    "Upwork",
    "Bitcoin",
    "Ethereum",
    "Toncoin",
    "Tether (USDT)",
    "Mastercard",
    "Visa",
    "PayPal",
    "Airbnb",
    "Evernote",
    "Skrill",
    "Notion",
    "Slack",
    "uTorrent",
    "Wikipedia",
    "Adobe After Effects",
    "Adobe Animate",
    "Adobe Audition",
    "Adobe Photoshop",
    "Adobe Illustrator",
    "Adobe InDesign",
    "Adobe Premiere Pro",
    "DaVinci Resolve",
    "CapCut",
    "Procreate",
    "Spline",
    "ZBrush",
    "Figma",
    "Sketch",
    "3ds Max",
    "Maya",
    "Blender",
    "Cinema 4D",
    "ZBrush (Alternate)",
    "Dune AI",
    "Unity",
    "Unreal Engine",
    "ChatGPT",
    "Microsoft Copilot",
    "Midjourney",
    "Stability AI",
    "Google Chrome",
    "Microsoft Edge",
    "Firefox",
    "Opera",
    "Safari",
    "Google Maps",
    "Google Earth",
    "Yandex Maps",
    "Dropbox",
    "Google Drive",
    "Mega",
    "NordVPN",
    "Ableton Live",
    "Cubase",
    "C++",
    "Java",
    "JavaScript",
    "Python",
    "Rust",
    "Sublime Text",
    "Visual Studio Code",
    "App Store",
    "Google Play Store",
    "Microsoft Store",
    "PlayStation",
    "Xbox",
    "Steam",
    "Daraz",
    "Foodpanda"
]

SERVICE_ICONS = {}

SERVICE_CUSTOM_EMOJI_IDS = {
    "badoo": "5323379047315555501",
    "bigo live": "5334954057192719331",
    "botim": "5335012820935261564",
    "bumble": "5323764984486837459",
    "discord": "5325612636467903082",
    "facebook": "5323261730283863478",
    "foursquare": "5330081305126255700",
    "goodgame studios": "5334750591707005005",
    "hinge": "5330345557284109044",
    "imo": "5334595899869905653",
    "instagram": "5319160079465857105",
    "kakaotalk": "5334933574493683027",
    "kwai": "5325726234057915652",
    "likee": "5321403907820240828",
    "line": "5323608076446613036",
    "medium": "5325647210954637197",
    "messenger": "5323687726615119535",
    "meta": "5321447183910716259",
    "odnoklassniki ok": "5325865356638569272",
    "pure": "5325742125436910868",
    "qq": "5328064671951896068",
    "quora": "5327959866159938948",
    "reddit": "5330321861949539755",
    "signal": "5328050550099427291",
    "skype": "5328175271654736902",
    "snapchat": "5330248916224983855",
    "telegram": "5330237710655306682",
    "threads": "5334592721594105691",
    "tiktok": "5327982530702359565",
    "tinder": "5328029650788563621",
    "triller": "5334792209940102096",
    "tumblr": "5328242556612395440",
    "twitch": "5334678011054669335",
    "viber": "5332449498553663205",
    "vimeo": "5334764984142412896",
    "vk": "5334853932915114338",
    "youtube music": "5334807822146225472",
    "wechat": "5332524123610430820",
    "weibo": "5332823323917173335",
    "wolframalpha": "5334663339446387801",
    "whatsapp": "5334998226636390258",
    "x twitter": "5330337435500951363",
    "xiaohongshu": "5334707727933390944",
    "youtube": "5334681713316479679",
    "youtube shorts": "5334942061349059951",
    "zalo": "5321533581472842536",
    "zoom": "5334932883003949665",
    "amazon": "5346056560537779652",
    "apple": "5334955749409834455",
    "disney": "5332394707655869572",
    "hulu": "5346024142124633117",
    "imdb": "5346242859039209592",
    "kanopy": "5346309375197725525",
    "letterboxd": "5357184657592962696",
    "max hbo": "5346319945112240722",
    "netflix": "5318911503938634641",
    "paramount": "5346134750417403743",
    "plex": "5345799562579688546",
    "rotten tomatoes": "5346242644290846992",
    "ted": "5345937224871461019",
    "apple music": "5346251367369425932",
    "apple podcasts": "5345776794958053103",
    "shazam": "5346259862814734771",
    "soundcloud": "5345844509412444249",
    "spotify": "5346074681004801565",
    "suno": "5346296430166293639",
    "artstation": "5345967461441223890",
    "behance": "5346017678198848586",
    "bandlab": "5346188613602263703",
    "deviantart": "5345845252441784353",
    "dribbble": "5346008706012169915",
    "flickr": "5346066456142429527",
    "freepik": "5346172593374248407",
    "github": "5346181118884331907",
    "gitlab": "5346308584923740680",
    "kickstarter": "5346284438617604231",
    "linkedin": "5346024520081751155",
    "onlyfans": "5346213374088723754",
    "patreon": "5345833716159627218",
    "pinterest": "5346103513120258857",
    "upwork": "5345818383126384016",
    "bitcoin": "5359584650958226302",
    "ethereum": "5359321266383766546",
    "toncoin": "5359320566304096699",
    "tether usdt": "5359437015752401733",
    "mastercard": "5364036341610858181",
    "visa": "5364075889669718872",
    "paypal": "5364111181415996352",
    "airbnb": "5366068097365066701",
    "evernote": "5363831115188553043",
    "skrill": "5363810946022131056",
    "notion": "5364199932620194408",
    "slack": "5363899233369868662",
    "utorrent": "5359772714691216710",
    "wikipedia": "5359472852959512416",
    "adobe after effects": "5357394595594388140",
    "adobe animate": "5363989350373671453",
    "adobe audition": "5364101182732127639",
    "adobe photoshop": "5359480394922082925",
    "adobe illustrator": "5359320531944358335",
    "adobe indesign": "5363858422590619939",
    "adobe premiere pro": "5363973751052452405",
    "davinci resolve": "5364121867294621703",
    "capcut": "5364339557712020484",
    "procreate": "5364314346253991843",
    "spline": "5363892211098339885",
    "zbrush": "5364343191254352560",
    "figma": "5357286671656176924",
    "sketch": "5359602676935968589",
    "3ds max": "5363977152666550281",
    "maya": "5364182988974211543",
    "blender": "5364290083983736876",
    "cinema 4d": "5364105576483668906",
    "zbrush alternate": "5364006925379846491",
    "dune ai": "5363986369666369188",
    "unity": "5364226346669065610",
    "unreal engine": "5363863671040657237",
    "chatgpt": "5359726582447487916",
    "microsoft copilot": "5372937764411031477",
    "midjourney": "5359618237602480327",
    "stability ai": "5371089601328857708",
    "google chrome": "5359758030198031389",
    "microsoft edge": "5359503252738029857",
    "firefox": "5362034259785694259",
    "opera": "5361963895336485277",
    "safari": "5361575381184823162",
    "google maps": "5370988368949690738",
    "google earth": "5373262983629650845",
    "yandex maps": "5373193993569977969",
    "dropbox": "5372994299065550645",
    "google drive": "5372878055775683161",
    "mega": "5373246052868571826",
    "nordvpn": "5373025510592888616",
    "ableton live": "5373145666597961572",
    "cubase": "5373054312643575332",
    "c": "5372917956021862036",
    "java": "5373232592441065346",
    "javascript": "5370577035636786019",
    "python": "5372878077250519677",
    "rust": "5373229590258925215",
    "sublime text": "5373054600406382936",
    "visual studio code": "5370852523429085514",
    "app store": "5370722600668382252",
    "google play store": "5373130604147654226",
    "microsoft store": "5370857634440170316",
    "playstation": "5373306783706137993",
    "xbox": "5373019729566908647",
    "steam": "5373144051690258848",
    "daraz": "5373265917092316632",
    "foodpanda": "5373261557700509032",
    "twitter": "5330337435500951363",
    "x": "5330337435500951363",
    "twitter x": "5330337435500951363",
    "ok": "5325865356638569272",
    "odnoklassniki": "5325865356638569272",
    "hbo": "5346319945112240722",
    "max": "5346319945112240722",
    "hbo max": "5346319945112240722",
    "vscode": "5370852523429085514",
    "google play": "5373130604147654226",
    "play store": "5373130604147654226",
    "copilot": "5372937764411031477",
    "openai": "5359726582447487916",
    "gpt": "5359726582447487916",
    "c plus plus": "5372917956021862036",
    "cpp": "5372917956021862036",
    "js": "5370577035636786019"
}

SERVICE_DISPLAY_NAMES = {
    "badoo": "Badoo",
    "bigo live": "Bigo Live",
    "botim": "Botim",
    "bumble": "Bumble",
    "discord": "Discord",
    "facebook": "Facebook",
    "foursquare": "FourSquare",
    "goodgame studios": "Goodgame Studios",
    "hinge": "Hinge",
    "imo": "Imo",
    "instagram": "Instagram",
    "kakaotalk": "KakaoTalk",
    "kwai": "Kwai",
    "likee": "Likee",
    "line": "LINE",
    "medium": "Medium",
    "messenger": "Messenger",
    "meta": "Meta",
    "odnoklassniki ok": "Odnoklassniki (OK)",
    "pure": "Pure",
    "qq": "QQ",
    "quora": "Quora",
    "reddit": "Reddit",
    "signal": "Signal",
    "skype": "Skype",
    "snapchat": "Snapchat",
    "telegram": "Telegram",
    "threads": "Threads",
    "tiktok": "TikTok",
    "tinder": "Tinder",
    "triller": "Triller",
    "tumblr": "Tumblr",
    "twitch": "Twitch",
    "viber": "Viber",
    "vimeo": "Vimeo",
    "vk": "VK",
    "youtube music": "YouTube Music",
    "wechat": "WeChat",
    "weibo": "Weibo",
    "wolframalpha": "WolframAlpha",
    "whatsapp": "WhatsApp",
    "x twitter": "X (Twitter)",
    "xiaohongshu": "Xiaohongshu",
    "youtube": "YouTube",
    "youtube shorts": "YouTube Shorts",
    "zalo": "Zalo",
    "zoom": "Zoom",
    "amazon": "Amazon",
    "apple": "Apple",
    "disney": "Disney+",
    "hulu": "Hulu",
    "imdb": "IMDb",
    "kanopy": "Kanopy",
    "letterboxd": "Letterboxd",
    "max hbo": "Max (HBO)",
    "netflix": "Netflix",
    "paramount": "Paramount+",
    "plex": "Plex",
    "rotten tomatoes": "Rotten Tomatoes",
    "ted": "TED",
    "apple music": "Apple Music",
    "apple podcasts": "Apple Podcasts",
    "shazam": "Shazam",
    "soundcloud": "SoundCloud",
    "spotify": "Spotify",
    "suno": "Suno",
    "artstation": "ArtStation",
    "behance": "Behance",
    "bandlab": "Bandlab",
    "deviantart": "DeviantArt",
    "dribbble": "Dribbble",
    "flickr": "Flickr",
    "freepik": "FreePik",
    "github": "GitHub",
    "gitlab": "GitLab",
    "kickstarter": "Kickstarter",
    "linkedin": "LinkedIn",
    "onlyfans": "OnlyFans",
    "patreon": "Patreon",
    "pinterest": "Pinterest",
    "upwork": "Upwork",
    "bitcoin": "Bitcoin",
    "ethereum": "Ethereum",
    "toncoin": "Toncoin",
    "tether usdt": "Tether (USDT)",
    "mastercard": "Mastercard",
    "visa": "Visa",
    "paypal": "PayPal",
    "airbnb": "Airbnb",
    "evernote": "Evernote",
    "skrill": "Skrill",
    "notion": "Notion",
    "slack": "Slack",
    "utorrent": "uTorrent",
    "wikipedia": "Wikipedia",
    "adobe after effects": "Adobe After Effects",
    "adobe animate": "Adobe Animate",
    "adobe audition": "Adobe Audition",
    "adobe photoshop": "Adobe Photoshop",
    "adobe illustrator": "Adobe Illustrator",
    "adobe indesign": "Adobe InDesign",
    "adobe premiere pro": "Adobe Premiere Pro",
    "davinci resolve": "DaVinci Resolve",
    "capcut": "CapCut",
    "procreate": "Procreate",
    "spline": "Spline",
    "zbrush": "ZBrush",
    "figma": "Figma",
    "sketch": "Sketch",
    "3ds max": "3ds Max",
    "maya": "Maya",
    "blender": "Blender",
    "cinema 4d": "Cinema 4D",
    "zbrush alternate": "ZBrush (Alternate)",
    "dune ai": "Dune AI",
    "unity": "Unity",
    "unreal engine": "Unreal Engine",
    "chatgpt": "ChatGPT",
    "microsoft copilot": "Microsoft Copilot",
    "midjourney": "Midjourney",
    "stability ai": "Stability AI",
    "google chrome": "Google Chrome",
    "microsoft edge": "Microsoft Edge",
    "firefox": "Firefox",
    "opera": "Opera",
    "safari": "Safari",
    "google maps": "Google Maps",
    "google earth": "Google Earth",
    "yandex maps": "Yandex Maps",
    "dropbox": "Dropbox",
    "google drive": "Google Drive",
    "mega": "Mega",
    "nordvpn": "NordVPN",
    "ableton live": "Ableton Live",
    "cubase": "Cubase",
    "c": "C++",
    "java": "Java",
    "javascript": "JavaScript",
    "python": "Python",
    "rust": "Rust",
    "sublime text": "Sublime Text",
    "visual studio code": "Visual Studio Code",
    "app store": "App Store",
    "google play store": "Google Play Store",
    "microsoft store": "Microsoft Store",
    "playstation": "PlayStation",
    "xbox": "Xbox",
    "steam": "Steam",
    "daraz": "Daraz",
    "foodpanda": "Foodpanda",
    "twitter": "X (Twitter)",
    "x": "X (Twitter)",
    "twitter x": "X (Twitter)",
    "ok": "Odnoklassniki (OK)",
    "odnoklassniki": "Odnoklassniki (OK)",
    "hbo": "Max (HBO)",
    "max": "Max (HBO)",
    "hbo max": "Max (HBO)",
    "vscode": "Visual Studio Code",
    "google play": "Google Play Store",
    "play store": "Google Play Store",
    "copilot": "Microsoft Copilot",
    "openai": "ChatGPT",
    "gpt": "ChatGPT",
    "c plus plus": "C++",
    "cpp": "C++",
    "js": "JavaScript"
}

COUNTRY_CUSTOM_EMOJI_IDS = {
    "abkhazia": "5294236848103643477",
    "afghanistan": "5291937511591925566",
    "aland islands": "5294077418917616055",
    "albania": "5294202819077756005",
    "algeria": "5294048127240655242",
    "america": "5294244076533600593",
    "american samoa": "5291994273879709721",
    "andorra": "5294215205763434181",
    "angola": "5294516785482062829",
    "anguilla": "5292186323342350940",
    "antigua and barbuda": "5294005972136647964",
    "argentina": "5292208210495689627",
    "armenia": "5291978717508164018",
    "aruba": "5294007002928798927",
    "australia": "5294444247779399477",
    "austria": "5291975174160145850",
    "azerbaijan": "5294323533428579078",
    "bahamas": "5294031587321600012",
    "bahrain": "5294108398516720753",
    "bangladesh": "5291824687096027834",
    "barbados": "5294526187165471742",
    "belarus": "5294134426018536120",
    "belgium": "5291774466043435275",
    "belize": "5294171848068584842",
    "benin": "5293984969746566866",
    "bhutan": "5294121983498277263",
    "bolivia": "5294201479047957700",
    "botswana": "5294026179957772585",
    "brazil": "5291892229751723900",
    "britain": "5293993521026453119",
    "brunei": "5292098293692650297",
    "bulgaria": "5294308947719640437",
    "burkina faso": "5294153164960848949",
    "burundi": "5294051631933967760",
    "cambodia": "5294225191562400452",
    "cameroon": "5291997306126626950",
    "canada": "5292290347450259214",
    "cape verde": "5292203503211535593",
    "central african republic": "5294210571493724819",
    "chad": "5291780728105753403",
    "chile": "5294231037012888049",
    "china": "5294068833277990704",
    "colombia": "5294010206974397371",
    "comoros": "5294351381996521508",
    "congo": "5294035229453865597",
    "cook islands": "5292098684534675100",
    "costa rica": "5292063805105263554",
    "cote d ivoire": "5293991322003200135",
    "croatia": "5291999676948569127",
    "cuba": "5291963947115631526",
    "cyprus": "5294062721539526918",
    "czech": "5294242852467923382",
    "czech republic": "5294242852467923382",
    "denmark": "5294531860817268837",
    "djibouti": "5294127214768468283",
    "dominica": "5294485513825178032",
    "dominican republic": "5294522197140857947",
    "ecuador": "5292083733753517221",
    "egypt": "5293992082212409502",
    "el salvador": "5294337307388695687",
    "emirates": "5294314831824835370",
    "england": "5294410107084365278",
    "equatorial guinea": "5292170045416297012",
    "eritrea": "5291922054004625949",
    "estonia": "5291951143818123103",
    "ethiopia": "5292245976143124155",
    "european union": "5291992809295861098",
    "finland": "5294049961191690629",
    "france": "5291817660529533837",
    "gabon": "5294321325815389139",
    "gambia": "5294399820637688352",
    "georgia": "5294349389131697267",
    "germany": "5292013274815028523",
    "ghana": "5294347396266873249",
    "gibraltar": "5292055799286224027",
    "great britain": "5293993521026453119",
    "greece": "5291948395039054764",
    "greenland": "5292014752283774878",
    "guatemala": "5294336633078831209",
    "guinea": "5291892096607739008",
    "guinea bissau": "5294409819321550432",
    "guyana": "5292062692708736193",
    "haiti": "5292045130587462814",
    "honduras": "5291901034434682297",
    "hong kong": "5292166459118606932",
    "hong kong sar": "5292166459118606932",
    "hungary": "5294229581018975260",
    "iceland": "5294354358408859664",
    "india": "5291933173674957761",
    "iran": "5294220170745630736",
    "iraq": "5294325010897327367",
    "ireland": "5294471971793293647",
    "isle of man": "5294318478252070646",
    "israel": "5294069056616289553",
    "italy": "5291826830284709120",
    "ivory coast": "5293991322003200135",
    "jamaica": "5294505107465982830",
    "japan": "5291799063321139445",
    "jersey": "5291950280529697493",
    "jordan": "5291988613112814801",
    "kazakhstan": "5294227175837290463",
    "kenya": "5292111852904416801",
    "kiribati": "5294538934628405146",
    "korea": "5294408281723262763",
    "ksa": "5294163983983463099",
    "kuwait": "5292066437920218075",
    "kyrgyzstan": "5292091954320922577",
    "laos": "5291981530711746037",
    "latvia": "5292236016113966127",
    "lebanon": "5294193108156699621",
    "lesotho": "5292040693886247604",
    "liberia": "5291793810576137439",
    "libya": "5291858711826946840",
    "liechtenstein": "5292048742654957785",
    "lithuania": "5294343084119708700",
    "luxembourg": "5294423709245787718",
    "madagascar": "5291991568050312348",
    "malawi": "5294241881805312589",
    "malaysia": "5291858351049696702",
    "maldives": "5292004203844097218",
    "mali": "5292086972158858331",
    "malta": "5294532213004588353",
    "marshall islands": "5294180730060954484",
    "mauritania": "5294429743674840973",
    "mauritius": "5294127824653797277",
    "mexico": "5294535073452809778",
    "micronesia": "5291838156113470124",
    "moldova": "5294158486425325375",
    "monaco": "5294378161117614233",
    "mongolia": "5294316532631883496",
    "morocco": "5292108962391414885",
    "mozambique": "5294086708931874940",
    "myanmar": "5294254478944393569",
    "namibia": "5292021761670404922",
    "nauru": "5294463274484521342",
    "nepal": "5294458756178924088",
    "netherlands": "5291917995260533077",
    "new zealand": "5294189019347833274",
    "nicaragua": "5294240825243358100",
    "niger": "5291809418487290691",
    "nigeria": "5294456308047563965",
    "niue": "5294471336138134209",
    "north korea": "5294193812531333564",
    "north macedonia": "5294023611567332075",
    "norway": "5291761718580502030",
    "oman": "5291813666209946812",
    "pakistan": "5291825606219029010",
    "palestine": "5294289826525238172",
    "palestine state": "5294289826525238172",
    "panama": "5291959935616178405",
    "papua new guinea": "5291917995260533077",
    "paraguay": "5294525611639852679",
    "peru": "5292099427564018941",
    "philippines": "5291798075478661634",
    "poland": "5292190970496963836",
    "portugal": "5294436555492973610",
    "puerto rico": "5292121516580820347",
    "qatar": "5292166360334357676",
    "republic of the congo": "5294035229453865597",
    "romania": "5294107724206856227",
    "russia": "5294335323113807278",
    "russia federation": "5294335323113807278",
    "rwanda": "5294191265615729158",
    "san marino": "5292147350809106831",
    "sao tome and principe": "5292183188016222701",
    "saudi": "5294163983983463099",
    "saudi arabia": "5294163983983463099",
    "scotland": "5294434665707368018",
    "senegal": "5292087023698466689",
    "serbia": "5294458584380230360",
    "seychelles": "5291891186074672309",
    "sierra leone": "5294494314213167952",
    "singapore": "5294451304410663668",
    "slovakia": "5294538440707166931",
    "slovenia": "5294279359689938006",
    "solomon islands": "5294283890880433237",
    "somalia": "5294058817414255960",
    "south africa": "5294325281480266304",
    "south korea": "5294408281723262763",
    "spain": "5294513087515216901",
    "sri lanka": "5292102670264328257",
    "sudan": "5294177148058228060",
    "suriname": "5294396668131692138",
    "swaziland": "5294312482477724867",
    "sweden": "5291737091238026321",
    "switzerland": "5291791748991835084",
    "syria": "5294013428199869487",
    "taiwan": "5294095745543069603",
    "tajikistan": "5294120269806328883",
    "tanzania": "5292146096678658977",
    "thailand": "5293994384314882755",
    "togo": "5294097669688415562",
    "tonga": "5294283689016973348",
    "trinidad and tobago": "5294362935458548705",
    "tunisia": "5294484680601521871",
    "turkey": "5293993400767367408",
    "turkiye": "5293993400767367408",
    "turkmenistan": "5294098958178603764",
    "turks and caicos islands": "5294320866253884749",
    "uae": "5294314831824835370",
    "uganda": "5294192317882716626",
    "uk": "5293993521026453119",
    "ukraine": "5294263837678131580",
    "united arab emirates": "5294314831824835370",
    "united kingdom": "5293993521026453119",
    "united states": "5294244076533600593",
    "united states of america": "5294244076533600593",
    "uruguay": "5291928449210932974",
    "us": "5294244076533600593",
    "usa": "5294244076533600593",
    "uzbekistan": "5294217645304864345",
    "vanuatu": "5294448585696368047",
    "venezuela": "5294476442854247878",
    "viet nam": "5294235963340379688",
    "vietnam": "5294235963340379688",
    "virgin islands": "5294228039125718124",
    "wales": "5294139949346476093",
    "yemen": "5294058972033076492",
    "zambia": "5294100109229838880",
    "zimbabwe": "5294422158762592930",
}

COUNTRY_DISPLAY_NAMES = {
    "abkhazia": "Abkhazia",
    "afghanistan": "Afghanistan",
    "aland islands": "Aland Islands",
    "albania": "Albania",
    "algeria": "Algeria",
    "america": "United States",
    "american samoa": "American Samoa",
    "andorra": "Andorra",
    "angola": "Angola",
    "anguilla": "Anguilla",
    "antigua and barbuda": "Antigua and Barbuda",
    "argentina": "Argentina",
    "armenia": "Armenia",
    "aruba": "Aruba",
    "australia": "Australia",
    "austria": "Austria",
    "azerbaijan": "Azerbaijan",
    "bahamas": "Bahamas",
    "bahrain": "Bahrain",
    "bangladesh": "Bangladesh",
    "barbados": "Barbados",
    "belarus": "Belarus",
    "belgium": "Belgium",
    "belize": "Belize",
    "benin": "Benin",
    "bhutan": "Bhutan",
    "bolivia": "Bolivia",
    "botswana": "Botswana",
    "brazil": "Brazil",
    "britain": "United Kingdom",
    "brunei": "Brunei",
    "bulgaria": "Bulgaria",
    "burkina faso": "Burkina Faso",
    "burundi": "Burundi",
    "cambodia": "Cambodia",
    "cameroon": "Cameroon",
    "canada": "Canada",
    "cape verde": "Cape Verde",
    "central african republic": "Central African Republic",
    "chad": "Chad",
    "chile": "Chile",
    "china": "China",
    "colombia": "Colombia",
    "comoros": "Comoros",
    "congo": "Republic of the Congo",
    "cook islands": "Cook Islands",
    "costa rica": "Costa Rica",
    "cote d ivoire": "Ivory Coast",
    "croatia": "Croatia",
    "cuba": "Cuba",
    "cyprus": "Cyprus",
    "czech": "Czech Republic",
    "czech republic": "Czech Republic",
    "denmark": "Denmark",
    "djibouti": "Djibouti",
    "dominica": "Dominica",
    "dominican republic": "Dominican Republic",
    "ecuador": "Ecuador",
    "egypt": "Egypt",
    "el salvador": "El Salvador",
    "emirates": "United Arab Emirates",
    "england": "󠁧󠁢󠁥󠁮󠁧󠁿 England",
    "equatorial guinea": "Equatorial Guinea",
    "eritrea": "Eritrea",
    "estonia": "Estonia",
    "ethiopia": "Ethiopia",
    "european union": "European Union",
    "finland": "Finland",
    "france": "France",
    "gabon": "Gabon",
    "gambia": "Gambia",
    "georgia": "Georgia",
    "germany": "Germany",
    "ghana": "Ghana",
    "gibraltar": "Gibraltar",
    "great britain": "United Kingdom",
    "greece": "Greece",
    "greenland": "Greenland",
    "guatemala": "Guatemala",
    "guinea": "Guinea",
    "guinea bissau": "Guinea-Bissau",
    "guyana": "Guyana",
    "haiti": "Haiti",
    "honduras": "Honduras",
    "hong kong": "Hong Kong",
    "hong kong sar": "Hong Kong",
    "hungary": "Hungary",
    "iceland": "Iceland",
    "india": "India",
    "iran": "Iran",
    "iraq": "Iraq",
    "ireland": "Ireland",
    "isle of man": "Isle of Man",
    "israel": "Israel",
    "italy": "Italy",
    "ivory coast": "Ivory Coast",
    "jamaica": "Jamaica",
    "japan": "Japan",
    "jersey": "Jersey",
    "jordan": "Jordan",
    "kazakhstan": "Kazakhstan",
    "kenya": "Kenya",
    "kiribati": "Kiribati",
    "korea": "South Korea",
    "ksa": "Saudi Arabia",
    "kuwait": "Kuwait",
    "kyrgyzstan": "Kyrgyzstan",
    "laos": "Laos",
    "latvia": "Latvia",
    "lebanon": "Lebanon",
    "lesotho": "Lesotho",
    "liberia": "Liberia",
    "libya": "Libya",
    "liechtenstein": "Liechtenstein",
    "lithuania": "Lithuania",
    "luxembourg": "Luxembourg",
    "madagascar": "Madagascar",
    "malawi": "Malawi",
    "malaysia": "Malaysia",
    "maldives": "Maldives",
    "mali": "Mali",
    "malta": "Malta",
    "marshall islands": "Marshall Islands",
    "mauritania": "Mauritania",
    "mauritius": "Mauritius",
    "mexico": "Mexico",
    "micronesia": "Micronesia",
    "moldova": "Moldova",
    "monaco": "Monaco",
    "mongolia": "Mongolia",
    "morocco": "Morocco",
    "mozambique": "Mozambique",
    "myanmar": "Myanmar",
    "namibia": "Namibia",
    "nauru": "Nauru",
    "nepal": "Nepal",
    "netherlands": "Netherlands",
    "new zealand": "New Zealand",
    "nicaragua": "Nicaragua",
    "niger": "Niger",
    "nigeria": "Nigeria",
    "niue": "Niue",
    "north korea": "North Korea",
    "north macedonia": "North Macedonia",
    "norway": "Norway",
    "oman": "Oman",
    "pakistan": "Pakistan",
    "palestine": "Palestine",
    "palestine state": "Palestine",
    "panama": "Panama",
    "papua new guinea": "Papua New Guinea",
    "paraguay": "Paraguay",
    "peru": "Peru",
    "philippines": "Philippines",
    "poland": "Poland",
    "portugal": "Portugal",
    "puerto rico": "Puerto Rico",
    "qatar": "Qatar",
    "republic of the congo": "Republic of the Congo",
    "romania": "Romania",
    "russia": "Russia",
    "russia federation": "Russia",
    "rwanda": "Rwanda",
    "san marino": "San Marino",
    "sao tome and principe": "Sao Tome and Principe",
    "saudi": "Saudi Arabia",
    "saudi arabia": "Saudi Arabia",
    "scotland": "Scotland",
    "senegal": "Senegal",
    "serbia": "Serbia",
    "seychelles": "Seychelles",
    "sierra leone": "Sierra Leone",
    "singapore": "Singapore",
    "slovakia": "Slovakia",
    "slovenia": "Slovenia",
    "solomon islands": "Solomon Islands",
    "somalia": "Somalia",
    "south africa": "South Africa",
    "south korea": "South Korea",
    "spain": "Spain",
    "sri lanka": "Sri Lanka",
    "sudan": "Sudan",
    "suriname": "Suriname",
    "swaziland": "Swaziland",
    "sweden": "Sweden",
    "switzerland": "Switzerland",
    "syria": "Syria",
    "taiwan": "Taiwan",
    "tajikistan": "Tajikistan",
    "tanzania": "Tanzania",
    "thailand": "Thailand",
    "togo": "Togo",
    "tonga": "Tonga",
    "trinidad and tobago": "Trinidad and Tobago",
    "tunisia": "Tunisia",
    "turkey": "Turkey",
    "turkiye": "Turkey",
    "turkmenistan": "Turkmenistan",
    "turks and caicos islands": "Turks and Caicos Islands",
    "uae": "United Arab Emirates",
    "uganda": "Uganda",
    "uk": "United Kingdom",
    "ukraine": "Ukraine",
    "united arab emirates": "United Arab Emirates",
    "united kingdom": "United Kingdom",
    "united states": "United States",
    "united states of america": "United States",
    "uruguay": "Uruguay",
    "us": "United States",
    "usa": "United States",
    "uzbekistan": "Uzbekistan",
    "vanuatu": "Vanuatu",
    "venezuela": "Venezuela",
    "viet nam": "Vietnam",
    "vietnam": "Vietnam",
    "virgin islands": "Virgin Islands",
    "wales": "󠁧󠁢󠁷󠁬󠁳󠁿 Wales",
    "yemen": "Yemen",
    "zambia": "Zambia",
    "zimbabwe": "Zimbabwe",
}

# ================= LIVE CUSTOM EMOJI SETUP =================
# Built from your uploaded live emoji list.
# You can still override/add entries using /setemoji. Overrides are saved in custom_emoji_overrides.json.
CUSTOM_EMOJI_FILE = "custom_emoji_overrides_v6_exact_country_buttons.json"

BUILTIN_CUSTOM_EMOJI_IDS = [
    "5323379047315555501",
    "5334954057192719331",
    "5335012820935261564",
    "5323764984486837459",
    "5325612636467903082",
    "5323261730283863478",
    "5330081305126255700",
    "5334750591707005005",
    "5330345557284109044",
    "5334595899869905653",
    "5319160079465857105",
    "5334933574493683027",
    "5325726234057915652",
    "5321403907820240828",
    "5323608076446613036",
    "5325647210954637197",
    "5323687726615119535",
    "5321447183910716259",
    "5325865356638569272",
    "5325742125436910868",
    "5328064671951896068",
    "5327959866159938948",
    "5330321861949539755",
    "5328050550099427291",
    "5328175271654736902",
    "5330248916224983855",
    "5330237710655306682",
    "5334592721594105691",
    "5327982530702359565",
    "5328029650788563621",
    "5334792209940102096",
    "5328242556612395440",
    "5334678011054669335",
    "5332449498553663205",
    "5334764984142412896",
    "5334853932915114338",
    "5334807822146225472",
    "5332524123610430820",
    "5332823323917173335",
    "5334663339446387801",
    "5334998226636390258",
    "5330337435500951363",
    "5334707727933390944",
    "5334681713316479679",
    "5334942061349059951",
    "5321533581472842536",
    "5334932883003949665",
    "5346056560537779652",
    "5334955749409834455",
    "5332394707655869572",
    "5346024142124633117",
    "5346242859039209592",
    "5346309375197725525",
    "5357184657592962696",
    "5346319945112240722",
    "5318911503938634641",
    "5346134750417403743",
    "5345799562579688546",
    "5346242644290846992",
    "5345937224871461019",
    "5346251367369425932",
    "5345776794958053103",
    "5346259862814734771",
    "5345844509412444249",
    "5346074681004801565",
    "5346296430166293639",
    "5345967461441223890",
    "5346017678198848586",
    "5346188613602263703",
    "5345845252441784353",
    "5346008706012169915",
    "5346066456142429527",
    "5346172593374248407",
    "5346181118884331907",
    "5346308584923740680",
    "5346284438617604231",
    "5346024520081751155",
    "5346213374088723754",
    "5345833716159627218",
    "5346103513120258857",
    "5345818383126384016",
    "5359584650958226302",
    "5359321266383766546",
    "5359320566304096699",
    "5359437015752401733",
    "5364036341610858181",
    "5364075889669718872",
    "5364111181415996352",
    "5366068097365066701",
    "5363831115188553043",
    "5363810946022131056",
    "5364199932620194408",
    "5363899233369868662",
    "5359772714691216710",
    "5359472852959512416",
    "5357394595594388140",
    "5363989350373671453",
    "5364101182732127639",
    "5359480394922082925",
    "5359320531944358335",
    "5363858422590619939",
    "5363973751052452405",
    "5364121867294621703",
    "5364339557712020484",
    "5364314346253991843",
    "5363892211098339885",
    "5364343191254352560",
    "5357286671656176924",
    "5359602676935968589",
    "5363977152666550281",
    "5364182988974211543",
    "5364290083983736876",
    "5364105576483668906",
    "5364006925379846491",
    "5363986369666369188",
    "5364226346669065610",
    "5363863671040657237",
    "5359726582447487916",
    "5372937764411031477",
    "5359618237602480327",
    "5371089601328857708",
    "5359758030198031389",
    "5359503252738029857",
    "5362034259785694259",
    "5361963895336485277",
    "5361575381184823162",
    "5370988368949690738",
    "5373262983629650845",
    "5373193993569977969",
    "5372994299065550645",
    "5372878055775683161",
    "5373246052868571826",
    "5373025510592888616",
    "5373145666597961572",
    "5373054312643575332",
    "5372917956021862036",
    "5373232592441065346",
    "5370577035636786019",
    "5372878077250519677",
    "5373229590258925215",
    "5373054600406382936",
    "5370852523429085514",
    "5370722600668382252",
    "5373130604147654226",
    "5370857634440170316",
    "5373306783706137993",
    "5373019729566908647",
    "5373144051690258848",
    "5373265917092316632",
    "5373261557700509032",
    "5294236848103643477",
    "5291937511591925566",
    "5294077418917616055",
    "5294202819077756005",
    "5294048127240655242",
    "5291994273879709721",
    "5294215205763434181",
    "5294516785482062829",
    "5292186323342350940",
    "5294005972136647964",
    "5292208210495689627",
    "5291978717508164018",
    "5294007002928798927",
    "5294444247779399477",
    "5291975174160145850",
    "5294323533428579078",
    "5294031587321600012",
    "5294108398516720753",
    "5291824687096027834",
    "5294526187165471742",
    "5294134426018536120",
    "5291774466043435275",
    "5294171848068584842",
    "5293984969746566866",
    "5294121983498277263",
    "5294201479047957700",
    "5294026179957772585",
    "5291892229751723900",
    "5292098293692650297",
    "5294308947719640437",
    "5294153164960848949",
    "5294051631933967760",
    "5294225191562400452",
    "5291997306126626950",
    "5292290347450259214",
    "5292203503211535593",
    "5294210571493724819",
    "5291780728105753403",
    "5294231037012888049",
    "5294068833277990704",
    "5294010206974397371",
    "5294351381996521508",
    "5294035229453865597",
    "5292098684534675100",
    "5292063805105263554",
    "5293991322003200135",
    "5291999676948569127",
    "5291963947115631526",
    "5294062721539526918",
    "5294242852467923382",
    "5294531860817268837",
    "5294127214768468283",
    "5294485513825178032",
    "5294522197140857947",
    "5292083733753517221",
    "5293992082212409502",
    "5294337307388695687",
    "5294410107084365278",
    "5292170045416297012",
    "5291922054004625949",
    "5291951143818123103",
    "5292245976143124155",
    "5291992809295861098",
    "5292055799286224027",
    "5294399820637688352",
    "5292014752283774878",
    "5294049961191690629",
    "5291817660529533837",
    "5294321325815389139",
    "5294349389131697267",
    "5292013274815028523",
    "5294347396266873249",
    "5291948395039054764",
    "5294409819321550432",
    "5294336633078831209",
    "5291892096607739008",
    "5292062692708736193",
    "5292045130587462814",
    "5291901034434682297",
    "5292166459118606932",
    "5294229581018975260",
    "5294354358408859664",
    "5291933173674957761",
    "5294220170745630736",
    "5294325010897327367",
    "5294471971793293647",
    "5294318478252070646",
    "5294069056616289553",
    "5291826830284709120",
    "5294505107465982830",
    "5291799063321139445",
    "5291950280529697493",
    "5291988613112814801",
    "5294227175837290463",
    "5292111852904416801",
    "5294538934628405146",
    "5294193812531333564",
    "5294408281723262763",
    "5292066437920218075",
    "5292091954320922577",
    "5291981530711746037",
    "5292236016113966127",
    "5294193108156699621",
    "5292040693886247604",
    "5291793810576137439",
    "5291858711826946840",
    "5292048742654957785",
    "5294343084119708700",
    "5294423709245787718",
    "5294023611567332075",
    "5291991568050312348",
    "5294241881805312589",
    "5291858351049696702",
    "5292004203844097218",
    "5292086972158858331",
    "5294532213004588353",
    "5294180730060954484",
    "5294429743674840973",
    "5294127824653797277",
    "5294535073452809778",
    "5291838156113470124",
    "5294158486425325375",
    "5294378161117614233",
    "5294316532631883496",
    "5292108962391414885",
    "5294086708931874940",
    "5294254478944393569",
    "5292021761670404922",
    "5294463274484521342",
    "5294458756178924088",
    "5291917995260533077",
    "5294189019347833274",
    "5294240825243358100",
    "5291809418487290691",
    "5294456308047563965",
    "5294471336138134209",
    "5291761718580502030",
    "5291813666209946812",
    "5291825606219029010",
    "5294289826525238172",
    "5291959935616178405",
    "5294525611639852679",
    "5291798075478661634",
    "5292099427564018941",
    "5292190970496963836",
    "5294436555492973610",
    "5292121516580820347",
    "5292166360334357676",
    "5294107724206856227",
    "5294335323113807278",
    "5294191265615729158",
    "5292147350809106831",
    "5292183188016222701",
    "5294163983983463099",
    "5294434665707368018",
    "5292087023698466689",
    "5294458584380230360",
    "5291891186074672309",
    "5294494314213167952",
    "5294451304410663668",
    "5294538440707166931",
    "5294279359689938006",
    "5294283890880433237",
    "5294058817414255960",
    "5294325281480266304",
    "5294513087515216901",
    "5292102670264328257",
    "5294177148058228060",
    "5294396668131692138",
    "5294312482477724867",
    "5291737091238026321",
    "5291791748991835084",
    "5294013428199869487",
    "5294095745543069603",
    "5294120269806328883",
    "5292146096678658977",
    "5293994384314882755",
    "5294097669688415562",
    "5294283689016973348",
    "5294362935458548705",
    "5294484680601521871",
    "5293993400767367408",
    "5294098958178603764",
    "5294320866253884749",
    "5294244076533600593",
    "5294192317882716626",
    "5294314831824835370",
    "5293993521026453119",
    "5294263837678131580",
    "5294448585696368047",
    "5294217645304864345",
    "5291928449210932974",
    "5294476442854247878",
    "5294235963340379688",
    "5294228039125718124",
    "5294139949346476093",
    "5294058972033076492",
    "5294100109229838880",
    "5294422158762592930",
    "5210952531676504517",
    "5443038326535759644",
    "5424972470023104089",
    "5276032951342088188",
    "5224607267797606837",
    "5461151367559141950",
    "5244837092042750681",
    "5424818078833715060",
    "5282843764451195532",
    "5253997076169115797",
    "5949584381424178413",
    "5192716985300951422",
    "5210956306952758910",
    "5294339927318739359"
]

ACTION_CUSTOM_EMOJI_IDS = {
    "warning": "5210952531676504517",
    "get_number": "5443038326535759644",
    "buy_premium": "5443038326535759644",
    "otp": "5443038326535759644",
    "open_bot": "5443038326535759644",
    "welcome": "5424972470023104089",
    "rocket": "5224607267797606837",
    "fastest": "5276032951342088188",
    "select_option": "5224607267797606837",
    "admin": "5461151367559141950",
    "stats": "5244837092042750681",
    "stock": "5244837092042750681",
    "broadcast": "5424818078833715060",
    "manage": "5282843764451195532",
    "support": "5282843764451195532",
    "upload": "5282843764451195532",
    "delete": "5282843764451195532",
    "user_mode": "5253997076169115797",
    "close": "5253997076169115797",
    "refresh": "5210952531676504517",
    "success": "5949584381424178413",
    "verify": "5949584381424178413",
    "service": "5949584381424178413",
    "custom": "5949584381424178413",
    "country": "5192716985300951422",
    "phone": "5210956306952758910",
    "phone_otp_received": "5210952531676504517",
    "phone_no_otp": "5206607081334906820",
    "waiting_otp": "5210956306952758910",
    "message_otp": "5443038326535759644",
    "voice_otp": "5294339927318739359",
    "lock": "5210952531676504517"
}

ACTION_EMOJI_ALTS = {
    'warning': LIVE_TEXT_FALLBACK,
    'get_number': LIVE_TEXT_FALLBACK,
    'buy_premium': LIVE_TEXT_FALLBACK,
    'otp': LIVE_TEXT_FALLBACK,
    'open_bot': LIVE_TEXT_FALLBACK,
    'welcome': LIVE_TEXT_FALLBACK,
    'rocket': LIVE_TEXT_FALLBACK,
    'fastest': LIVE_TEXT_FALLBACK,
    'select_option': LIVE_TEXT_FALLBACK,
    'admin': LIVE_TEXT_FALLBACK,
    'stats': LIVE_TEXT_FALLBACK,
    'stock': LIVE_TEXT_FALLBACK,
    'broadcast': LIVE_TEXT_FALLBACK,
    'manage': LIVE_TEXT_FALLBACK,
    'support': LIVE_TEXT_FALLBACK,
    'upload': LIVE_TEXT_FALLBACK,
    'delete': LIVE_TEXT_FALLBACK,
    'user_mode': LIVE_TEXT_FALLBACK,
    'close': LIVE_TEXT_FALLBACK,
    'refresh': LIVE_TEXT_FALLBACK,
    'success': LIVE_TEXT_FALLBACK,
    'verify': LIVE_TEXT_FALLBACK,
    'service': LIVE_TEXT_FALLBACK,
    'custom': LIVE_TEXT_FALLBACK,
    'country': LIVE_TEXT_FALLBACK,
    'phone': LIVE_TEXT_FALLBACK,
    'phone_otp_received': LIVE_TEXT_FALLBACK,
    'phone_no_otp': LIVE_TEXT_FALLBACK,
    'waiting_otp': LIVE_TEXT_FALLBACK,
    'message_otp': LIVE_TEXT_FALLBACK,
    'voice_otp': LIVE_TEXT_FALLBACK,
    'lock': LIVE_TEXT_FALLBACK,
}


# Force using the separated live-emoji data files when they are present.
if _SERVICE_CUSTOM_EMOJI_IDS_FILE is not None:
    SERVICE_CUSTOM_EMOJI_IDS = dict(_SERVICE_CUSTOM_EMOJI_IDS_FILE)
    SERVICE_DISPLAY_NAMES = dict(_SERVICE_DISPLAY_NAMES_FILE)
    SERVICE_OPTIONS = list(_SERVICE_OPTIONS_FILE)
if _COUNTRY_CUSTOM_EMOJI_IDS_FILE is not None:
    COUNTRY_CUSTOM_EMOJI_IDS = dict(_COUNTRY_CUSTOM_EMOJI_IDS_FILE)
    COUNTRY_DISPLAY_NAMES = dict(_COUNTRY_DISPLAY_NAMES_FILE)
if _ACTION_CUSTOM_EMOJI_IDS_FILE is not None:
    ACTION_CUSTOM_EMOJI_IDS = dict(_ACTION_CUSTOM_EMOJI_IDS_FILE)
    ACTION_EMOJI_ALTS = dict(_ACTION_EMOJI_ALTS_FILE)
ACTION_CUSTOM_EMOJI_IDS.setdefault("buy_premium", "5443038326535759644")
ACTION_CUSTOM_EMOJI_IDS.setdefault("support", "5282843764451195532")
ACTION_CUSTOM_EMOJI_IDS["phone_otp_received"] = "5210952531676504517"
ACTION_CUSTOM_EMOJI_IDS["phone_no_otp"] = "5206607081334906820"
ACTION_EMOJI_ALTS.setdefault("buy_premium", LIVE_TEXT_FALLBACK)
ACTION_EMOJI_ALTS.setdefault("support", LIVE_TEXT_FALLBACK)
ACTION_EMOJI_ALTS.setdefault("phone_otp_received", LIVE_TEXT_FALLBACK)
ACTION_EMOJI_ALTS.setdefault("phone_no_otp", LIVE_TEXT_FALLBACK)

SERVICE_EMOJI_ALTS = dict(_SERVICE_EMOJI_ALTS_FILE or {})
COUNTRY_EMOJI_ALTS = dict(_COUNTRY_EMOJI_ALTS_FILE or {})
ACTION_EMOJI_EXACT_ALTS = dict(_ACTION_EMOJI_EXACT_ALTS_FILE or {})

# Exact fallback text keeps Telegram custom-emoji entities valid for every mapped item.
SERVICE_ICONS = {key: SERVICE_EMOJI_ALTS.get(key, LIVE_TEXT_FALLBACK) for key in SERVICE_CUSTOM_EMOJI_IDS}
country_flags = {key: COUNTRY_EMOJI_ALTS.get(key, LIVE_TEXT_FALLBACK) for key in COUNTRY_CUSTOM_EMOJI_IDS}



def normalize_emoji_key(text):
    """Normalize service/country/action names for custom emoji lookup."""
    text = str(text or "").lower()
    text = re.sub(r"[\U0001F1E6-\U0001F1FF\U000E0060-\U000E007F]", " ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


SERVICE_PRIORITY_OPTIONS = [
    "Telegram",
    "WhatsApp",
    "Facebook",
    "Instagram",
    "Google",
    "Gmail",
    "Google Chrome",
    "Amazon",
    "YouTube",
    "TikTok",
    "Messenger",
    "Imo",
    "Discord",
    "Snapchat",
    "Netflix",
    "Apple",
    "PayPal",
    "Microsoft Copilot",
    "Google Play Store",
    "Google Drive",
    "Google Maps",
    "X (Twitter)",
    "LinkedIn",
    "Zoom",
    "Signal",
    "Viber",
    "LINE",
    "WeChat",
]


def prioritize_service_options(options):
    by_key = {}
    ordered = []
    for service in options:
        key = normalize_emoji_key(service)
        if key and key not in by_key:
            by_key[key] = service
            ordered.append(service)

    prioritized = []
    used = set()
    for service in SERVICE_PRIORITY_OPTIONS:
        key = normalize_emoji_key(service)
        if key in by_key and key not in used:
            prioritized.append(by_key[key])
            used.add(key)

    prioritized.extend(service for service in ordered if normalize_emoji_key(service) not in used)
    return prioritized


SERVICE_OPTIONS = prioritize_service_options(SERVICE_OPTIONS)


def strip_country_flag(text):
    text = str(text or "").strip()
    # Remove normal/Unicode emoji saved in old filenames or database rows.
    text = re.sub(r"^[\U0001F1E6-\U0001F1FF\U000E0060-\U000E007F\U0001F300-\U0001FAFF\u2600-\u27BF\ufe0f\u200d\s]+", "", text)
    text = re.sub(r"^(flag|country|service)\s*[:\-]\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def build_default_custom_emoji_config():
    service_items = {}
    for key, emoji_id in SERVICE_CUSTOM_EMOJI_IDS.items():
        service_items[key] = {
            "id": str(emoji_id),
            "alt": LIVE_TEXT_FALLBACK,
            "name": SERVICE_DISPLAY_NAMES.get(key, key)
        }

    country_items = {}
    for key, emoji_id in COUNTRY_CUSTOM_EMOJI_IDS.items():
        country_items[key] = {
            "id": str(emoji_id),
            "alt": LIVE_TEXT_FALLBACK,
            "name": COUNTRY_DISPLAY_NAMES.get(key, key)
        }

    action_items = {}
    for key, emoji_id in ACTION_CUSTOM_EMOJI_IDS.items():
        action_items[key] = {
            "id": str(emoji_id),
            "alt": LIVE_TEXT_FALLBACK,
            "name": key
        }

    return {
        "defaults": {
            "service": {"id": SERVICE_CUSTOM_EMOJI_IDS.get("whatsapp", BUILTIN_CUSTOM_EMOJI_IDS[0]), "alt": LIVE_TEXT_FALLBACK},
            "country": {"id": COUNTRY_CUSTOM_EMOJI_IDS.get("bangladesh", BUILTIN_CUSTOM_EMOJI_IDS[0]), "alt": LIVE_TEXT_FALLBACK},
            "action": {"id": ACTION_CUSTOM_EMOJI_IDS.get("success", BUILTIN_CUSTOM_EMOJI_IDS[0]), "alt": LIVE_TEXT_FALLBACK}
        },
        "id_pool": BUILTIN_CUSTOM_EMOJI_IDS,
        "services": service_items,
        "countries": country_items,
        "actions": action_items
    }


def deep_merge_dict(base, incoming):
    for key, value in (incoming or {}).items():
        # Do not let an older empty JSON file remove the built-in live emoji IDs.
        if key == "id" and str(value or "").strip() == "" and str(base.get("id", "")).strip():
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge_dict(base[key], value)
        else:
            base[key] = value
    return base


def force_builtin_live_emoji_ids(config):
    """Keep built-in service/country/action IDs correct even if an older JSON file had wrong IDs."""
    config.setdefault("services", {})
    config.setdefault("countries", {})
    config.setdefault("actions", {})

    for key, emoji_id in SERVICE_CUSTOM_EMOJI_IDS.items():
        item = config["services"].setdefault(key, {})
        # Force built-in IDs/alts every run so older JSON files cannot show wrong icons.
        item["id"] = str(emoji_id)
        item["alt"] = LIVE_TEXT_FALLBACK
        item["name"] = SERVICE_DISPLAY_NAMES.get(key, key)

    for key, emoji_id in COUNTRY_CUSTOM_EMOJI_IDS.items():
        item = config["countries"].setdefault(key, {})
        # Force exact country live-emoji IDs every run. This fixes the issue where all
        # country buttons showed the same Bangladesh/default flag from an old config file.
        item["id"] = str(emoji_id)
        item["alt"] = LIVE_TEXT_FALLBACK
        item["name"] = COUNTRY_DISPLAY_NAMES.get(key, key)

    for key, emoji_id in ACTION_CUSTOM_EMOJI_IDS.items():
        item = config["actions"].setdefault(key, {})
        item["id"] = str(emoji_id)
        item["alt"] = LIVE_TEXT_FALLBACK
        item["name"] = key

    config.setdefault("defaults", {})["country"] = {"id": COUNTRY_CUSTOM_EMOJI_IDS.get("bangladesh", ""), "alt": LIVE_TEXT_FALLBACK}
    config.setdefault("defaults", {})["service"] = {"id": SERVICE_CUSTOM_EMOJI_IDS.get("whatsapp", ""), "alt": LIVE_TEXT_FALLBACK}
    config.setdefault("defaults", {})["action"] = {"id": ACTION_CUSTOM_EMOJI_IDS.get("success", ""), "alt": LIVE_TEXT_FALLBACK}
    return config


def load_custom_emoji_config():
    config = build_default_custom_emoji_config()
    if os.path.exists(CUSTOM_EMOJI_FILE):
        try:
            with open(CUSTOM_EMOJI_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            deep_merge_dict(config, loaded)
        except Exception as e:
            print(f"Could not load {CUSTOM_EMOJI_FILE}: {e}")

    # Important: this fixes the problem where all country flags show the same emoji
    # because an older custom_emoji_overrides.json had stale/default country IDs.
    config = force_builtin_live_emoji_ids(config)
    save_custom_emoji_config(config)
    return config


def save_custom_emoji_config(config=None):
    config = config or CUSTOM_EMOJI_CONFIG
    with open(CUSTOM_EMOJI_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


CUSTOM_EMOJI_CONFIG = None
CUSTOM_EMOJI_CONFIG = load_custom_emoji_config()


def _emoji_item(kind, key):
    key = normalize_emoji_key(key)
    collection_name = f"{kind}s"
    collection = CUSTOM_EMOJI_CONFIG.get(collection_name, {})

    if key in collection:
        return collection[key]

    # Country/service aliases: exact match above is best; then try safe whole-word/long-key match.
    key_words = set(key.split())
    best_item = None
    best_score = -1
    for saved_key, item in collection.items():
        if not saved_key:
            continue
        saved_words = set(saved_key.split())
        if saved_words and (saved_words.issubset(key_words) or key_words.issubset(saved_words)):
            score = len(saved_key)
            if score > best_score:
                best_score = score
                best_item = item
    if best_item:
        return best_item

    return CUSTOM_EMOJI_CONFIG.get("defaults", {}).get(kind, {})


def get_live_emoji_id(kind, key):
    item = _emoji_item(kind, key)
    emoji_id = str(item.get("id", "")).strip()
    if emoji_id:
        return emoji_id

    default_item = CUSTOM_EMOJI_CONFIG.get("defaults", {}).get(kind, {})
    default_id = str(default_item.get("id", "")).strip()
    if default_id:
        return default_id

    pool = CUSTOM_EMOJI_CONFIG.get("id_pool") or BUILTIN_CUSTOM_EMOJI_IDS
    if pool:
        digest = hashlib.sha1(f"{kind}:{normalize_emoji_key(key)}".encode("utf-8")).hexdigest()
        return pool[int(digest[:8], 16) % len(pool)]
    return ""


def get_live_emoji_alt(kind, key, fallback=LIVE_TEXT_FALLBACK):
    item = _emoji_item(kind, key)
    alt = str(item.get("alt", "")).strip()
    if alt:
        return alt
    return fallback


def get_exact_live_emoji_id(kind, key):
    """Return the exact custom emoji ID. For countries this NEVER falls back to Bangladesh.
    This fixes the issue where every country button looked like the same/default flag.
    """
    kind = str(kind or "").strip().lower()
    norm = normalize_emoji_key(key)

    if kind == "country":
        # 1) exact country name / alias from live_countries.py
        emoji_id = str(COUNTRY_CUSTOM_EMOJI_IDS.get(norm, "")).strip()
        if emoji_id:
            return emoji_id
        # 2) custom override file, but only exact key; never use defaults for country buttons
        item = CUSTOM_EMOJI_CONFIG.get("countries", {}).get(norm, {})
        emoji_id = str(item.get("id", "")).strip()
        return emoji_id if re.fullmatch(r"\d{10,25}", emoji_id) else ""

    if kind == "service":
        emoji_id = str(SERVICE_CUSTOM_EMOJI_IDS.get(norm, "")).strip()
        if emoji_id:
            return emoji_id
        item = CUSTOM_EMOJI_CONFIG.get("services", {}).get(norm, {})
        emoji_id = str(item.get("id", "")).strip()
        if emoji_id:
            return emoji_id
        return get_live_emoji_id("service", key)

    if kind == "action":
        raw_key = str(key or "").strip().lower()
        emoji_id = str(ACTION_CUSTOM_EMOJI_IDS.get(raw_key, "")).strip()
        if emoji_id:
            return emoji_id
        emoji_id = str(ACTION_CUSTOM_EMOJI_IDS.get(norm, "")).strip()
        if emoji_id:
            return emoji_id
        item = CUSTOM_EMOJI_CONFIG.get("actions", {}).get(norm, {})
        emoji_id = str(item.get("id", "")).strip()
        if emoji_id:
            return emoji_id
        return get_live_emoji_id("action", key)

    return get_live_emoji_id(kind, key)


def live_emoji_html(kind, key, fallback=LIVE_TEXT_FALLBACK):
    emoji_id = get_exact_live_emoji_id(kind, key)
    # Telegram requires a real emoji character inside <tg-emoji>.
    # In live-only mode this placeholder is only a technical placeholder.
    placeholders = {
        "service": "📱",
        "country": "🏳️",
        "action": "🔹"
    }
    alt = placeholders.get(str(kind), fallback or LIVE_TEXT_FALLBACK)
    if emoji_id and re.fullmatch(r"\d{10,25}", str(emoji_id)):
        return f'<tg-emoji emoji-id="{escape(str(emoji_id))}">{escape(alt)}</tg-emoji>'
    return ""

def live_service_html(service_name):
    return live_emoji_html("service", service_name, LIVE_TEXT_FALLBACK)


def live_country_html(country_name):
    plain_country = strip_country_flag(country_name)
    return live_emoji_html("country", plain_country, LIVE_TEXT_FALLBACK)


def live_action_html(action_key):
    return live_emoji_html("action", action_key, LIVE_TEXT_FALLBACK)


class _LiveJsonMarkupBase(getattr(types, "JsonSerializable", object)):
    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)


class LiveInlineKeyboardMarkup(_LiveJsonMarkupBase):
    def __init__(self, row_width=3):
        self.row_width = row_width
        self.inline_keyboard = []

    def add(self, *buttons, row_width=None):
        width = row_width or self.row_width
        row = []
        for button in buttons:
            row.append(button)
            if len(row) >= width:
                self.inline_keyboard.append(row)
                row = []
        if row:
            self.inline_keyboard.append(row)
        return self

    def row(self, *buttons):
        self.inline_keyboard.append(list(buttons))
        return self

    def to_dict(self):
        return {"inline_keyboard": self.inline_keyboard}


class LiveReplyKeyboardMarkup(_LiveJsonMarkupBase):
    def __init__(self, resize_keyboard=True, row_width=3, one_time_keyboard=False, selective=False):
        self.keyboard = []
        self.resize_keyboard = resize_keyboard
        self.row_width = row_width
        self.one_time_keyboard = one_time_keyboard
        self.selective = selective

    def add(self, *buttons, row_width=None):
        width = row_width or self.row_width
        row = []
        for button in buttons:
            row.append(button)
            if len(row) >= width:
                self.keyboard.append(row)
                row = []
        if row:
            self.keyboard.append(row)
        return self

    def row(self, *buttons):
        self.keyboard.append(list(buttons))
        return self

    def to_dict(self):
        return {
            "keyboard": self.keyboard,
            "resize_keyboard": self.resize_keyboard,
            "one_time_keyboard": self.one_time_keyboard,
            "selective": self.selective
        }


def inline_button(text, callback_data=None, url=None, copy_text=None, action_key=None, emoji_kind=None, emoji_key=None, style=None, show_fallback_emoji=False):
    button = {"text": str(text)}
    if callback_data is not None:
        button["callback_data"] = str(callback_data)
    if url is not None:
        button["url"] = str(url)
    if copy_text is not None:
        button["copy_text"] = {"text": str(copy_text)}
    default_styles = {
        "get_number": "primary",
        "otp": "primary",
        "open_bot": "primary",
        "refresh": "danger",
        "warning": "danger",
        "delete": "danger",
        "close": "danger",
        "success": "success",
        "verify": "success",
        "back": "primary",
        "custom": "primary",
        "lock": "primary",
        "upload": "primary",
        "stats": "success",
        "manage": "primary",
        "broadcast": "primary",
    }
    resolved_style = style or default_styles.get(str(action_key or "").strip().lower())
    if resolved_style:
        button["style"] = str(resolved_style)

    icon_id = ""
    if emoji_kind and emoji_key:
        # Exact lookup. Country buttons do NOT use the default/Bangladesh flag anymore.
        icon_id = get_exact_live_emoji_id(emoji_kind, emoji_key)
    elif action_key:
        icon_id = get_exact_live_emoji_id("action", action_key)

    # Live-only mode: never prepend normal emoji text. The icon is passed by ID only.
    if icon_id and re.fullmatch(r"\d{10,25}", str(icon_id)):
        button["icon_custom_emoji_id"] = str(icon_id)
    return button


def reply_button(text, action_key=None, emoji_kind=None, emoji_key=None, style=None):
    # Current Telegram Bot API supports icon_custom_emoji_id on reply keyboard buttons too.
    # The button text remains clean; the live icon is passed by ID only.
    button = {"text": str(text)}
    icon_id = ""
    if emoji_kind and emoji_key:
        icon_id = get_exact_live_emoji_id(emoji_kind, emoji_key)
    elif action_key:
        icon_id = get_exact_live_emoji_id("action", action_key)
    if icon_id and re.fullmatch(r"\d{10,25}", str(icon_id)):
        button["icon_custom_emoji_id"] = str(icon_id)
    default_styles = {
        "get_number": "primary",
        "upload": "primary",
        "stats": "success",
        "manage": "primary",
        "broadcast": "primary",
        "back": "danger",
    }
    resolved_style = style or default_styles.get(str(action_key or "").strip().lower())
    if resolved_style:
        button["style"] = str(resolved_style)
    return button

def stock_pair_buttons(service_name, country_name, count, filename):
    token = get_file_token(filename)
    country_plain = strip_country_flag(country_name)
    service_btn = inline_button(
        f"{service_name}",
        callback_data=f"buy|{token}",
        emoji_kind="service",
        emoji_key=service_name
    )
    country_btn = inline_button(
        f"{country_plain} ({count})",
        callback_data=f"buy|{token}",
        emoji_kind="country",
        emoji_key=country_plain
    )
    return service_btn, country_btn


def warn_missing_default_live_emojis():
    missing = []
    for key in ("service", "country", "action"):
        if not CUSTOM_EMOJI_CONFIG.get("defaults", {}).get(key, {}).get("id"):
            missing.append(key)
    if missing:
        print("Live emoji defaults missing:", ", ".join(missing))
        print("   Set them with /setemoji default_service <custom_emoji_id> <alt>, /setemoji default_country <id> <alt>, /setemoji default_action <id> <alt>")


# =================  SERVICE/COUNTRY PICKER SETTINGS =================
# Telegram inline keyboards should be kept under the practical button limit.
# These paginated lists still show every service/country, just page by page.
SERVICE_PAGE_SIZE = 80
COUNTRY_PAGE_SIZE = 80

# User Get Number flow: first show services, then show countries for the selected service.
USER_SERVICE_PAGE_SIZE = 40
USER_COUNTRY_PAGE_SIZE = 40
LIVE_TRAFFIC_RANGE_LIMIT = 80
USER_LIVE_RANGE_PAGE_SIZE = 20
LIVE_TRAFFIC_AUTO_REFRESH_SECONDS = 20
LIVE_TRAFFIC_AUTO_REFRESH_TTL = 600


def _country_options():
    items = []
    seen = set()
    for key, name in COUNTRY_DISPLAY_NAMES.items():
        pretty = str(name).strip() or key.title()
        norm = normalize_emoji_key(pretty)
        if norm and norm not in seen:
            seen.add(norm)
            items.append(pretty)
    return sorted(items, key=lambda x: x.lower())


COUNTRY_OPTIONS = _country_options()


def total_pages(total_items, page_size):
    return max(1, (int(total_items) + int(page_size) - 1) // int(page_size))


def clamp_page(page, total_items, page_size):
    try:
        page = int(page)
    except Exception:
        page = 0
    return max(0, min(page, total_pages(total_items, page_size) - 1))


def send_or_edit_message(chat_id, text, reply_markup=None, parse_mode="HTML", edit_message=None):
    if edit_message is not None:
        try:
            return bot.edit_message_text(
                text,
                edit_message.chat.id,
                edit_message.message_id,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except Exception:
            pass
    return bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)


def build_service_picker_markup(page=0):
    page = clamp_page(page, len(SERVICE_OPTIONS), SERVICE_PAGE_SIZE)
    start = page * SERVICE_PAGE_SIZE
    end = start + SERVICE_PAGE_SIZE
    page_services = SERVICE_OPTIONS[start:end]

    markup = LiveInlineKeyboardMarkup(row_width=2)
    buttons = [
        inline_button(
            service,
            callback_data=f"svc|{start + index}",
            emoji_kind="service",
            emoji_key=service
        )
        for index, service in enumerate(page_services)
    ]
    if buttons:
        markup.add(*buttons)

    nav = []
    if page > 0:
        nav.append(inline_button("Back", callback_data=f"svcpage|{page - 1}", action_key="back"))
    if page < total_pages(len(SERVICE_OPTIONS), SERVICE_PAGE_SIZE) - 1:
        nav.append(inline_button("Next", callback_data=f"svcpage|{page + 1}", action_key="refresh"))
    if nav:
        markup.row(*nav)

    markup.add(inline_button("Custom Service", callback_data="svc|custom", action_key="custom"), row_width=1)
    return markup, page


def build_country_picker_markup(page=0):
    page = clamp_page(page, len(COUNTRY_OPTIONS), COUNTRY_PAGE_SIZE)
    start = page * COUNTRY_PAGE_SIZE
    end = start + COUNTRY_PAGE_SIZE
    page_countries = COUNTRY_OPTIONS[start:end]

    markup = LiveInlineKeyboardMarkup(row_width=2)
    buttons = [
        inline_button(
            country,
            callback_data=f"ctry|{start + index}",
            emoji_kind="country",
            emoji_key=country,
            show_fallback_emoji=False
        )
        for index, country in enumerate(page_countries)
    ]
    if buttons:
        markup.add(*buttons)

    nav = []
    if page > 0:
        nav.append(inline_button("Back", callback_data=f"cpage|{page - 1}", action_key="back"))
    if page < total_pages(len(COUNTRY_OPTIONS), COUNTRY_PAGE_SIZE) - 1:
        nav.append(inline_button("Next", callback_data=f"cpage|{page + 1}", action_key="refresh"))
    if nav:
        markup.row(*nav)

    markup.row(
        inline_button("Back to Services", callback_data="back_to_services", action_key="back"),
        inline_button("Custom Country", callback_data="ctry|custom", action_key="custom")
    )
    return markup, page


# =================  USER STOCK SERVICE → COUNTRY PICKERS =================

def get_service_token(service_name):
    return hashlib.sha1(f"svc:{normalize_emoji_key(service_name)}".encode("utf-8")).hexdigest()[:12]


def fetch_available_stock_groups(user_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if user_id is not None and not is_premium_user(user_id):
        if not get_bool_setting("free_used_numbers_enabled", True):
            rows = []
        else:
            cursor.execute(
                """SELECT n.filename, n.service, n.country, COUNT(*) FROM numbers n
                   LEFT JOIN file_settings fs ON fs.filename=n.filename
                   WHERE COALESCE(n.otp_received, 0)=0
                     AND COALESCE(fs.free_enabled, 0)=1
                   GROUP BY n.filename, n.service, n.country
                   ORDER BY n.service, n.country, n.filename"""
            )
            rows = cursor.fetchall()
    else:
        cursor.execute(
            """SELECT filename, service, country, COUNT(*) FROM numbers
               WHERE status='available' AND COALESCE(otp_received, 0)=0
               GROUP BY filename, service, country
               ORDER BY service, country, filename"""
        )
        rows = cursor.fetchall()
    conn.close()
    groups = []
    for filename, service, country, count in rows:
        service_name = display_service(service, filename)
        country_name = strip_country_flag(display_country(country, filename))
        groups.append({
            "filename": filename,
            "service": service_name,
            "service_key": normalize_emoji_key(service_name),
            "country": country_name,
            "country_key": normalize_emoji_key(country_name),
            "count": int(count or 0),
        })
    return groups


def get_available_service_rows(user_id=None):
    service_counts = {}
    service_names = {}
    for group in fetch_available_stock_groups(user_id):
        key = group["service_key"]
        service_counts[key] = service_counts.get(key, 0) + group["count"]
        service_names.setdefault(key, group["service"])
    rows = [(service_names[key], service_counts[key]) for key in service_counts]
    priority = {normalize_emoji_key(name): index for index, name in enumerate(SERVICE_PRIORITY_OPTIONS)}
    return sorted(rows, key=lambda item: (priority.get(normalize_emoji_key(item[0]), len(priority)), item[0].lower()))


def get_live_service_rows():
    service_counts = {}
    service_names = {}
    for item in sorted_live_traffic_items():
        service_name = clean_label(item.get("service"), "WhatsApp")
        key = normalize_emoji_key(service_name)
        service_counts[key] = service_counts.get(key, 0) + max(1, int(item.get("hit", 0) or 0))
        service_names.setdefault(key, service_name)
    rows = [(service_names[key], service_counts[key]) for key in service_counts]
    priority = {normalize_emoji_key(name): index for index, name in enumerate(SERVICE_PRIORITY_OPTIONS)}
    return sorted(rows, key=lambda item: (priority.get(normalize_emoji_key(item[0]), len(priority)), -item[1], item[0].lower()))


def resolve_available_service_token(token, user_id=None):
    for service_name, _count in get_live_service_rows():
        if get_service_token(service_name) == token:
            return service_name
    for service_name, _count in get_available_service_rows(user_id):
        if get_service_token(service_name) == token:
            return service_name
    return None


def get_live_range_rows_for_service(service_name):
    wanted_key = normalize_emoji_key(service_name)
    rows = []
    for item in sorted_live_traffic_items():
        service = clean_label(item.get("service"), "WhatsApp")
        if normalize_emoji_key(service) != wanted_key:
            continue
        country = strip_country_flag(clean_label(item.get("country"), "Unknown"))
        range_text = str(item.get("range", "")).strip()
        if not range_text:
            continue
        token = live_range_token(range_text, item.get("provider_key", "fastx"))
        rows.append({
            "token": token,
            "country": country,
            "range": range_text,
            "hit": int(item.get("hit", 0) or 0),
            "provider_name": clean_label(item.get("provider_name"), "API"),
        })
    return rows


def get_available_country_rows_for_service(service_name, user_id=None):
    wanted_key = normalize_emoji_key(service_name)
    country_map = {}
    for group in fetch_available_stock_groups(user_id):
        if group["service_key"] != wanted_key:
            continue
        ckey = group["country_key"]
        if ckey not in country_map:
            country_map[ckey] = {
                "country": group["country"],
                "count": 0,
                "filename": group["filename"],
            }
        country_map[ckey]["count"] += group["count"]
        # Keep the oldest/current grouped filename as the buy target.
        country_map[ckey].setdefault("filename", group["filename"])
    return sorted(country_map.values(), key=lambda item: item["country"].lower())


def build_user_service_stock_markup(page=0, user_id=None):
    services = get_live_service_rows() or get_available_service_rows(user_id)
    page = clamp_page(page, len(services), USER_SERVICE_PAGE_SIZE)
    start = page * USER_SERVICE_PAGE_SIZE
    end = start + USER_SERVICE_PAGE_SIZE

    markup = LiveInlineKeyboardMarkup(row_width=2)
    buttons = []
    for service_name, count in services[start:end]:
        buttons.append(
            inline_button(
                f"{service_name} ({count})",
                callback_data=f"ustocksvc|{get_service_token(service_name)}",
                emoji_kind="service",
                emoji_key=service_name,
                show_fallback_emoji=False
            )
        )
    if buttons:
        markup.add(*buttons)

    nav = []
    if page > 0:
        nav.append(inline_button("Back", callback_data=f"ustocksvcpage|{page - 1}", action_key="back"))
    if page < total_pages(len(services), USER_SERVICE_PAGE_SIZE) - 1:
        nav.append(inline_button("Next", callback_data=f"ustocksvcpage|{page + 1}", action_key="refresh"))
    if nav:
        markup.row(*nav)

    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    return markup, page, len(services)


def send_user_service_stock_picker(chat_id, page=0, edit_message=None, reply_to_message=None, user_id=None):
    user_id = user_id or chat_id
    refresh_live_traffic_cache()
    markup, page, total = build_user_service_stock_markup(page, user_id)
    if total <= 0:
        text = f"{live_action_html('warning')} No live numbers available right now."
    else:
        pages = total_pages(total, USER_SERVICE_PAGE_SIZE)
        text = (
            f"{live_action_html('get_number')} <b>Get Number</b>\n"
            f"{live_action_html('service')} <b>Select Service From Live Traffic:</b> "
            f"Page <b>{page + 1}/{pages}</b> — Total <b>{total}</b> services"
        )
    if edit_message is not None:
        return send_or_edit_message(chat_id, text, reply_markup=markup if total else None, parse_mode="HTML", edit_message=edit_message)
    if reply_to_message is not None:
        return bot.reply_to(reply_to_message, text, parse_mode="HTML", reply_markup=markup if total else None)
    return bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup if total else None)


def build_user_country_stock_markup(service_name, page=0, user_id=None):
    live_ranges = get_live_range_rows_for_service(service_name)
    if live_ranges:
        page = clamp_page(page, len(live_ranges), USER_LIVE_RANGE_PAGE_SIZE)
        start = page * USER_LIVE_RANGE_PAGE_SIZE
        end = start + USER_LIVE_RANGE_PAGE_SIZE
        service_token = get_service_token(service_name)

        markup = LiveInlineKeyboardMarkup(row_width=1)
        for item in live_ranges[start:end]:
            label = f"{item['country']} | {item['range']} | Hit {item['hit']}"
            if len(label) > 58:
                label = f"{item['country'][:18]} | {item['range'][:22]} | Hit {item['hit']}"
            markup.add(
                inline_button(
                    label,
                    callback_data=f"trafficget|{item['token']}",
                    emoji_kind="country",
                    emoji_key=item["country"],
                    show_fallback_emoji=False
                ),
                row_width=1,
            )

        nav = []
        if page > 0:
            nav.append(inline_button("Back", callback_data=f"ustockctrypage|{service_token}|{page - 1}", action_key="back"))
        if page < total_pages(len(live_ranges), USER_LIVE_RANGE_PAGE_SIZE) - 1:
            nav.append(inline_button("Next", callback_data=f"ustockctrypage|{service_token}|{page + 1}", action_key="refresh"))
        if nav:
            markup.row(*nav)

        markup.row(
            inline_button("Back to Services", callback_data="ustockback", action_key="back"),
            inline_button("Refresh", callback_data=f"ustockctrypage|{service_token}|{page}", action_key="refresh"),
            inline_button("Close", callback_data="close", action_key="close")
        )
        return markup, page, len(live_ranges)

    countries = get_available_country_rows_for_service(service_name, user_id)
    page = clamp_page(page, len(countries), USER_COUNTRY_PAGE_SIZE)
    start = page * USER_COUNTRY_PAGE_SIZE
    end = start + USER_COUNTRY_PAGE_SIZE
    service_token = get_service_token(service_name)

    markup = LiveInlineKeyboardMarkup(row_width=2)
    buttons = []
    for item in countries[start:end]:
        country_name = item["country"]
        buttons.append(
            inline_button(
                f"{country_name} ({item['count']})",
                callback_data=f"buy|{get_file_token(item['filename'])}",
                emoji_kind="country",
                emoji_key=country_name,
                show_fallback_emoji=False
            )
        )
    if buttons:
        markup.add(*buttons)

    nav = []
    if page > 0:
        nav.append(inline_button("Back", callback_data=f"ustockctrypage|{service_token}|{page - 1}", action_key="back"))
    if page < total_pages(len(countries), USER_COUNTRY_PAGE_SIZE) - 1:
        nav.append(inline_button("Next", callback_data=f"ustockctrypage|{service_token}|{page + 1}", action_key="refresh"))
    if nav:
        markup.row(*nav)

    markup.row(
        inline_button("Back to Services", callback_data="ustockback", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close")
    )
    return markup, page, len(countries)


def send_user_country_stock_picker(chat_id, service_name, page=0, edit_message=None, user_id=None):
    user_id = user_id or chat_id
    refresh_live_traffic_cache()
    markup, page, total = build_user_country_stock_markup(service_name, page, user_id)
    if total <= 0:
        text = (
            f"{live_action_html('warning')} Stock empty for "
            f"{live_service_html(service_name)} <b>{escape(service_name)}</b>."
        )
        return send_or_edit_message(chat_id, text, reply_markup=None, parse_mode="HTML", edit_message=edit_message)
    pages = total_pages(total, USER_COUNTRY_PAGE_SIZE)
    text = (
        f"{live_action_html('service')} <b>Service:</b> {live_service_html(service_name)} {escape(service_name)}\n"
        f"{live_action_html('country')} <b>Select Country / Range:</b> "
        f"Page <b>{page + 1}/{pages}</b> — Total <b>{total}</b> countries"
    )
    return send_or_edit_message(chat_id, text, reply_markup=markup, parse_mode="HTML", edit_message=edit_message)


# =================  DATABASE SETUP =================

def get_db_connection():
    return sqlite3.connect(DB_FILE_NUMBER, check_same_thread=False)


def heartbeat_worker():
    while True:
        try:
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as heartbeat:
                heartbeat.write(str(time.time()))
        except Exception as e:
            print(f"Heartbeat write failed: {e}")
        time.sleep(5)

def init_dbs():
    with db_lock:
        conn1 = get_db_connection()
        c1 = conn1.cursor()
        c1.execute('''CREATE TABLE IF NOT EXISTS numbers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        phone TEXT, filename TEXT,
                        service TEXT DEFAULT 'Unknown',
                        country TEXT DEFAULT 'Unknown',
                        status TEXT DEFAULT 'available', 
                        user_id INTEGER, otp_received INTEGER DEFAULT 0,
                        otp_time REAL DEFAULT 0,
                        assigned_time REAL DEFAULT 0,
                        premium_used INTEGER DEFAULT 0,
                        premium_user_id INTEGER,
                        premium_assigned_time REAL DEFAULT 0,
                        UNIQUE(phone, filename))''')
        columns = [row[1] for row in c1.execute("PRAGMA table_info(numbers)").fetchall()]
        if "service" not in columns:
            c1.execute("ALTER TABLE numbers ADD COLUMN service TEXT DEFAULT 'Unknown'")
        if "country" not in columns:
            c1.execute("ALTER TABLE numbers ADD COLUMN country TEXT DEFAULT 'Unknown'")
        if "assigned_time" not in columns:
            c1.execute("ALTER TABLE numbers ADD COLUMN assigned_time REAL DEFAULT 0")
        if "premium_used" not in columns:
            c1.execute("ALTER TABLE numbers ADD COLUMN premium_used INTEGER DEFAULT 0")
        if "premium_user_id" not in columns:
            c1.execute("ALTER TABLE numbers ADD COLUMN premium_user_id INTEGER")
        if "premium_assigned_time" not in columns:
            c1.execute("ALTER TABLE numbers ADD COLUMN premium_assigned_time REAL DEFAULT 0")
        
        c1.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        total_text_otps INTEGER DEFAULT 0,
                        total_voice_otps INTEGER DEFAULT 0
                     )''')
        user_columns = [row[1] for row in c1.execute("PRAGMA table_info(users)").fetchall()]
        if "total_text_otps" not in user_columns:
            c1.execute("ALTER TABLE users ADD COLUMN total_text_otps INTEGER DEFAULT 0")
        if "total_voice_otps" not in user_columns:
            c1.execute("ALTER TABLE users ADD COLUMN total_voice_otps INTEGER DEFAULT 0")
        if "plan" not in user_columns:
            c1.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")
        if "premium_until" not in user_columns:
            c1.execute("ALTER TABLE users ADD COLUMN premium_until REAL DEFAULT 0")
        if "username" not in user_columns:
            c1.execute("ALTER TABLE users ADD COLUMN username TEXT")
        if "banned" not in user_columns:
            c1.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        if "balance" not in user_columns:
            c1.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
        c1.execute('''CREATE TABLE IF NOT EXISTS bot_targets (
                        target_key TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        link TEXT NOT NULL,
                        required INTEGER DEFAULT 1,
                        otp_source INTEGER DEFAULT 0
                     )''')
        c1.executemany(
            """INSERT OR IGNORE INTO bot_targets
               (target_key, name, chat_id, link, required, otp_source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            DEFAULT_TARGETS
        )
        c1.execute('''CREATE TABLE IF NOT EXISTS app_settings (
                        setting_key TEXT PRIMARY KEY,
                        setting_value TEXT NOT NULL
                     )''')
        c1.execute('''CREATE TABLE IF NOT EXISTS api_providers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        base_url TEXT NOT NULL,
                        api_key TEXT NOT NULL,
                        enabled INTEGER DEFAULT 1,
                        service TEXT DEFAULT 'WhatsApp',
                        country TEXT DEFAULT 'Unknown',
                        range_prefix TEXT DEFAULT '',
                        kind TEXT DEFAULT 'fastx',
                        created_at REAL DEFAULT 0
                     )''')
        c1.execute('''CREATE TABLE IF NOT EXISTS file_settings (
                        filename TEXT PRIMARY KEY,
                        free_enabled INTEGER DEFAULT 0
                     )''')
        c1.execute('''CREATE TABLE IF NOT EXISTS subscription_requests (
                        request_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        method TEXT NOT NULL,
                        payment_identifier TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        requested_at REAL DEFAULT 0,
                        decided_at REAL DEFAULT 0,
                        admin_id INTEGER,
                        days INTEGER DEFAULT 0
                     )''')
        c1.execute('''CREATE TABLE IF NOT EXISTS number_assignments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        number_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        premium INTEGER DEFAULT 0,
                        assigned_time REAL DEFAULT 0,
                        UNIQUE(number_id, user_id, premium)
                     )''')
        c1.execute('''CREATE TABLE IF NOT EXISTS auto_payment_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        txid TEXT UNIQUE,
                        method TEXT,
                        amount_bdt REAL DEFAULT 0,
                        sender TEXT,
                        sms_text TEXT,
                        matched_request_id TEXT,
                        status TEXT DEFAULT 'received',
                        created_at REAL DEFAULT 0
                     )''')
        c1.executemany(
            "INSERT OR IGNORE INTO app_settings (setting_key, setting_value) VALUES (?, ?)",
            [
                ("proxy_link", "https://t.me/ProxyHub_BD_BOT"),
                ("vpn_link", "https://t.me/SOHAG_BD_SHOP_BOT"),
                ("premium_link", "https://t.me/SOHAG_BD_SHOP_BOT"),
                ("support_link", "https://t.me/SOHAG_BD_SHOP_BOT"),
                ("central_payment_bot_link", ""),
                ("central_payment_client_id", ""),
                ("free_used_numbers_enabled", "1"),
                ("premium_first_stock_enabled", "1"),
                ("payment_bkash_number", "Not set"),
                ("payment_nagad_number", "Not set"),
                ("payment_rocket_number", "Not set"),
                ("payment_binance_number", "Not set"),
                ("payment_plan_bdt", ""),
                ("payment_plan_days", ""),
                ("auto_payment_bridge_enabled", "1"),
                ("auto_payment_bridge_secret", hashlib.sha1(f"{API_TOKEN}|{ADMIN_ID}|auto-payment".encode("utf-8")).hexdigest()[:24]),
                ("auto_payment_bridge_port", str(AUTO_PAYMENT_BRIDGE_PORT)),
                ("binance_auto_verify_enabled", "1"),
                ("binance_api_key", ""),
                ("binance_api_secret", ""),
                ("binance_id", ""),
                ("binance_payment_currency", "USDT"),
                ("binance_verify_window_minutes", "15"),
                ("binance_plan_usdt", ""),
                ("binance_plan_days", ""),
                ("free_numbers_per_assignment", str(FREE_NUMBERS_PER_ASSIGNMENT)),
                ("premium_numbers_per_assignment", str(NUMBERS_PER_ASSIGNMENT)),
                ("free_change_number_cooldown", str(FREE_CHANGE_NUMBER_COOLDOWN)),
                ("api_sync_enabled", env_default("OTP_API_SYNC_ENABLED", "1")),
                ("api_sync_interval_seconds", env_default("OTP_API_SYNC_INTERVAL", "20")),
                ("api_agent_enabled", env_default("AGENT_API_ENABLED", "0")),
                ("api_agent_base_url", env_default("AGENT_API_BASE_URL", DEFAULT_AGENT_BASE_URL)),
                ("api_agent_key", env_default("AGENT_API_KEY", "")),
                ("api_agent_cli", env_default("AGENT_API_CLI", "")),
                ("api_agent_service", env_default("AGENT_API_SERVICE", "WhatsApp")),
                ("api_agent_country", env_default("AGENT_API_COUNTRY", "Unknown")),
                ("api_fastx_enabled", env_default("FASTX_API_ENABLED", "1")),
                ("api_fastx_base_url", env_default("FASTX_API_BASE_URL", DEFAULT_FASTX_BASE_URL)),
                ("api_fastx_key", env_default("FASTX_API_KEY", DEFAULT_FASTX_API_KEY)),
                ("api_fastx_range", env_default("FASTX_API_RANGE", "")),
                ("api_fastx_service", env_default("FASTX_API_SERVICE", "WhatsApp")),
                ("api_fastx_country", env_default("FASTX_API_COUNTRY", "Unknown")),
                ("api_last_agent_since", ""),
            ]
        )
        api_default_updates = [
            ("api_sync_enabled", env_default("OTP_API_SYNC_ENABLED", "1"), True),
            ("api_fastx_enabled", env_default("FASTX_API_ENABLED", "1"), True),
            ("api_fastx_base_url", env_default("FASTX_API_BASE_URL", DEFAULT_FASTX_BASE_URL), False),
            ("api_fastx_key", env_default("FASTX_API_KEY", DEFAULT_FASTX_API_KEY), False),
            ("api_fastx_service", env_default("FASTX_API_SERVICE", "WhatsApp"), False),
            ("api_fastx_country", env_default("FASTX_API_COUNTRY", "Unknown"), False),
            ("api_agent_base_url", env_default("AGENT_API_BASE_URL", DEFAULT_AGENT_BASE_URL), False),
            ("api_agent_service", env_default("AGENT_API_SERVICE", "WhatsApp"), False),
        ]
        for setting_key, setting_value, force_on in api_default_updates:
            if force_on:
                c1.execute(
                    "UPDATE app_settings SET setting_value=? WHERE setting_key=? AND COALESCE(setting_value, '') IN ('', '0', 'false', 'off')",
                    (setting_value, setting_key),
                )
            else:
                c1.execute(
                    "UPDATE app_settings SET setting_value=? WHERE setting_key=? AND COALESCE(setting_value, '')=''",
                    (setting_value, setting_key),
                )
        c1.execute(
            """UPDATE numbers
               SET premium_used=1, premium_user_id=user_id
               WHERE status='taken'
                 AND COALESCE(premium_used, 0)=0
                 AND user_id IN (SELECT user_id FROM users WHERE plan='premium')"""
        )
        conn1.commit(); conn1.close()
        init_payment_system(DB_FILE_NUMBER)

init_dbs()


def normalize_chat_id(value):
    raw = str(value or "").strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def derive_public_target_from_link(link):
    raw = str(link or "").strip()
    match = re.match(r"^https?://t\.me/([A-Za-z0-9_]{5,})/?$", raw)
    return f"@{match.group(1)}" if match else ""


def normalize_public_username(value):
    raw = str(value or "").strip()
    if raw.lower() in {"off", "none", "disable", "disabled"}:
        return ""
    raw = raw.replace("https://t.me/", "").replace("http://t.me/", "").strip().strip("/")
    raw = raw[1:] if raw.startswith("@") else raw
    return raw if re.fullmatch(r"[A-Za-z0-9_]{5,}", raw) else ""


def get_app_setting(setting_key, default=""):
    conn = get_db_connection()
    row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (setting_key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_app_setting(setting_key, setting_value):
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO app_settings (setting_key, setting_value)
           VALUES (?, ?)
           ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
        (setting_key, setting_value)
    )
    conn.commit()
    conn.close()


def get_bool_setting(setting_key, default=True):
    fallback = "1" if default else "0"
    return str(get_app_setting(setting_key, fallback)).strip().lower() in {"1", "true", "yes", "on"}


def set_bool_setting(setting_key, enabled):
    set_app_setting(setting_key, "1" if enabled else "0")


def get_int_setting(setting_key, default, min_value=1, max_value=1000):
    raw = str(get_app_setting(setting_key, str(default)) or "").strip()
    try:
        value = int(float(raw))
    except Exception:
        value = int(default)
    value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


def get_api_sync_interval():
    return get_int_setting("api_sync_interval_seconds", 20, 5, 3600)


def get_api_clients():
    return {
        "agent": OtpApiClient(
            "agent",
            get_app_setting("api_agent_base_url", DEFAULT_AGENT_BASE_URL),
            get_app_setting("api_agent_key", ""),
            get_app_setting("api_agent_service", "WhatsApp"),
            get_app_setting("api_agent_country", "Unknown"),
        ),
        "fastx": OtpApiClient(
            "fastx",
            get_app_setting("api_fastx_base_url", DEFAULT_FASTX_BASE_URL),
            get_app_setting("api_fastx_key", ""),
            get_app_setting("api_fastx_service", "WhatsApp"),
            get_app_setting("api_fastx_country", "Unknown"),
        ),
    }


def get_extra_api_providers(enabled_only=False):
    conn = get_db_connection()
    query = """SELECT id, name, base_url, api_key, enabled, service, country, range_prefix, kind
               FROM api_providers"""
    params = []
    if enabled_only:
        query += " WHERE COALESCE(enabled, 0)=1"
    query += " ORDER BY id ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    providers = []
    for row in rows:
        provider_id, name, base_url, api_key, enabled, service, country, range_prefix, kind = row
        providers.append({
            "id": int(provider_id),
            "key": f"extra:{int(provider_id)}",
            "name": clean_label(name, f"API-{provider_id}"),
            "base_url": str(base_url or "").strip(),
            "api_key": str(api_key or "").strip(),
            "enabled": bool(enabled),
            "service": clean_label(service, "WhatsApp"),
            "country": clean_label(country, "Unknown"),
            "range": str(range_prefix or "").strip(),
            "kind": str(kind or "fastx").strip().lower() or "fastx",
        })
    return providers


def count_extra_api_providers():
    conn = get_db_connection()
    total = conn.execute("SELECT COUNT(*) FROM api_providers").fetchone()[0]
    conn.close()
    return int(total or 0)


def find_extra_api_provider(base_url, api_key):
    wanted_url = str(base_url or "").strip().rstrip("/")
    wanted_key = str(api_key or "").strip()
    if not wanted_url or not wanted_key:
        return None
    for provider in get_extra_api_providers(enabled_only=False):
        if provider["base_url"].rstrip("/") == wanted_url and provider["api_key"] == wanted_key:
            return provider
    return None


def get_fastx_provider_configs(enabled_only=True):
    providers = []
    if not enabled_only or api_provider_ready("fastx"):
        providers.append({
            "key": "fastx",
            "name": "FastX",
            "base_url": get_app_setting("api_fastx_base_url", DEFAULT_FASTX_BASE_URL),
            "api_key": get_app_setting("api_fastx_key", ""),
            "enabled": get_bool_setting("api_fastx_enabled", False),
            "service": get_app_setting("api_fastx_service", "WhatsApp"),
            "country": get_app_setting("api_fastx_country", "Unknown"),
            "range": get_app_setting("api_fastx_range", ""),
            "kind": "fastx",
        })
    for provider in get_extra_api_providers(enabled_only=enabled_only):
        if provider["base_url"] and provider["api_key"]:
            providers.append(provider)
    return providers


def get_api_provider_by_key(provider_key):
    if provider_key == "fastx":
        return get_fastx_provider_configs(enabled_only=False)[0]
    if str(provider_key or "").startswith("extra:"):
        wanted = str(provider_key).split(":", 1)[1]
        for provider in get_extra_api_providers(enabled_only=False):
            if str(provider["id"]) == wanted:
                return provider
    return None


def api_provider_token(provider_id):
    return hashlib.sha1(f"api-provider:{provider_id}".encode("utf-8")).hexdigest()[:12]


def resolve_extra_api_provider_token(token):
    for provider in get_extra_api_providers(enabled_only=False):
        if api_provider_token(provider["id"]) == token:
            return provider
    return None


def build_fastx_client(provider):
    return OtpApiClient(
        provider.get("name") or "FastX",
        provider.get("base_url", ""),
        provider.get("api_key", ""),
        provider.get("service", "WhatsApp"),
        provider.get("country", "Unknown"),
    )


def save_extra_api_provider(name, base_url, api_key, service="WhatsApp", country="Unknown", range_prefix=""):
    existing = find_extra_api_provider(base_url, api_key)
    if existing:
        update_extra_api_provider(existing["id"], "name", clean_label(name, existing["name"]))
        update_extra_api_provider(existing["id"], "service", clean_label(service, existing["service"]))
        update_extra_api_provider(existing["id"], "country", clean_label(country, existing["country"]))
        update_extra_api_provider(existing["id"], "range_prefix", str(range_prefix or "").strip())
        update_extra_api_provider(existing["id"], "enabled", 1)
        return existing["id"]
    if count_extra_api_providers() >= MAX_EXTRA_API_PROVIDERS:
        raise ValueError(f"Maximum {MAX_EXTRA_API_PROVIDERS} extra APIs can be added.")
    conn = get_db_connection()
    with db_lock:
        cursor = conn.execute(
            """INSERT INTO api_providers
               (name, base_url, api_key, enabled, service, country, range_prefix, kind, created_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, 'fastx', ?)""",
            (
                clean_label(name, "API"),
                str(base_url or "").strip(),
                str(api_key or "").strip(),
                clean_label(service, "WhatsApp"),
                clean_label(country, "Unknown"),
                str(range_prefix or "").strip(),
                time.time(),
            ),
        )
        provider_id = cursor.lastrowid
        conn.commit()
    conn.close()
    return provider_id


def update_extra_api_provider(provider_id, field, value):
    allowed = {"name", "base_url", "api_key", "service", "country", "range_prefix", "enabled"}
    if field not in allowed:
        return False
    conn = get_db_connection()
    with db_lock:
        conn.execute(f"UPDATE api_providers SET {field}=? WHERE id=?", (value, int(provider_id)))
        conn.commit()
    conn.close()
    return True


def delete_extra_api_provider(provider_id):
    conn = get_db_connection()
    with db_lock:
        conn.execute("DELETE FROM api_providers WHERE id=?", (int(provider_id),))
        conn.commit()
    conn.close()


def api_provider_ready(provider):
    client = get_api_clients()[provider]
    return client.enabled() and get_bool_setting(f"api_{provider}_enabled", False)


def get_free_numbers_per_assignment():
    return get_int_setting("free_numbers_per_assignment", FREE_NUMBERS_PER_ASSIGNMENT, 1, 20)


def get_premium_numbers_per_assignment():
    return get_int_setting("premium_numbers_per_assignment", NUMBERS_PER_ASSIGNMENT, 1, 50)


def get_free_change_number_cooldown():
    return get_int_setting("free_change_number_cooldown", FREE_CHANGE_NUMBER_COOLDOWN, 0, 86400)


def format_seconds(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remain = seconds % 60
    return f"{minutes}m {remain}s" if remain else f"{minutes}m"


def parse_duration_seconds(text):
    raw = str(text or "").strip().lower()
    match = re.search(r"(\d+)", raw)
    if not match:
        return None
    value = int(match.group(1))
    if any(unit in raw for unit in ("m", "min", "minute", "minutes", "মিনিট")):
        return value * 60
    return value


def is_file_free_enabled(filename):
    conn = get_db_connection()
    row = conn.execute("SELECT free_enabled FROM file_settings WHERE filename=?", (filename,)).fetchone()
    conn.close()
    return bool(row and int(row[0] or 0))


def set_file_free_enabled(filename, enabled):
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO file_settings (filename, free_enabled)
           VALUES (?, ?)
           ON CONFLICT(filename) DO UPDATE SET free_enabled=excluded.free_enabled""",
        (filename, 1 if enabled else 0)
    )
    conn.commit()
    conn.close()


def get_file_free_counts(filename):
    conn = get_db_connection()
    row = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN COALESCE(otp_received, 0)=0 THEN 1 ELSE 0 END)
           FROM numbers WHERE filename=?""",
        (filename,)
    ).fetchone()
    conn.close()
    if not row:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def build_file_free_access_markup(filename):
    enabled = is_file_free_enabled(filename)
    markup = LiveInlineKeyboardMarkup(row_width=1)
    markup.add(
        inline_button(
            "Turn OFF Free Users" if enabled else "Turn ON Free Users",
            callback_data=f"quickfreefile|{get_file_token(filename)}",
            action_key="refresh"
        )
    )
    return markup


def build_file_free_access_message(filename, header_text=None):
    service_name, country_name = parse_filename_info(filename)
    total_count, free_ready_count = get_file_free_counts(filename)
    status = "ON" if is_file_free_enabled(filename) else "OFF"
    lines = []
    if header_text:
        lines.append(header_text)
        lines.append("")
    lines.extend([
        f"{live_action_html('manage')} <b>Free User Access</b>",
        f"{live_service_html(service_name)} <b>Service:</b> {escape(service_name)}",
        f"{live_country_html(country_name)} <b>Country:</b> {escape(strip_country_flag(country_name))}",
        f"<b>Status:</b> {status}",
        f"<b>Free ready:</b> {free_ready_count}/{total_count}",
        "If ON, free users can receive numbers from this file until OTP arrives."
    ])
    return "\n".join(lines)


def track_user_profile(user):
    if not user:
        return
    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO users (user_id, username)
           VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=COALESCE(excluded.username, username)""",
        (int(user.id), username or None)
    )
    conn.commit()
    conn.close()


def resolve_user_reference(reference):
    raw = str(reference or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    username = raw.lstrip("@").lower()
    conn = get_db_connection()
    row = conn.execute(
        "SELECT user_id FROM users WHERE LOWER(COALESCE(username, ''))=?",
        (username,)
    ).fetchone()
    conn.close()
    return int(row[0]) if row else None


def is_user_banned(user_id):
    conn = get_db_connection()
    row = conn.execute("SELECT banned FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    conn.close()
    return bool(row and int(row[0] or 0))


def set_user_banned(user_id, banned=True):
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO users (user_id, banned, plan, premium_until)
           VALUES (?, ?, 'free', 0)
           ON CONFLICT(user_id) DO UPDATE SET banned=excluded.banned, plan='free', premium_until=0""",
        (int(user_id), 1 if banned else 0)
    )
    conn.commit()
    conn.close()


def send_banned_notice(chat_id):
    bot.send_message(chat_id, f"{live_action_html('lock')} Your account is banned.", parse_mode="HTML")


def is_premium_user(user_id):
    if int(user_id) == ADMIN_ID:
        return True
    conn = get_db_connection()
    row = conn.execute(
        "SELECT plan, premium_until FROM users WHERE user_id=?",
        (int(user_id),)
    ).fetchone()
    conn.close()
    if not row:
        return False
    plan, premium_until = row
    if str(plan or "").lower() != "premium":
        return False
    premium_until = float(premium_until or 0)
    return premium_until <= 0 or premium_until > time.time()


def activate_premium_user(user_id, days=30):
    premium_until = 0 if int(days or 0) <= 0 else time.time() + (int(days) * 86400)
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO users (user_id, plan, premium_until)
           VALUES (?, 'premium', ?)
           ON CONFLICT(user_id) DO UPDATE SET plan='premium', premium_until=excluded.premium_until""",
        (int(user_id), premium_until)
    )
    conn.commit()
    conn.close()
    return premium_until


def activate_premium_user_from_time(user_id, days=30, start_time=None):
    start_time = float(start_time or time.time())
    premium_until = 0 if int(days or 0) <= 0 else start_time + (int(days) * 86400)
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO users (user_id, plan, premium_until)
           VALUES (?, 'premium', ?)
           ON CONFLICT(user_id) DO UPDATE SET plan='premium', premium_until=excluded.premium_until""",
        (int(user_id), premium_until)
    )
    conn.commit()
    conn.close()
    return premium_until


def add_user_balance(user_id, amount):
    amount = float(amount or 0)
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO users (user_id, balance)
           VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET balance=COALESCE(balance, 0)+excluded.balance""",
        (int(user_id), amount)
    )
    conn.commit()
    conn.close()


def get_user_balance(user_id):
    conn = get_db_connection()
    row = conn.execute("SELECT COALESCE(balance, 0) FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    conn.close()
    return float(row[0] or 0) if row else 0.0


def remove_premium_user(user_id):
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO users (user_id, plan, premium_until)
           VALUES (?, 'free', 0)
           ON CONFLICT(user_id) DO UPDATE SET plan='free', premium_until=0""",
        (int(user_id),)
    )
    conn.commit()
    conn.close()


def format_subscription_status(user_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT plan, premium_until, total_text_otps, total_voice_otps FROM users WHERE user_id=?",
        (int(user_id),)
    ).fetchone()
    conn.close()
    if not row:
        return "Free", "Not registered", 0, 0
    plan, premium_until, text_otps, voice_otps = row
    if str(plan or "").lower() == "premium" and is_premium_user(user_id):
        premium_until = float(premium_until or 0)
        if premium_until <= 0:
            expire_text = "Lifetime"
        else:
            seconds_left = max(0, int(premium_until - time.time()))
            days_left = seconds_left // 86400
            hours_left = (seconds_left % 86400) // 3600
            expire_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(premium_until))
            expire_text = f"{days_left} days {hours_left} hours left (until {expire_date})"
        return "Premium", expire_text, int(text_otps or 0), int(voice_otps or 0)
    return "Free", "No active premium", int(text_otps or 0), int(voice_otps or 0)


def payment_method_label(method):
    return PAYMENT_METHODS.get(method, {}).get("label", str(method or "").title())


def get_payment_plan_values():
    amount_text = str(get_app_setting("payment_plan_bdt", "") or "").strip()
    days_text = str(get_app_setting("payment_plan_days", "") or "").strip()
    return amount_text, days_text


def get_binance_plan_values():
    amount_text = str(get_app_setting("binance_plan_usdt", "") or "").strip()
    days_text = str(get_app_setting("binance_plan_days", "") or "").strip()
    if not amount_text or not days_text:
        bdt_amount, bdt_days = get_payment_plan_values()
        if bdt_amount and not amount_text:
            try:
                amount_text = f"{float(str(bdt_amount).replace(',', '')) / BDT_PER_USD:.2f}"
            except Exception:
                amount_text = ""
        if bdt_days and not days_text:
            days_text = bdt_days
    return amount_text, days_text


def build_payment_plan_lines(method):
    lines = []
    if method == "binance":
        amount_text, days_text = get_binance_plan_values()
        if amount_text:
            lines.append(f"<b>Rate:</b> {escape(amount_text)} {escape(get_app_setting('binance_payment_currency', 'USDT') or 'USDT')} = {escape(days_text or '0')} days")
    else:
        amount_text, days_text = get_payment_plan_values()
        if amount_text:
            lines.append(f"<b>Amount:</b> {escape(amount_text)} BDT")
    if days_text:
        lines.append(f"<b>Subscription:</b> {escape(days_text)} days")
    return lines


def create_subscription_request(user_id, method, payment_identifier):
    request_id = hashlib.sha1(f"{user_id}|{method}|{payment_identifier}|{time.time()}".encode("utf-8")).hexdigest()[:12]
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO subscription_requests
           (request_id, user_id, method, payment_identifier, status, requested_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (request_id, int(user_id), method, payment_identifier, time.time())
    )
    conn.commit()
    conn.close()
    return request_id


def get_subscription_request(request_id):
    conn = get_db_connection()
    row = conn.execute(
        """SELECT request_id, user_id, method, payment_identifier, status, requested_at, days
           FROM subscription_requests WHERE request_id=?""",
        (request_id,)
    ).fetchone()
    conn.close()
    return row


def update_subscription_request_status(request_id, status, admin_id=None, days=0):
    conn = get_db_connection()
    conn.execute(
        """UPDATE subscription_requests
           SET status=?, admin_id=?, days=?, decided_at=?
           WHERE request_id=?""",
        (status, admin_id, int(days or 0), time.time(), request_id)
    )
    conn.commit()
    conn.close()


def mask_secret(value, keep=4):
    value = str(value or "").strip()
    if not value:
        return "Not set"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def normalize_payment_identifier(value):
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def detect_sms_payment_method(sender, sms_text):
    source = f"{sender or ''} {sms_text or ''}".lower()
    if "bkash" in source or "b-kash" in source:
        return "bkash"
    if "nagad" in source:
        return "nagad"
    if "rocket" in source or "dbbl" in source:
        return "rocket"
    return ""


def extract_sms_amount_bdt(sms_text):
    text = str(sms_text or "")
    patterns = [
        r"(?:tk|bdt|৳)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:tk|bdt|৳)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1).replace(",", ""))
        except Exception:
            return 0.0
    return 0.0


def extract_sms_txid_candidates(sms_text):
    text = str(sms_text or "")
    candidates = set()
    labeled_patterns = [
        r"(?:trxid|txnid|txnid|transaction\s*id|trans\s*id|ref(?:erence)?\s*id)\s*[:#\-]?\s*([A-Za-z0-9]{5,30})",
        r"(?:trx|txn|transaction)\s*[:#\-]?\s*([A-Za-z0-9]{5,30})",
    ]
    for pattern in labeled_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidates.add(normalize_payment_identifier(match.group(1)))
    for token in re.findall(r"\b[A-Za-z0-9]{7,24}\b", text):
        normalized = normalize_payment_identifier(token)
        if any(ch.isdigit() for ch in normalized) and any(ch.isalpha() for ch in normalized):
            candidates.add(normalized)
    return [candidate for candidate in candidates if candidate]


def get_pending_subscription_requests_for_auto(method=""):
    conn = get_db_connection()
    if method:
        rows = conn.execute(
            """SELECT request_id, user_id, method, payment_identifier
               FROM subscription_requests
               WHERE status='pending' AND method=?""",
            (method,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT request_id, user_id, method, payment_identifier
               FROM subscription_requests
               WHERE status='pending'"""
        ).fetchall()
    conn.close()
    return rows


def save_auto_payment_event(txid, method, amount_bdt, sender, sms_text, request_id="", status="received"):
    conn = get_db_connection()
    conn.execute(
        """INSERT OR IGNORE INTO auto_payment_events
           (txid, method, amount_bdt, sender, sms_text, matched_request_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (txid, method, float(amount_bdt or 0), sender, sms_text, request_id, status, time.time())
    )
    conn.commit()
    conn.close()


def is_auto_payment_txid_used(txid):
    normalized = normalize_payment_identifier(txid)
    if not normalized:
        return False
    conn = get_db_connection()
    row = conn.execute(
        "SELECT status FROM auto_payment_events WHERE txid=? AND status='approved'",
        (normalized,)
    ).fetchone()
    conn.close()
    return bool(row)


def approve_subscription_request_auto(request_id, txid, method, amount_bdt, sender, sms_text):
    row = get_subscription_request(request_id)
    if not row:
        return False, "Request not found"
    _request_id, user_id, _method, payment_identifier, status, _requested_at, _old_days = row
    if status != "pending":
        return False, "Request is not pending"

    amount_text, days_text = get_payment_plan_values()
    required_amount = 0.0
    if amount_text:
        try:
            required_amount = float(str(amount_text).replace(",", ""))
        except Exception:
            required_amount = 0.0
    if required_amount and float(amount_bdt or 0) + 0.001 < required_amount:
        save_auto_payment_event(txid, method, amount_bdt, sender, sms_text, request_id, "amount_low")
        return False, "Amount is lower than plan"

    days = int(days_text) if str(days_text or "").isdigit() else 30
    premium_until = activate_premium_user(user_id, days)
    update_subscription_request_status(request_id, "approved", 0, days)
    save_auto_payment_event(txid, method, amount_bdt, sender, sms_text, request_id, "approved")
    expire_text = "Lifetime" if premium_until <= 0 else time.strftime("%Y-%m-%d %H:%M", time.localtime(premium_until))

    try:
        bot.send_message(
            user_id,
            (
                f"{live_action_html('success')} Your premium subscription is active.\n"
                f"Plan: <b>Premium</b>\n"
                f"Until: <b>{escape(expire_text)}</b>"
            ),
            parse_mode="HTML",
            reply_markup=build_main_menu_markup(user_id)
        )
    except Exception:
        pass
    try:
        bot.send_message(
            ADMIN_ID,
            (
                f"{live_action_html('success')} <b>Auto Payment Approved</b>\n\n"
                f"Request ID: <code>{escape(request_id)}</code>\n"
                f"User ID: <code>{user_id}</code>\n"
                f"Method: <b>{escape(payment_method_label(method))}</b>\n"
                f"TxID: <code>{escape(txid)}</code>\n"
                f"Amount: <b>{escape(str(amount_bdt))} BDT</b>\n"
                f"Days: <b>{days}</b>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass
    return True, f"Approved {request_id}"


def get_binance_auto_settings():
    return {
        "enabled": get_bool_setting("binance_auto_verify_enabled", True),
        "api_key": get_app_setting("binance_api_key", ""),
        "api_secret": get_app_setting("binance_api_secret", ""),
        "binance_id": get_app_setting("binance_id", ""),
        "currency": str(get_app_setting("binance_payment_currency", "USDT") or "USDT").strip().upper(),
        "window_minutes": get_int_setting("binance_verify_window_minutes", 15, 1, 1440),
    }


def payment_system_active_text():
    status = get_license_status(DB_FILE_NUMBER)
    if status["active"]:
        until_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(status["active_until"]))
        days_left = status["seconds_left"] // 86400
        return f"ACTIVE until {until_text} ({days_left} days left)"
    return "INACTIVE"


def get_binance_required_amount():
    amount_text, _days_text = get_binance_plan_values()
    if not amount_text:
        return 0.0
    try:
        return round(float(str(amount_text).replace(",", "")), 8)
    except Exception:
        return 0.0


def get_binance_plan_days(default=30):
    _amount_text, days_text = get_binance_plan_values()
    return int(days_text) if str(days_text or "").isdigit() else int(default)


def calculate_binance_days_from_amount(paid_amount):
    base_amount_text, base_days_text = get_binance_plan_values()
    try:
        base_amount = float(str(base_amount_text).replace(",", ""))
        base_days = int(base_days_text)
        paid_amount = float(paid_amount or 0)
    except Exception:
        return 0
    if base_amount <= 0 or base_days <= 0 or paid_amount <= 0:
        return 0
    return int((paid_amount / base_amount) * base_days)


def binance_signed_request(path, params=None):
    settings = get_binance_auto_settings()
    api_key = settings["api_key"].strip()
    api_secret = settings["api_secret"].strip()
    if not api_key or not api_secret:
        raise RuntimeError("Binance API key/secret is not set")
    payload = dict(params or {})
    payload.setdefault("recvWindow", 5000)
    payload["timestamp"] = int(time.time() * 1000)
    query = urlencode(payload, doseq=True)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com{path}?{query}&signature={signature}"
    request = Request(url, headers={"X-MBX-APIKEY": api_key, "User-Agent": "PremiumBot/1.0"})
    with urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8", "ignore")
    return json.loads(body or "{}")


def find_dicts_containing_order_id(data, order_id):
    needle = normalize_payment_identifier(order_id)
    found = []

    def walk(item):
        if isinstance(item, dict):
            values_blob = normalize_payment_identifier(" ".join(str(value) for value in item.values() if not isinstance(value, (dict, list))))
            if needle and needle in values_blob:
                found.append(item)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(data)
    return found


def dict_contains_currency(item, currency):
    currency = str(currency or "").upper()
    if not currency:
        return True
    blob = str(item).upper()
    return currency in blob


def extract_amounts_from_dict(item):
    amount_keys = {
        "amount", "orderamount", "receiveamount", "receivedamount", "obtainamount",
        "quantity", "qty", "total", "totalprice", "payamount", "paymentamount"
    }
    amounts = []

    def walk(value, key=""):
        normalized_key = normalize_emoji_key(key)
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                walk(child_value, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        else:
            if normalized_key.replace(" ", "") in amount_keys:
                try:
                    amounts.append(float(str(value).replace(",", "")))
                except Exception:
                    pass

    walk(item)
    return amounts


def binance_payment_matches(order_id, currency, window_minutes):
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(window_minutes or 15) * 60 * 1000
    endpoints = [
        ("/sapi/v1/pay/transactions", {"startTime": start_ms, "endTime": now_ms, "limit": 100}),
        ("/sapi/v1/pay/transactions", {"startTimestamp": start_ms, "endTimestamp": now_ms, "limit": 100}),
        ("/sapi/v1/c2c/orderMatch/listUserOrderHistory", {"startTimestamp": start_ms, "endTimestamp": now_ms, "page": 1, "rows": 100}),
        ("/sapi/v1/c2c/orderMatch/listUserOrderHistory", {"tradeType": "SELL", "startTimestamp": start_ms, "endTimestamp": now_ms, "page": 1, "rows": 100}),
    ]
    last_error = ""
    for path, params in endpoints:
        try:
            data = binance_signed_request(path, params)
        except Exception as e:
            last_error = str(e)
            continue
        candidates = find_dicts_containing_order_id(data, order_id)
        for item in candidates:
            if not dict_contains_currency(item, currency):
                continue
            amounts = extract_amounts_from_dict(item)
            paid_amount = max(amounts) if amounts else 0.0
            return "matched", item, paid_amount
    return "not_found", last_error or "Order ID not found in Binance history", 0.0


def try_auto_approve_binance_request(request_id):
    if not is_payment_system_active(DB_FILE_NUMBER):
        return False, "Payment system license is inactive"
    row = get_subscription_request(request_id)
    if not row:
        return False, "Request not found"
    _request_id, user_id, method, payment_identifier, status, _requested_at, _old_days = row
    if method != "binance" or status != "pending":
        return False, "Not a pending Binance request"
    settings = get_binance_auto_settings()
    if not settings["enabled"]:
        return False, "Binance auto verify is OFF"
    if not settings["api_key"] or not settings["api_secret"]:
        return False, "Binance API key/secret is not set"
    order_id = normalize_payment_identifier(payment_identifier)
    if is_auto_payment_txid_used(order_id):
        save_auto_payment_event(order_id, "binance", get_binance_required_amount(), "binance_api", "", request_id, "duplicate")
        return False, "Order ID already approved"

    required_amount = get_binance_required_amount()
    match_status, detail, paid_amount = binance_payment_matches(order_id, settings["currency"], settings["window_minutes"])
    if match_status != "matched":
        save_auto_payment_event(order_id, "binance", required_amount, "binance_api", str(detail), request_id, "not_found")
        return False, str(detail)

    days = calculate_binance_days_from_amount(paid_amount)
    if days <= 0:
        save_auto_payment_event(order_id, "binance", paid_amount, "binance_api", json.dumps(detail, ensure_ascii=False)[:1000], request_id, "amount_low")
        update_subscription_request_status(request_id, "rejected", 0, 0)
        try:
            bot.send_message(
                user_id,
                (
                    f"{live_action_html('warning')} Binance payment rejected.\n"
                    f"Reason: Amount is too low for a subscription day.\n"
                    f"Paid: <b>{paid_amount} {escape(settings['currency'])}</b>\n"
                    f"Order ID: <code>{escape(order_id)}</code>"
                ),
                parse_mode="HTML",
                reply_markup=build_main_menu_markup(user_id)
            )
        except Exception:
            pass
        return False, "Amount is too low for a subscription day"

    premium_until = activate_premium_user(user_id, days)
    update_subscription_request_status(request_id, "approved", 0, days)
    save_auto_payment_event(order_id, "binance", paid_amount, "binance_api", json.dumps(detail, ensure_ascii=False)[:1000], request_id, "approved")
    expire_text = "Lifetime" if premium_until <= 0 else time.strftime("%Y-%m-%d %H:%M", time.localtime(premium_until))
    bot.send_message(
        user_id,
        (
            f"{live_action_html('success')} Your premium subscription is active.\n"
            f"Plan: <b>Premium</b>\n"
            f"Until: <b>{escape(expire_text)}</b>"
        ),
        parse_mode="HTML",
        reply_markup=build_main_menu_markup(user_id)
    )
    bot.send_message(
        ADMIN_ID,
        (
            f"{live_action_html('success')} <b>Binance Auto Approved</b>\n\n"
            f"Request ID: <code>{escape(request_id)}</code>\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Order ID: <code>{escape(order_id)}</code>\n"
            f"Paid: <b>{paid_amount} {escape(settings['currency'])}</b>\n"
            f"Days: <b>{days}</b>"
        ),
        parse_mode="HTML"
    )
    return True, "Approved"


def process_auto_payment_sms(sender, sms_text):
    if not is_payment_system_active(DB_FILE_NUMBER):
        return False, "Payment system license is inactive"
    method = detect_sms_payment_method(sender, sms_text)
    txids = extract_sms_txid_candidates(sms_text)
    amount_bdt = extract_sms_amount_bdt(sms_text)
    if not txids:
        save_auto_payment_event("", method, amount_bdt, sender, sms_text, "", "no_txid")
        return False, "No transaction ID found"

    pending_rows = get_pending_subscription_requests_for_auto(method)
    normalized_text = normalize_payment_identifier(sms_text)
    for request_id, _user_id, row_method, payment_identifier in pending_rows:
        wanted = normalize_payment_identifier(payment_identifier)
        if not wanted:
            continue
        matched_txid = wanted if wanted in normalized_text else ""
        if not matched_txid and wanted in txids:
            matched_txid = wanted
        if not matched_txid:
            continue
        if is_auto_payment_txid_used(matched_txid):
            save_auto_payment_event(matched_txid, row_method, amount_bdt, sender, sms_text, request_id, "duplicate")
            return False, "Transaction ID already approved"
        return approve_subscription_request_auto(request_id, matched_txid, row_method, amount_bdt, sender, sms_text)

    save_auto_payment_event(txids[0], method, amount_bdt, sender, sms_text, "", "unmatched")
    try:
        bot.send_message(
            ADMIN_ID,
            (
                f"{live_action_html('warning')} <b>Auto Payment SMS Unmatched</b>\n\n"
                f"Method: <b>{escape(payment_method_label(method) if method else 'Unknown')}</b>\n"
                f"TxID: <code>{escape(txids[0])}</code>\n"
                f"Amount: <b>{escape(str(amount_bdt))} BDT</b>\n"
                f"Sender: <code>{escape(str(sender or ''))}</code>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass
    return False, "No pending request matched"


class AutoPaymentSMSHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *args):
        return

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self):
        parsed = urlparse(self.path)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    data = json.loads(raw or "{}")
                    params.update({str(k): str(v) for k, v in data.items()})
                except Exception:
                    pass
            else:
                params.update({key: values[-1] for key, values in parse_qs(raw).items()})
        return parsed.path, params

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        path, params = self._read_payload()
        if path == "/license/redeem":
            expected_secret = get_app_setting("auto_payment_bridge_secret", "")
            supplied_secret = params.get("secret") or self.headers.get("X-Bridge-Secret", "")
            if expected_secret and supplied_secret != expected_secret:
                self._send_json(403, {"ok": False, "error": "bad_secret"})
                return
            ok, message, active_until = redeem_activation_key(DB_FILE_NUMBER, params.get("key", ""), ADMIN_ID)
            self._send_json(200 if ok else 400, {
                "ok": ok,
                "message": message,
                "active_until": active_until,
            })
            return
        if path == "/client/activate":
            expected_secret = get_app_setting("auto_payment_bridge_secret", "")
            supplied_secret = params.get("secret") or self.headers.get("X-Bridge-Secret", "")
            if expected_secret and supplied_secret != expected_secret:
                self._send_json(403, {"ok": False, "error": "bad_secret"})
                return
            try:
                user_id = int(params.get("user_id", "0"))
                days = int(float(params.get("days", "0")))
                paid_at = float(params.get("paid_at") or time.time())
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad_request"})
                return
            if user_id <= 0 or days <= 0:
                self._send_json(400, {"ok": False, "error": "invalid_user_or_days"})
                return
            premium_until = activate_premium_user_from_time(user_id, days, paid_at)
            expire_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(premium_until))
            try:
                bot.send_message(
                    user_id,
                    (
                        f"{live_action_html('success')} Your premium subscription is active.\n"
                        f"Plan: <b>Premium</b>\n"
                        f"Until: <b>{escape(expire_text)}</b>"
                    ),
                    parse_mode="HTML",
                    reply_markup=build_main_menu_markup(user_id)
                )
            except Exception:
                pass
            self._send_json(200, {"ok": True, "premium_until": premium_until})
            return
        if path == "/client/add_balance":
            expected_secret = get_app_setting("auto_payment_bridge_secret", "")
            supplied_secret = params.get("secret") or self.headers.get("X-Bridge-Secret", "")
            if expected_secret and supplied_secret != expected_secret:
                self._send_json(403, {"ok": False, "error": "bad_secret"})
                return
            try:
                user_id = int(params.get("user_id", "0"))
                amount = float(params.get("amount", "0"))
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad_request"})
                return
            if user_id <= 0 or amount <= 0:
                self._send_json(400, {"ok": False, "error": "invalid_user_or_amount"})
                return
            add_user_balance(user_id, amount)
            balance = get_user_balance(user_id)
            try:
                bot.send_message(
                    user_id,
                    (
                        f"{live_action_html('success')} Balance added successfully.\n"
                        f"Added: <b>{amount:.2f}</b>\n"
                        f"Current Balance: <b>{balance:.2f}</b>"
                    ),
                    parse_mode="HTML",
                    reply_markup=build_main_menu_markup(user_id)
                )
            except Exception:
                pass
            self._send_json(200, {"ok": True, "balance": balance})
            return
        if path == "/client/product_order":
            expected_secret = get_app_setting("auto_payment_bridge_secret", "")
            supplied_secret = params.get("secret") or self.headers.get("X-Bridge-Secret", "")
            if expected_secret and supplied_secret != expected_secret:
                self._send_json(403, {"ok": False, "error": "bad_secret"})
                return
            try:
                user_id = int(params.get("user_id", "0"))
                amount = float(params.get("amount", "0"))
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad_request"})
                return
            product_name = params.get("product", "Product")
            try:
                bot.send_message(
                    ADMIN_ID,
                    (
                        f"{live_action_html('broadcast')} <b>Paid Product Order</b>\n\n"
                        f"User: <code>{user_id}</code>\n"
                        f"Product: <b>{escape(product_name)}</b>\n"
                        f"Amount: <b>{amount:.2f}</b>"
                    ),
                    parse_mode="HTML"
                )
                bot.send_message(user_id, f"{live_action_html('success')} Product payment received.", parse_mode="HTML")
            except Exception:
                pass
            self._send_json(200, {"ok": True})
            return
        if path not in ("/sms", "/sms-payment", "/payment-sms"):
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        expected_secret = get_app_setting("auto_payment_bridge_secret", "")
        supplied_secret = params.get("secret") or self.headers.get("X-Bridge-Secret", "")
        if expected_secret and supplied_secret != expected_secret:
            self._send_json(403, {"ok": False, "error": "bad_secret"})
            return
        sender = params.get("sender") or params.get("from") or params.get("address") or ""
        sms_text = params.get("text") or params.get("body") or params.get("message") or ""
        ok, message = process_auto_payment_sms(sender, sms_text)
        self._send_json(200 if ok else 202, {"ok": ok, "message": message})


def start_auto_payment_bridge():
    if not get_bool_setting("auto_payment_bridge_enabled", True):
        print("Auto payment bridge disabled.")
        return
    port = get_int_setting("auto_payment_bridge_port", AUTO_PAYMENT_BRIDGE_PORT, 1024, 65535)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), AutoPaymentSMSHandler)
        print(f"Auto payment SMS bridge running on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"Auto payment bridge failed: {e}")


def get_target(target_key):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT target_key, name, chat_id, link, required, otp_source FROM bot_targets WHERE target_key=?",
        (target_key,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "key": row[0],
        "name": row[1],
        "chat_id": normalize_chat_id(row[2]),
        "link": row[3],
        "required": bool(row[4]),
        "otp_source": bool(row[5]),
    }


def get_required_groups():
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT target_key, name, chat_id, link, required, otp_source
           FROM bot_targets
           WHERE required=1 AND TRIM(link) <> ''
           ORDER BY rowid"""
    ).fetchall()
    conn.close()
    return [{
        "key": row[0],
        "name": row[1],
        "chat_id": normalize_chat_id(row[2]),
        "link": row[3],
        "required": bool(row[4]),
        "otp_source": bool(row[5]),
    } for row in rows]


def get_all_targets():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT target_key, name, chat_id, link, required, otp_source FROM bot_targets ORDER BY rowid"
    ).fetchall()
    conn.close()
    return [{
        "key": row[0],
        "name": row[1],
        "chat_id": normalize_chat_id(row[2]),
        "link": row[3],
        "required": bool(row[4]),
        "otp_source": bool(row[5]),
    } for row in rows]


def get_bot_id():
    global bot_id_cache
    if bot_id_cache:
        return bot_id_cache
    bot_id_cache = bot.get_me().id
    return bot_id_cache


def resolve_group_chat_ref(group):
    return derive_public_target_from_link(group["link"]) or group["chat_id"]


def bot_is_admin_in_group(group):
    chat_ref = resolve_group_chat_ref(group)
    if not chat_ref:
        return False
    cache_key = f"{group.get('key', group.get('name'))}|{chat_ref}"
    now = time.time()
    cached = bot_admin_check_cache.get(cache_key)
    if cached and now - cached[0] < BOT_ADMIN_CHECK_CACHE_SECONDS:
        return cached[1]
    try:
        member = bot.get_chat_member(chat_ref, get_bot_id())
        is_admin = member.status in ("creator", "administrator")
        bot_admin_check_cache[cache_key] = (now, is_admin)
        return is_admin
    except Exception as e:
        error_text = str(e)
        last_log = bot_admin_error_log_cache.get(cache_key)
        if not last_log or last_log[0] != error_text or now - last_log[1] >= BOT_ADMIN_ERROR_LOG_SECONDS:
            print(f"Bot admin check unavailable for {group['name']}: {error_text}")
            bot_admin_error_log_cache[cache_key] = (error_text, now)
        bot_admin_check_cache[cache_key] = (now, False)
        return False


def get_active_required_groups():
    return [group for group in get_required_groups() if bot_is_admin_in_group(group)]


def get_otp_source_chat_ids():
    conn = get_db_connection()
    rows = conn.execute("SELECT chat_id, link FROM bot_targets WHERE otp_source=1 AND TRIM(link) <> ''").fetchall()
    conn.close()
    resolved_ids = set()
    for row in rows:
        configured = derive_public_target_from_link(row[1]) or normalize_chat_id(row[0])
        if isinstance(configured, int):
            resolved_ids.add(configured)
            continue
        try:
            resolved_ids.add(bot.get_chat(configured).id)
        except Exception as e:
            print(f"OTP source resolve failed for {configured}: {e}")
    return resolved_ids

# =================  HELPERS =================

def is_joined_member(member):
    if member.status in ['creator', 'administrator', 'member']:
        return True
    if member.status == 'restricted':
        return getattr(member, 'is_member', True)
    return False

def get_missing_required_groups(user_id):
    if user_id == ADMIN_ID:
        return []

    missing_groups = []
    for group in get_active_required_groups():
        chat_ref = resolve_group_chat_ref(group)
        try:
            member = bot.get_chat_member(chat_ref, user_id)
            if not is_joined_member(member):
                missing_groups.append(group)
        except Exception as e:
            missing_groups.append(group)
            print(f"Membership check failed for {group['name']} and user {user_id}: {e}")
    return missing_groups

def check_membership(user_id):
    return len(get_missing_required_groups(user_id)) == 0

def send_join_required_message(chat_id, missing_groups=None):
    missing_groups = missing_groups if missing_groups is not None else get_missing_required_groups(chat_id)
    markup = LiveInlineKeyboardMarkup(row_width=1)
    for group in missing_groups:
        markup.add(inline_button(f"Join {group['name']}", url=group["link"], action_key="lock"))
    markup.add(inline_button("Verify Now", callback_data="verify_join", action_key="verify"))

    group_lines = "\n".join(f"• {group['name']}" for group in missing_groups)
    text = (
        f"{live_action_html('lock')} <b>Access Locked</b>\n\n"
        "Please join all required groups/channels, then tap <b>Verify Now</b>.\n\n"
        f"<b>Missing:</b>\n{escape(group_lines)}"
    )
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

def clean_phone_number(number):
    num_str = str(number).strip()
    digits = "".join(filter(str.isdigit, num_str))
    return "+" + digits if num_str.startswith('+') else digits


def extract_numbers_from_excel(file_bytes):
    """Extract unique phone-like values from likely phone columns in an Excel file."""
    numbers = []
    seen = set()
    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    for worksheet in workbook.worksheets:
        rows = list(worksheet.iter_rows(values_only=True))
        if not rows:
            continue

        header = [str(cell or "").strip().lower() for cell in rows[0]]
        preferred_indexes = [
            index for index, name in enumerate(header)
            if name in {"number", "phone", "phone number", "mobile", "msisdn"}
        ]
        data_rows = rows[1:] if preferred_indexes else rows

        for row in data_rows:
            cells = [row[index] for index in preferred_indexes if index < len(row)] if preferred_indexes else row
            for cell in cells:
                if cell is None:
                    continue
                raw = str(cell).strip()
                candidates = re.findall(r"\+?\d[\d\s().-]{5,25}\d", raw) or [raw]
                for candidate in candidates:
                    digits = clean_phone_number(candidate)
                    plain_digits = digits.lstrip("+")
                    if 7 <= len(plain_digits) <= 18 and plain_digits.isdigit():
                        if plain_digits in {"0000000", "00000000"}:
                            continue
                        if digits not in seen:
                            seen.add(digits)
                            numbers.append(digits)
    return numbers

def add_flag_to_name(text):
    # Live-only mode: store/display plain country names. Live flag is rendered separately.
    return strip_country_flag(text)

def clean_label(text, fallback="Unknown"):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:60] if text else fallback

def get_service_icon(service_name):
    # Live-only mode: no normal service emoji fallback in text.
    return LIVE_TEXT_FALLBACK

def get_file_token(filename):
    return hashlib.sha1(filename.encode("utf-8")).hexdigest()[:12]

def resolve_file_token(token_or_filename):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT filename FROM numbers")
    filenames = [row[0] for row in cursor.fetchall()]
    conn.close()
    for filename in filenames:
        if get_file_token(filename) == token_or_filename:
            return filename
    return token_or_filename if token_or_filename in filenames else None

def parse_filename_info(filename):
    base = filename.replace(".txt", "")
    if " - " in base:
        service, country = base.split(" - ", 1)
        return service.strip(), country.strip()
    return base.strip(), "Unknown"

def display_service(service, filename=None):
    if service and service != "Unknown":
        return service
    if filename:
        return parse_filename_info(filename)[0]
    return "Unknown"

def display_country(country, filename=None):
    if country and country != "Unknown":
        return add_flag_to_name(country)
    if filename:
        parsed_country = parse_filename_info(filename)[1]
        return parsed_country if parsed_country != "Unknown" else "Unknown"
    return "Unknown"

def fetch_available_numbers(cursor, filename):
    cursor.execute(
        """SELECT id, phone, service, country, filename, otp_received FROM numbers
           WHERE filename=? AND status='available' AND COALESCE(otp_received, 0)=0
           ORDER BY id ASC LIMIT ?""",
        (filename, get_premium_numbers_per_assignment())
    )
    return cursor.fetchall()


def fetch_restock_numbers(cursor, filename):
    cursor.execute(
        """SELECT id, phone, service, country, filename, otp_received FROM numbers
           WHERE filename=? AND status='available' AND COALESCE(otp_received, 0)=0
           ORDER BY otp_received ASC, id ASC LIMIT ?""",
        (filename, get_premium_numbers_per_assignment())
    )
    return cursor.fetchall()


def fetch_free_used_numbers(cursor, filename, user_id=None):
    limit = get_free_numbers_per_assignment()
    cursor.execute(
        """SELECT n.id, n.phone, n.service, n.country, n.filename, n.otp_received FROM numbers n
           LEFT JOIN file_settings fs ON fs.filename=n.filename
           WHERE n.filename=?
             AND COALESCE(n.otp_received, 0)=0
             AND COALESCE(fs.free_enabled, 0)=1
             AND (? IS NULL OR NOT EXISTS (
                 SELECT 1 FROM number_assignments a
                 WHERE a.number_id=n.id AND a.user_id=? AND a.premium=0
             ))
           ORDER BY n.assigned_time ASC, n.id ASC LIMIT ?""",
        (filename, user_id, user_id, limit)
    )
    rows = cursor.fetchall()
    if len(rows) >= limit or user_id is None:
        return rows
    used_ids = {row[0] for row in rows}
    extra_limit = limit - len(rows)
    if used_ids:
        placeholders = ",".join("?" for _ in used_ids)
        cursor.execute(
            f"""SELECT n.id, n.phone, n.service, n.country, n.filename, n.otp_received FROM numbers n
                LEFT JOIN file_settings fs ON fs.filename=n.filename
                WHERE n.filename=?
                  AND COALESCE(n.otp_received, 0)=0
                  AND COALESCE(fs.free_enabled, 0)=1
                  AND n.id NOT IN ({placeholders})
                ORDER BY n.assigned_time ASC, n.id ASC LIMIT ?""",
            (filename, *used_ids, extra_limit)
        )
    else:
        cursor.execute(
            """SELECT n.id, n.phone, n.service, n.country, n.filename, n.otp_received FROM numbers n
               LEFT JOIN file_settings fs ON fs.filename=n.filename
               WHERE n.filename=?
                 AND COALESCE(n.otp_received, 0)=0
                 AND COALESCE(fs.free_enabled, 0)=1
               ORDER BY n.assigned_time ASC, n.id ASC LIMIT ?""",
            (filename, extra_limit)
        )
    rows.extend(cursor.fetchall())
    return rows


def fetch_available_numbers_by_prefix(cursor, prefix):
    cursor.execute(
        """SELECT id, phone, service, country, filename, otp_received FROM numbers
           WHERE status='available' AND COALESCE(otp_received, 0)=0 AND phone LIKE ?
           ORDER BY id ASC LIMIT ?""",
        (f"{prefix}%", get_premium_numbers_per_assignment())
    )
    return cursor.fetchall()


def fetch_restock_numbers_by_prefix(cursor, prefix):
    cursor.execute(
        """SELECT id, phone, service, country, filename, otp_received FROM numbers
           WHERE status='available' AND COALESCE(otp_received, 0)=0 AND phone LIKE ?
           ORDER BY otp_received ASC, id ASC LIMIT ?""",
        (f"{prefix}%", get_premium_numbers_per_assignment())
    )
    return cursor.fetchall()


def mark_numbers_assigned(cursor, rows, user_id, current_time, premium_user=False):
    if premium_user:
        cursor.executemany(
            """UPDATE numbers
               SET status='taken', user_id=?, assigned_time=?,
                   premium_used=1, premium_user_id=?, premium_assigned_time=?
               WHERE id=?""",
            [(user_id, current_time, user_id, current_time, row[0]) for row in rows]
        )
        return
    cursor.executemany(
        """INSERT OR IGNORE INTO number_assignments
           (number_id, user_id, premium, assigned_time)
           VALUES (?, ?, 0, ?)""",
        [(row[0], user_id, current_time) for row in rows]
    )

def build_stock_saved_message(service_name, country_display, saved_count):
    country_plain = strip_country_flag(country_display)
    return (
        f"{live_action_html('success')} <b>Saved successfully!</b>\n"
        f"{live_action_html('service')} <b>Service:</b> {live_service_html(service_name)} {escape(service_name)}\n"
        f"{live_action_html('country')} <b>Country:</b> {live_country_html(country_plain)} {escape(country_plain)}\n"
        f"{live_action_html('stats')} <b>Added:</b> {saved_count}"
    )

def build_public_stock_message(stock_message):
    return (
        f"{live_action_html('broadcast')} <b>New Number Stock Available</b>\n"
        f"{stock_message}\n\n"
        f"{live_action_html('get_number')} Tap <b>Get Number</b> to receive numbers."
    )


def build_channel_safe_stock_message(service_name, country_display, saved_count):
    country_plain = strip_country_flag(country_display)
    return (
        f"{live_action_html('broadcast')} <b>New Number Stock Available</b>\n"
        f"{live_action_html('success')} <b>Saved successfully!</b>\n"
        f"{live_action_html('service')} <b>Service:</b> {live_service_html(service_name)} {escape(service_name)}\n"
        f"{live_action_html('country')} <b>Country:</b> {live_country_html(country_plain)} {escape(country_plain)}\n"
        f"{live_action_html('stats')} <b>Added:</b> {saved_count}\n\n"
        f"{live_action_html('open_bot')} Tap <b>Open Bot</b> to receive numbers."
    )

def get_bot_url():
    global bot_username_cache
    if bot_username_cache:
        return f"https://t.me/{bot_username_cache}"
    try:
        bot_username_cache = bot.get_me().username
        return f"https://t.me/{bot_username_cache}" if bot_username_cache else None
    except Exception as e:
        print(f"Could not load bot username: {e}")
        return None

def get_broadcast_user_ids(premium_only=False):
    conn = get_db_connection()
    cursor = conn.cursor()
    if premium_only:
        cursor.execute("SELECT user_id FROM users WHERE plan='premium'")
    else:
        cursor.execute("SELECT user_id FROM users")
    user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    if premium_only:
        return [user_id for user_id in user_ids if is_premium_user(user_id)]
    return user_ids

def broadcast_stock_update(message_text, filename, exclude_user_id=None):
    premium_first = False
    user_ids = get_broadcast_user_ids(premium_only=premium_first)
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Get Number", callback_data=f"buy|{get_file_token(filename)}", action_key="get_number"),
        inline_button("OTP Channel", url=get_target("otp_group")["link"], action_key="otp")
    )

    sent_count = 0
    failed_count = 0
    for user_id in user_ids:
        if exclude_user_id and user_id == exclude_user_id:
            continue
        try:
            bot.send_message(user_id, message_text, parse_mode="HTML", reply_markup=markup)
            sent_count += 1
            time.sleep(0.05)
        except Exception as e:
            failed_count += 1
            print(f"Broadcast failed for {user_id}: {e}")
    audience = "premium users" if premium_first else "all users"
    print(f"Stock broadcast done for {audience}. Sent: {sent_count}, Failed: {failed_count}")

def create_group_broadcast_request(admin_chat_id, stock_message, filename, service_name=None, country_display=None, saved_count=None):
    request_id = hashlib.sha1(f"{filename}|{time.time()}".encode("utf-8")).hexdigest()[:12]
    pending_group_broadcasts[request_id] = {
        "message_text": build_public_stock_message(stock_message),
        "channel_message_text": build_channel_safe_stock_message(service_name, country_display, saved_count)
            if service_name is not None and country_display is not None and saved_count is not None
            else build_public_stock_message(stock_message),
        "filename": filename
    }

    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Approve Broadcast", callback_data=f"groupbc|approve|{request_id}", action_key="success"),
        inline_button("Decline", callback_data=f"groupbc|decline|{request_id}", action_key="warning")
    )
    bot.send_message(
        admin_chat_id,
        f"{live_action_html('broadcast')} <b>Group Broadcast Approval</b>\n\n"
        "This stock update is ready to send to the 3 required groups/channels.\n\n"
        f"{stock_message}",
        parse_mode="HTML",
        reply_markup=markup
    )

def build_open_bot_markup():
    bot_url = get_bot_url()
    if not bot_url:
        return None
    markup = LiveInlineKeyboardMarkup(row_width=1)
    markup.add(inline_button("Open Bot", url=f"{bot_url}?start=get_number", action_key="open_bot"))
    return markup

def broadcast_to_required_groups(message_text, channel_message_text=None):
    sent_targets = []
    failed_targets = []
    markup = build_open_bot_markup()

    for group in get_active_required_groups():
        chat_ref = resolve_group_chat_ref(group)
        try:
            outgoing_text = channel_message_text if str(chat_ref).startswith("@") else message_text
            bot.send_message(chat_ref, outgoing_text, parse_mode="HTML", reply_markup=markup)
            sent_targets.append(group["name"])
            time.sleep(0.05)
        except Exception as e:
            failed_targets.append(group["name"])
            print(f"Group broadcast failed for {group['name']}: {e}")
    return sent_targets, failed_targets


def api_filename(service_name, country_name, provider):
    service = clean_label(service_name, "API OTP")
    country = strip_country_flag(clean_label(country_name, "Unknown"))
    if provider == "agent":
        provider_label = "Agent"
    elif provider == "fastx":
        provider_label = "FastX"
    else:
        provider_label = clean_label(provider, "API")
    return f"{API_SYNC_FILENAME_PREFIX} {provider_label} - {service} - {country}.txt"


def cleanup_empty_api_files():
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT filename
           FROM numbers
           WHERE filename LIKE ?
           GROUP BY filename
           HAVING SUM(CASE WHEN status='available' AND COALESCE(otp_received, 0)=0 THEN 1 ELSE 0 END)=0
              AND SUM(CASE WHEN status='taken' AND COALESCE(otp_received, 0)=0 THEN 1 ELSE 0 END)=0""",
        (f"{API_SYNC_FILENAME_PREFIX} %",),
    ).fetchall()
    removed = 0
    with db_lock:
        for (filename,) in rows:
            conn.execute("DELETE FROM numbers WHERE filename=?", (filename,))
            conn.execute("DELETE FROM file_settings WHERE filename=?", (filename,))
            removed += 1
        conn.commit()
    conn.close()
    return removed


def cleanup_available_api_stock():
    conn = get_db_connection()
    with db_lock:
        cursor = conn.execute(
            """DELETE FROM numbers
               WHERE filename LIKE ?
                 AND status='available'
                 AND user_id IS NULL
                 AND COALESCE(otp_received, 0)=0""",
            (f"{API_SYNC_FILENAME_PREFIX} %",),
        )
        removed = max(0, cursor.rowcount)
        conn.commit()
    conn.close()
    cleanup_empty_api_files()
    return removed


def api_error_text(error):
    if isinstance(error, ApiRequestError):
        return str(error)
    return re.sub(r"https?://\\S+", "[hidden-url]", str(error or "API request failed"))


def save_api_numbers(numbers, announce=False, admin_chat_id=None):
    if not numbers:
        return 0, None, None, None
    conn = get_db_connection()
    cursor = conn.cursor()
    saved_total = 0
    first_filename = None
    first_service = None
    first_country = None
    with db_lock:
        for item in numbers:
            service_name = clean_label(item.service, "API OTP")
            country_name = clean_label(item.country, "Unknown")
            filename = api_filename(service_name, country_name, item.provider)
            first_filename = first_filename or filename
            first_service = first_service or service_name
            first_country = first_country or country_name
            cursor.execute(
                """INSERT OR IGNORE INTO numbers
                   (phone, filename, service, country, status, user_id, otp_received, otp_time, assigned_time)
                   VALUES (?, ?, ?, ?, 'available', NULL, 0, 0, 0)""",
                (item.phone, filename, service_name, country_name),
            )
            saved_total += max(0, cursor.rowcount)
            cursor.execute("INSERT OR IGNORE INTO file_settings (filename, free_enabled) VALUES (?, 1)", (filename,))
            cursor.execute("UPDATE file_settings SET free_enabled=1 WHERE filename=? AND COALESCE(free_enabled, 0)=0", (filename,))
        conn.commit()
    conn.close()
    if announce and saved_total > 0 and admin_chat_id and first_filename:
        country_display = add_flag_to_name(first_country)
        stock_message = build_stock_saved_message(first_service, country_display, saved_total)
        bot.send_message(
            admin_chat_id,
            build_file_free_access_message(first_filename, header_text=stock_message),
            parse_mode="HTML",
            reply_markup=build_file_free_access_markup(first_filename),
        )
    return saved_total, first_filename, first_service, first_country


def sync_agent_numbers(limit=100, announce=False, admin_chat_id=None):
    if not api_provider_ready("agent"):
        return 0
    client = get_api_clients()["agent"]
    numbers = client.agent_numbers(limit=limit, cli=get_app_setting("api_agent_cli", ""))
    saved, _filename, _service, _country = save_api_numbers(numbers, announce=announce, admin_chat_id=admin_chat_id)
    return saved


def fetch_fastx_numbers(count=1, announce=False, admin_chat_id=None, provider_key="fastx"):
    provider = get_api_provider_by_key(provider_key)
    if not provider or not provider.get("enabled") or not provider.get("base_url") or not provider.get("api_key"):
        return 0
    range_prefix = provider.get("range", "")
    saved, _filename = fetch_fastx_range_stock(range_prefix, count=count, announce=announce, admin_chat_id=admin_chat_id, provider_key=provider_key)
    return saved


def fetch_fastx_range_stock(range_prefix, count=1, announce=False, admin_chat_id=None, provider_key="fastx"):
    provider = get_api_provider_by_key(provider_key)
    if not provider or not provider.get("enabled") or not provider.get("base_url") or not provider.get("api_key"):
        return 0, None
    client = build_fastx_client(provider)
    if not range_prefix:
        range_prefix = client.fastx_default_range(provider.get("service", "WhatsApp"))
        if range_prefix and provider_key == "fastx":
            set_app_setting("api_fastx_range", range_prefix)
        elif range_prefix and str(provider_key).startswith("extra:"):
            update_extra_api_provider(provider["id"], "range_prefix", range_prefix)
    if not range_prefix:
        return 0, None
    all_numbers = []
    for _ in range(max(1, min(int(count or 1), 20))):
        all_numbers.extend(client.fastx_get_number(range_prefix))
        time.sleep(0.15)
    saved, filename, _service, _country = save_api_numbers(all_numbers, announce=announce, admin_chat_id=admin_chat_id)
    return saved, filename


def api_fill_stock_for_filename(filename, needed):
    if not filename or not str(filename).startswith(API_SYNC_FILENAME_PREFIX):
        return 0
    needed = max(1, min(int(needed or 1), 20))
    total = 0
    if "FastX" in filename:
        service_name, country_name = parse_filename_info(filename)
        old_service = get_app_setting("api_fastx_service", "WhatsApp")
        old_country = get_app_setting("api_fastx_country", "Unknown")
        set_app_setting("api_fastx_service", service_name)
        set_app_setting("api_fastx_country", country_name)
        total += fetch_fastx_numbers(count=needed)
        set_app_setting("api_fastx_service", old_service)
        set_app_setting("api_fastx_country", old_country)
    elif "Agent" in filename:
        total += sync_agent_numbers(limit=needed)
    else:
        provider_label = str(filename).replace(f"{API_SYNC_FILENAME_PREFIX} ", "", 1).split(" - ", 1)[0].strip()
        for provider in get_extra_api_providers(enabled_only=True):
            if normalize_emoji_key(provider["name"]) == normalize_emoji_key(provider_label):
                total += fetch_fastx_numbers(count=needed, provider_key=provider["key"])
                break
    return total


def fetch_rows_for_user_click(cursor, filename, user_id, premium_user=False):
    if premium_user:
        rows = fetch_available_numbers(cursor, filename)
        return rows or fetch_restock_numbers(cursor, filename)
    if get_bool_setting("free_used_numbers_enabled", True):
        return fetch_free_used_numbers(cursor, filename, user_id)
    return []


def process_external_api_otp(api_otp):
    text = api_otp.message or api_otp.code or ""
    if api_otp.number and api_otp.number not in text:
        text = f"{api_otp.number} {text}"
    matches = find_matching_assignments(text)
    if not matches:
        return 0

    delivered_keys = set()
    matched_row_ids = []
    delivered = 0
    for user_id, row_id, phone, service, country, filename, _score, _candidate in matches:
        matched_row_ids.append(row_id)
        delivery_key = (user_id, row_id)
        if delivery_key in delivered_keys:
            continue
        service_name = display_service(api_otp.service or service, filename)
        country_plain = strip_country_flag(display_country(api_otp.country or country, filename))
        otp_code = api_otp.code or extract_otp_code_from_text(text)
        card = build_sms_card(phone, service_name, country_plain, country_plain, otp_code)
        try:
            bot.send_message(
                user_id,
                card,
                parse_mode="HTML",
                reply_markup=otp_button_rows(otp_code, text),
                disable_web_page_preview=True,
            )
            conn = get_db_connection()
            conn.execute(
                """INSERT INTO users (user_id, total_text_otps)
                   VALUES (?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET total_text_otps=total_text_otps+1""",
                (user_id,),
            )
            conn.commit(); conn.close()
            delivered += 1
        except Exception as e:
            print(f"API OTP delivery failed for user {user_id}: {e}")
        delivered_keys.add(delivery_key)

    if matched_row_ids:
        conn = get_db_connection()
        unique_row_ids = list(dict.fromkeys(matched_row_ids))
        placeholders = ",".join("?" for _ in unique_row_ids)
        conn.execute(
            f"UPDATE numbers SET otp_received=1, otp_time=? WHERE id IN ({placeholders})",
            [time.time(), *unique_row_ids],
        )
        conn.commit(); conn.close()
    if delivered:
        try:
            bot.send_message(
                normalize_chat_id(get_target("otp_group")["chat_id"]),
                build_sms_card(api_otp.number, api_otp.service, api_otp.country, api_otp.country, api_otp.code),
                parse_mode="HTML",
                reply_markup=otp_button_rows(api_otp.code, text),
                disable_web_page_preview=True,
            )
        except Exception as e:
            print(f"API OTP group relay failed: {e}")
    return delivered


def poll_api_otps_once():
    if not get_bool_setting("api_sync_enabled", False):
        return 0
    delivered = 0
    seen = set()
    try:
        if api_provider_ready("agent"):
            client = get_api_clients()["agent"]
            since = get_app_setting("api_last_agent_since", "")
            otps = client.agent_otps(since=since, platform=get_app_setting("api_agent_service", ""))
            for otp in otps:
                key = otp.event_id or f"agent:{otp.number}:{otp.code}:{otp.message}"
                if key in seen:
                    continue
                seen.add(key)
                delivered += process_external_api_otp(otp)
            if otps:
                set_app_setting("api_last_agent_since", time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        print(f"Agent OTP poll failed: {api_error_text(e)}")
    try:
        if api_provider_ready("fastx"):
            for otp in get_api_clients()["fastx"].fastx_otps():
                key = otp.event_id or f"fastx:{otp.number}:{otp.code}:{otp.message}"
                if key in seen:
                    continue
                seen.add(key)
                delivered += process_external_api_otp(otp)
    except Exception as e:
        print(f"FastX OTP poll failed: {api_error_text(e)}")
    for provider in get_extra_api_providers(enabled_only=True):
        try:
            for otp in build_fastx_client(provider).fastx_otps():
                key = otp.event_id or f"{provider['key']}:{otp.number}:{otp.code}:{otp.message}"
                if key in seen:
                    continue
                seen.add(key)
                delivered += process_external_api_otp(otp)
        except Exception as e:
            print(f"{provider.get('name', 'API')} OTP poll failed: {api_error_text(e)}")
    return delivered


def api_sync_worker():
    while True:
        try:
            if get_bool_setting("api_sync_enabled", False):
                poll_api_otps_once()
        except Exception as e:
            print(f"API sync worker failed: {api_error_text(e)}")
        time.sleep(get_api_sync_interval())


def text_from_record(record, keys, default=""):
    if not isinstance(record, dict):
        return default
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def live_range_token(range_text, provider_key=""):
    return hashlib.sha1(f"{provider_key}|{range_text or ''}".encode("utf-8")).hexdigest()[:12]


def live_range_from_record(record):
    if isinstance(record, dict):
        ranges = record.get("ranges")
        if isinstance(ranges, list):
            for item in ranges:
                if isinstance(item, dict):
                    range_text = text_from_record(item, ("range", "prefix", "cli", "name", "value", "id"), "")
                else:
                    range_text = str(item or "").strip()
                if range_text:
                    return range_text
        return text_from_record(record, ("range", "prefix", "cli", "cli_range", "id"), "")
    return str(record or "").strip()


def parse_live_hit_count(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def live_hit_from_record(record):
    if not isinstance(record, dict):
        return 0
    for key in ("hit", "hits", "traffic", "count", "available", "stock", "numbers", "total", "quantity", "load"):
        value = record.get(key)
        if value is not None and str(value).strip():
            return parse_live_hit_count(value)
    return 0


def sorted_live_traffic_items(limit=None):
    items = list(live_traffic_range_cache.values())
    items.sort(
        key=lambda item: (
            -int(item.get("hit", 0) or 0),
            clean_label(item.get("country"), "Unknown").lower(),
            clean_label(item.get("service"), "WhatsApp").lower(),
            str(item.get("range", "")),
        )
    )
    return items[:limit] if limit else items


def remember_live_ranges(records, provider):
    for record in records[:LIVE_TRAFFIC_RANGE_LIMIT]:
        range_text = live_range_from_record(record)
        if not range_text:
            continue
        provider_key = provider.get("key", "fastx")
        token = live_range_token(range_text, provider_key)
        live_traffic_range_cache[token] = {
            "range": range_text,
            "service": text_from_record(record, ("service", "platform", "app", "name", "cli_name"), provider.get("service", "WhatsApp")) if isinstance(record, dict) else provider.get("service", "WhatsApp"),
            "country": text_from_record(record, ("country", "country_name", "region"), provider.get("country", "Unknown")) if isinstance(record, dict) else provider.get("country", "Unknown"),
            "hit": live_hit_from_record(record),
            "provider_key": provider_key,
            "provider_name": provider.get("name", "FastX"),
        }


def build_live_traffic_markup():
    markup = LiveInlineKeyboardMarkup(row_width=1)
    markup.add(inline_button("Refresh", callback_data="traffic|refresh", action_key="refresh"), row_width=1)
    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    return markup


def refresh_live_traffic_cache():
    providers = get_fastx_provider_configs(enabled_only=True)
    if not providers:
        live_traffic_range_cache.clear()
        return 0, providers, ["No API is enabled yet. Ask admin to enable API Integrations."]
    live_traffic_range_cache.clear()
    loaded = 0
    errors = []
    for provider in providers:
        try:
            records = build_fastx_client(provider).fastx_live_access()
        except Exception as exc:
            errors.append(f"{provider.get('name', 'API')}: {api_error_text(exc)[:80]}")
            continue
        if records:
            remember_live_ranges(records, provider)
            loaded += len(records)
    return loaded, providers, errors


def build_live_traffic_message():
    _loaded, providers, errors = refresh_live_traffic_cache()
    if not providers:
        return (
            f"{live_action_html('warning')} <b>Live Traffic</b>\n\n"
            "No API is enabled yet. Ask admin to enable API Integrations."
        )
    if not live_traffic_range_cache:
        live_traffic_range_cache.clear()
        text = f"{live_action_html('warning')} <b>Live Traffic</b>\n\nNo live ranges available right now."
        if errors:
            text += "\n" + "\n".join(f"<code>{escape(error)}</code>" for error in errors[:3])
        return text

    lines = [
        f"{live_action_html('stats')} <b>Live Traffic</b>",
        f"<b>Live Range:</b> {len(live_traffic_range_cache)} | <b>API:</b> {len(providers)} | <b>Auto:</b> {LIVE_TRAFFIC_AUTO_REFRESH_SECONDS}s",
        "",
    ]
    for index, item in enumerate(sorted_live_traffic_items(LIVE_TRAFFIC_RANGE_LIMIT), start=1):
        service = clean_label(item.get("service"), "WhatsApp")
        country = strip_country_flag(clean_label(item.get("country"), "Unknown"))
        range_text = str(item.get("range", "")).strip()
        hit = int(item.get("hit", 0) or 0)
        lines.append(
            f"{index}. {live_country_html(country)} <b>{escape(country)}</b> | "
            f"{live_service_html(service)} <b>{escape(service)}</b> | "
            f"<code>{escape(range_text)}</code> | <b>Hit:</b> {hit}"
        )
    return "\n".join(lines)


def send_live_traffic(chat_id, edit_message=None):
    sent = send_or_edit_message(
        chat_id,
        build_live_traffic_message(),
        reply_markup=build_live_traffic_markup(),
        parse_mode="HTML",
        edit_message=edit_message,
    )
    try:
        live_traffic_auto_messages[(sent.chat.id, sent.message_id)] = time.time()
    except Exception:
        pass
    return sent


def live_traffic_auto_refresh_worker():
    while True:
        time.sleep(LIVE_TRAFFIC_AUTO_REFRESH_SECONDS)
        now = time.time()
        for key, created_at in list(live_traffic_auto_messages.items()):
            chat_id, message_id = key
            if now - created_at > LIVE_TRAFFIC_AUTO_REFRESH_TTL:
                live_traffic_auto_messages.pop(key, None)
                continue
            try:
                bot.edit_message_text(
                    build_live_traffic_message(),
                    chat_id,
                    message_id,
                    parse_mode="HTML",
                    reply_markup=build_live_traffic_markup(),
                )
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    live_traffic_auto_messages.pop(key, None)


def send_pending_group_broadcast(chat_id, request_id, message_text):
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Approve Broadcast", callback_data=f"groupbc|approve|{request_id}", action_key="success"),
        inline_button("Decline", callback_data=f"groupbc|decline|{request_id}", action_key="warning")
    )
    bot.send_message(
        chat_id,
        f"{live_action_html('broadcast')} <b>Pending Group Broadcast</b>\n\n"
        f"{message_text}",
        parse_mode="HTML",
        reply_markup=markup
    )

# =================  START & MENU =================

def build_main_menu_markup(user_id):
    markup = LiveReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    prefix_button = reply_button("Cancel Prefix", action_key="close", style="danger") if user_id in active_prefix_filters else reply_button("Set Prefix", action_key="manage", style="primary")
    markup.add(
        reply_button("Get Number", action_key="get_number"),
        reply_button("Live Traffic", action_key="stats", style="success"),
        row_width=2
    )
    markup.add(
        prefix_button,
        reply_button("My Account", action_key="stats", style="success"),
        row_width=2
    )
    markup.add(
        reply_button("Support", action_key="support", style="success"),
        row_width=1
    )
    return markup


def send_main_menu(chat_id):
    """Send the main user menu."""
    bot.send_message(
        chat_id,
        f"{live_action_html('welcome')} <b>Welcome!</b>\n{live_action_html('fastest')} <b>Fastest OTP Number Service</b>\n{live_action_html('select_option')} Select an option:",
        parse_mode="HTML",
        reply_markup=build_main_menu_markup(chat_id)
    )

@bot.message_handler(commands=['start'])
def user_welcome(message):
    track_user_profile(message.from_user)
    if is_user_banned(message.from_user.id):
        send_banned_notice(message.chat.id)
        return

    missing_groups = get_missing_required_groups(message.chat.id)
    if missing_groups:
        send_join_required_message(message.chat.id, missing_groups)
        return
    
    if len(message.text.split()) > 1:
        param = message.text.split()[1]
        if param == "get_number":
            show_countries(message)
            return

    send_main_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "verify_join")
def verify_join_callback(call):
    missing_groups = get_missing_required_groups(call.from_user.id)
    if not missing_groups:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.answer_callback_query(call.id, "Verified!")
        send_main_menu(call.from_user.id)
    else:
        missing_names = ", ".join(group["name"] for group in missing_groups)
        bot.answer_callback_query(call.id, f"Still missing: {missing_names}", show_alert=True)
        send_join_required_message(call.from_user.id, missing_groups)

# ================= NUMBER HANDLERS =================

@bot.message_handler(func=lambda m: m.text in ('Get Number', 'Get Number') or (m.text and m.text.startswith('/start') and 'get_number' in m.text))
def show_countries(message):
    # User flow changed: first show only available services.
    # After service click, show countries for that service.
    track_user_profile(message.from_user)
    if is_user_banned(message.from_user.id):
        send_banned_notice(message.chat.id)
        return
    missing_groups = get_missing_required_groups(message.chat.id)
    if missing_groups:
        send_join_required_message(message.chat.id, missing_groups)
        return
    cleanup_available_api_stock()
    send_user_service_stock_picker(message.chat.id, page=0, reply_to_message=message, user_id=message.from_user.id)


@bot.message_handler(func=lambda m: m.text == 'Live Traffic')
def live_traffic_from_menu(message):
    track_user_profile(message.from_user)
    if is_user_banned(message.from_user.id):
        send_banned_notice(message.chat.id)
        return
    missing_groups = get_missing_required_groups(message.chat.id)
    if missing_groups:
        send_join_required_message(message.chat.id, missing_groups)
        return
    send_live_traffic(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("traffic|"))
def live_traffic_callback(call):
    parts = call.data.split("|")
    action = parts[1] if len(parts) > 1 else "refresh"
    if action == "range" and len(parts) > 2:
        bot.answer_callback_query(call.id, "Use Get Number to take numbers.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Refreshed.")
    send_live_traffic(call.message.chat.id, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("trafficget|"))
def live_range_get_number_callback(call):
    token = call.data.split("|", 1)[1]
    item = live_traffic_range_cache.get(token)
    if not item:
        refresh_live_traffic_cache()
        item = live_traffic_range_cache.get(token)
    if not item:
        bot.answer_callback_query(call.id, "Live range expired. Refresh Get Number first.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Getting number...")
    assign_live_range_number(call, item)


@bot.message_handler(func=lambda m: m.text == 'My Account')
def my_account(message):
    track_user_profile(message.from_user)
    if is_user_banned(message.from_user.id):
        send_banned_notice(message.chat.id)
        return
    _plan, _expire_text, text_count, voice_count = format_subscription_status(message.from_user.id)
    text = (
        f"{live_action_html('stats')} <b>My Account</b>\n"
        f"{live_action_html('phone')} <b>User ID:</b> <code>{message.from_user.id}</code>\n"
        f"{live_action_html('message_otp')} <b>Lifetime OTP Messages:</b> {text_count}\n"
        f"{live_action_html('voice_otp')} <b>Lifetime Voice Messages:</b> {voice_count}"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(func=lambda m: m.text == "Set Prefix")
def set_prefix_from_menu(message):
    pending_prefix_inputs.add(message.from_user.id)
    msg = bot.send_message(
        message.chat.id,
        f"{live_action_html('manage')} Send the starting digits you want to search.\n\nExample: <code>38160</code>",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, save_prefix_and_assign)


@bot.message_handler(func=lambda m: m.text == "Cancel Prefix")
def cancel_prefix_from_menu(message):
    active_prefix_filters.pop(message.from_user.id, None)
    pending_prefix_inputs.discard(message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"{live_action_html('success')} Prefix mode cancelled. Normal number flow is active.",
        parse_mode="HTML",
        reply_markup=build_main_menu_markup(message.from_user.id)
    )


def send_shop_link_message(chat_id, title, url, emoji_id=None, style="primary", action_key="open_bot"):
    markup = LiveInlineKeyboardMarkup(row_width=1)
    button = inline_button(f"Open {title}", url=url, action_key=action_key, style=style)
    if emoji_id:
        button["icon_custom_emoji_id"] = emoji_id
    markup.add(button, row_width=1)
    bot.send_message(chat_id, f"<b>{escape(title)}</b>", parse_mode="HTML", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "Buy Premium")
def buy_premium_from_menu(message):
    track_user_profile(message.from_user)
    if is_user_banned(message.from_user.id):
        send_banned_notice(message.chat.id)
        return
    bot.send_message(
        message.chat.id,
        f"{live_action_html('get_number')} Subscriptions are disabled. Use <b>Get Number</b> to receive live numbers.",
        parse_mode="HTML",
        reply_markup=build_main_menu_markup(message.from_user.id)
    )


def send_subscription_payment_methods(chat_id, edit_message=None):
    if not SUBSCRIPTION_FEATURES_ENABLED:
        text = f"{live_action_html('get_number')} Subscriptions are disabled. Use <b>Get Number</b> for live numbers."
        return send_or_edit_message(chat_id, text, reply_markup=None, parse_mode="HTML", edit_message=edit_message)
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Bkash", callback_data="subpay|bkash", action_key="buy_premium"),
        inline_button("Nagad", callback_data="subpay|nagad", action_key="buy_premium"),
        row_width=2
    )
    markup.add(
        inline_button("Rocket", callback_data="subpay|rocket", action_key="buy_premium"),
        inline_button("Binance", callback_data="subpay|binance", action_key="buy_premium"),
        row_width=2
    )
    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    text = (
        f"{live_action_html('buy_premium')} <b>Buy Premium</b>\n\n"
        "Select a payment method. After payment, submit your Transaction ID or Order ID for admin approval."
    )
    return send_or_edit_message(chat_id, text, reply_markup=markup, parse_mode="HTML", edit_message=edit_message)


def find_local_filename_for_live_item(item):
    service = clean_label(item.get("service"), "WhatsApp")
    country = strip_country_flag(clean_label(item.get("country"), "Unknown"))
    preferred = api_filename(service, country, item.get("provider_name", "FastX"))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT n.filename, COUNT(*) AS available_count FROM numbers n
           LEFT JOIN file_settings fs ON fs.filename=n.filename
           WHERE COALESCE(n.otp_received, 0)=0
             AND COALESCE(fs.free_enabled, 0)=1
             AND (n.filename=? OR (LOWER(n.service)=LOWER(?) AND LOWER(n.country)=LOWER(?)))
           GROUP BY n.filename
           ORDER BY CASE WHEN n.filename=? THEN 0 ELSE 1 END, available_count DESC, n.filename ASC
           LIMIT 1""",
        (preferred, service, country, preferred),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else preferred


def assign_live_range_number(call, item):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    current_time = time.time()
    if chat_id != user_id:
        bot.answer_callback_query(call.id, "Please open the bot in private to get numbers.", show_alert=True)
        return
    if is_user_banned(user_id):
        bot.answer_callback_query(call.id, "Your account is banned.", show_alert=True)
        return
    missing_groups = get_missing_required_groups(user_id)
    if missing_groups:
        bot.answer_callback_query(call.id, "Please join all required groups first.", show_alert=True)
        send_join_required_message(user_id, missing_groups)
        return
    elapsed = current_time - user_cooldowns.get(user_id, 0)
    free_cooldown = get_free_change_number_cooldown()
    if elapsed < free_cooldown:
        bot.answer_callback_query(call.id, f"Wait {int(free_cooldown - elapsed)}s before getting next numbers.", show_alert=True)
        return

    filename = find_local_filename_for_live_item(item)
    conn = get_db_connection()
    cursor = conn.cursor()
    with db_lock:
        rows = fetch_free_used_numbers(cursor, filename, user_id)
    conn.close()

    if not rows:
        range_text = item.get("range", "")
        provider_key = item.get("provider_key", "fastx")
        if provider_key == "fastx":
            set_app_setting("api_fastx_range", range_text)
            set_app_setting("api_fastx_service", item.get("service") or get_app_setting("api_fastx_service", "WhatsApp"))
            set_app_setting("api_fastx_country", item.get("country") or get_app_setting("api_fastx_country", "Unknown"))
        elif str(provider_key).startswith("extra:"):
            provider = get_api_provider_by_key(provider_key)
            if provider:
                update_extra_api_provider(provider["id"], "range_prefix", range_text)
        try:
            _saved, api_file = fetch_fastx_range_stock(range_text, count=get_free_numbers_per_assignment(), provider_key=provider_key)
            filename = api_file or filename
        except Exception as exc:
            bot.send_message(user_id, f"{live_action_html('warning')} Could not get number: <code>{escape(api_error_text(exc))}</code>", parse_mode="HTML")
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        with db_lock:
            rows = fetch_free_used_numbers(cursor, filename, user_id)
        conn.close()

    if not rows:
        bot.send_message(user_id, f"{live_action_html('warning')} No live number found for this range right now.", parse_mode="HTML", reply_markup=build_main_menu_markup(user_id))
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    with db_lock:
        mark_numbers_assigned(cursor, rows, user_id, current_time, premium_user=False)
        conn.commit()
    conn.close()
    user_cooldowns[user_id] = current_time
    try:
        bot.delete_message(chat_id, call.message.message_id)
    except Exception:
        pass
    send_assigned_numbers(user_id, rows, current_time, refresh_filename=filename)


@bot.callback_query_handler(func=lambda call: call.data == "subpayback")
def subscription_payment_back(call):
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Subscriptions are disabled.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_subscription_payment_methods(call.message.chat.id, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("subpay|"))
def subscription_payment_method_callback(call):
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Subscriptions are disabled.", show_alert=True)
        return
    track_user_profile(call.from_user)
    if is_user_banned(call.from_user.id):
        bot.answer_callback_query(call.id, "Your account is banned.", show_alert=True)
        return
    method = call.data.split("|", 1)[1]
    info = PAYMENT_METHODS.get(method)
    if not info:
        bot.answer_callback_query(call.id, "Payment method not found.", show_alert=True)
        return
    payment_number = get_app_setting(info["setting"], "Not set")
    id_label = info["id_label"]
    instruction = (
        f"Send payment to this {escape(info['label'])} account, then tap Submit and send your {id_label}."
        if method != "binance"
        else f"Send payment to this Binance account, then tap Submit and send your {id_label}."
    )
    markup = LiveInlineKeyboardMarkup(row_width=1)
    markup.add(inline_button("Copy Number", copy_text=payment_number, action_key="copy_full", style="success"))
    markup.add(inline_button(f"Submit {id_label}", callback_data=f"subsubmit|{method}", action_key="success"))
    markup.row(
        inline_button("Back", callback_data="subpayback", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close")
    )
    plan_lines = build_payment_plan_lines(method)
    text_lines = [
        f"{live_action_html('buy_premium')} <b>{escape(info['label'])} Payment</b>\n\n"
        f"<b>Number/ID:</b> <code>{escape(payment_number)}</code>"
    ]
    if plan_lines:
        text_lines.extend(plan_lines)
    text_lines.extend(["", escape(instruction)])
    bot.answer_callback_query(call.id)
    send_or_edit_message(call.message.chat.id, "\n".join(text_lines), reply_markup=markup, parse_mode="HTML", edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("subsubmit|"))
def subscription_submit_callback(call):
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Subscriptions are disabled.", show_alert=True)
        return
    track_user_profile(call.from_user)
    if is_user_banned(call.from_user.id):
        bot.answer_callback_query(call.id, "Your account is banned.", show_alert=True)
        return
    method = call.data.split("|", 1)[1]
    info = PAYMENT_METHODS.get(method)
    if not info:
        bot.answer_callback_query(call.id, "Payment method not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.from_user.id,
        f"{live_action_html('manage')} Send your <b>{escape(info['id_label'])}</b> for {escape(info['label'])}.",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, handle_subscription_payment_identifier, method)


def handle_subscription_payment_identifier(message, method):
    if not SUBSCRIPTION_FEATURES_ENABLED:
        return
    track_user_profile(message.from_user)
    if is_user_banned(message.from_user.id):
        send_banned_notice(message.chat.id)
        return
    info = PAYMENT_METHODS.get(method)
    if not info:
        return
    payment_identifier = str(message.text or "").strip()
    if len(payment_identifier) < 3:
        bot.reply_to(message, f"{live_action_html('warning')} Please send a valid {escape(info['id_label'])}.", parse_mode="HTML")
        return
    request_id = create_subscription_request(message.from_user.id, method, payment_identifier)
    if method == "binance":
        try:
            approved, detail = try_auto_approve_binance_request(request_id)
            if approved:
                bot.reply_to(
                    message,
                    (
                        f"{live_action_html('success')} Binance payment verified automatically.\n"
                        f"Request ID: <code>{escape(request_id)}</code>"
                    ),
                    parse_mode="HTML",
                    reply_markup=build_main_menu_markup(message.from_user.id)
                )
                return
            else:
                print(f"Binance auto verify pending for {request_id}: {detail}")
        except Exception as e:
            print(f"Binance auto verify failed for {request_id}: {e}")
    bot.reply_to(
        message,
        (
            f"{live_action_html('success')} Payment request submitted.\n"
            f"Request ID: <code>{escape(request_id)}</code>\n"
            "Admin will review it soon."
        ),
        parse_mode="HTML",
        reply_markup=build_main_menu_markup(message.from_user.id)
    )
    send_subscription_request_to_admin(request_id)


def send_subscription_request_to_admin(request_id):
    row = get_subscription_request(request_id)
    if not row:
        return
    _request_id, user_id, method, payment_identifier, status, requested_at, _days = row
    id_label = PAYMENT_METHODS.get(method, {}).get("id_label", "Payment ID")
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button(f"Copy {id_label}", copy_text=payment_identifier, style="success"),
        row_width=1
    )
    markup.add(
        inline_button("Accept", callback_data=f"subreq|accept|{request_id}", action_key="success"),
        inline_button("Reject", callback_data=f"subreq|reject|{request_id}", action_key="warning"),
        row_width=2
    )
    bot.send_message(
        ADMIN_ID,
        (
            f"{live_action_html('verify')} <b>New Premium Order</b>\n\n"
            "A user submitted a premium payment request. Check the payment ID below, then accept or reject it.\n\n"
            f"Request ID: <code>{escape(request_id)}</code>\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Payment Method: <b>{escape(payment_method_label(method))}</b>\n"
            f"{escape(id_label)}: <code>{escape(payment_identifier)}</code>\n"
            f"Status: <b>{escape(status)}</b>"
        ),
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.message_handler(func=lambda m: m.text == "Support")
def support_from_menu(message):
    send_shop_link_message(
        message.chat.id,
        "Support",
        get_app_setting("support_link", "https://t.me/SOHAG_BD_SHOP_BOT"),
        style="success",
        action_key="support"
    )


@bot.message_handler(func=lambda m: m.text == "Buy VPN")
def buy_vpn_from_menu(message):
    send_shop_link_message(
        message.chat.id,
        "VPN Purchase Bot",
        get_app_setting("vpn_link", "https://t.me/SOHAG_BD_SHOP_BOT"),
        BUTTON_EMOJI_IDS["buy_vpn"],
        "success",
        "open_bot"
    )


@bot.message_handler(func=lambda m: m.text == "Buy Proxy")
def buy_proxy_from_menu(message):
    send_shop_link_message(
        message.chat.id,
        "Proxy Purchase Bot",
        get_app_setting("proxy_link", "https://t.me/ProxyHub_BD_BOT"),
        BUTTON_EMOJI_IDS["buy_proxy"],
        "primary",
        "open_bot"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("prefix|"))
def prefix_tools_callback(call):
    action = call.data.split("|", 1)[1]
    if action == "cancel":
        active_prefix_filters.pop(call.from_user.id, None)
        pending_prefix_inputs.discard(call.from_user.id)
        bot.answer_callback_query(call.id, "Prefix mode cancelled.")
        bot.send_message(
            call.from_user.id,
            f"{live_action_html('success')} Prefix mode cancelled. Normal number flow is active.",
            parse_mode="HTML",
            reply_markup=build_main_menu_markup(call.from_user.id)
        )
        return
    pending_prefix_inputs.add(call.from_user.id)
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.from_user.id,
        f"{live_action_html('manage')} Send the starting digits you want to search.\n\nExample: <code>38160</code>",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, save_prefix_and_assign)


def save_prefix_and_assign(message):
    user_id = message.from_user.id
    if user_id not in pending_prefix_inputs:
        return
    pending_prefix_inputs.discard(user_id)
    prefix = re.sub(r"\D+", "", str(message.text or ""))
    if len(prefix) < 3:
        bot.reply_to(message, f"{live_action_html('warning')} At least 3 starting digits are required.", parse_mode="HTML")
        return

    missing_groups = get_missing_required_groups(user_id)
    if missing_groups:
        send_join_required_message(user_id, missing_groups)
        return

    current_time = time.time()
    conn = get_db_connection()
    cursor = conn.cursor()
    with db_lock:
        rows = fetch_available_numbers_by_prefix(cursor, prefix)
        if not rows:
            rows = fetch_restock_numbers_by_prefix(cursor, prefix)
            if not rows:
                conn.close()
                bot.reply_to(message, f"{live_action_html('warning')} No available numbers found for prefix <code>{escape(prefix)}</code>.", parse_mode="HTML")
                return
        mark_numbers_assigned(cursor, rows, user_id, current_time, premium_user=True)
        conn.commit()
    conn.close()
    active_prefix_filters[user_id] = prefix
    user_cooldowns[user_id] = current_time
    send_assigned_numbers(user_id, rows, current_time, prefix_mode=True, prefix_value=prefix)
    bot.send_message(
        user_id,
        f"{live_action_html('success')} Prefix mode ready.",
        parse_mode="HTML",
        reply_markup=build_main_menu_markup(user_id)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('ustocksvcpage|'))
def user_stock_service_page_callback(call):
    page = int(call.data.split("|", 1)[1])
    bot.answer_callback_query(call.id)
    send_user_service_stock_picker(call.message.chat.id, page=page, edit_message=call.message, user_id=call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('ustocksvc|'))
def user_stock_service_callback(call):
    service_token = call.data.split("|", 1)[1]
    refresh_live_traffic_cache()
    service_name = resolve_available_service_token(service_token, call.from_user.id)
    if not service_name:
        bot.answer_callback_query(call.id, "Service stock not found. Please refresh Get Number.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_user_country_stock_picker(call.message.chat.id, service_name, page=0, edit_message=call.message, user_id=call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('ustockctrypage|'))
def user_stock_country_page_callback(call):
    _prefix, service_token, page_text = call.data.split("|", 2)
    refresh_live_traffic_cache()
    service_name = resolve_available_service_token(service_token, call.from_user.id)
    if not service_name:
        bot.answer_callback_query(call.id, "Service stock not found. Please refresh Get Number.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_user_country_stock_picker(call.message.chat.id, service_name, page=int(page_text), edit_message=call.message, user_id=call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == 'ustockback')
def user_stock_back_to_services(call):
    bot.answer_callback_query(call.id)
    send_user_service_stock_picker(call.message.chat.id, page=0, edit_message=call.message, user_id=call.from_user.id)


def send_assigned_numbers(user_id, rows, current_time, refresh_filename=None, prefix_mode=False, prefix_value=None):
    first = rows[0]
    if len(first) >= 6:
        _row_id, _phone, service_raw, country_raw, filename, _otp_received = first
    elif len(first) >= 5:
        _row_id, _phone, service_raw, country_raw, filename = first
    else:
        _row_id, _phone, service_raw, country_raw = first
        filename = refresh_filename

    service_name = display_service(service_raw, filename)
    country_name = display_country(country_raw, filename)
    service_title = f"{live_service_html(service_name)} {escape(service_name)}"
    country_title = f"{live_country_html(country_name)} {escape(strip_country_flag(country_name))}"
    msg_text = (
        f"{live_action_html('service')} <b>Service:</b> {service_title}\n"
        f"{live_action_html('country')} <b>Country:</b> {country_title}\n"
        + (f"{live_action_html('success')} <b>Prefix:</b> <code>{escape(prefix_value)}</code>\n" if prefix_mode and prefix_value else "")
        + f"{live_action_html('waiting_otp')} <b>Waiting for OTP...</b>"
    )

    markup = LiveInlineKeyboardMarkup(row_width=1)
    for index, row in enumerate(rows, start=1):
        otp_received = int(row[5] or 0) if len(row) >= 6 else 0
        phone_action_key = "phone_otp_received" if otp_received else "phone_no_otp"
        markup.add(
            inline_button(
                f"{index}. {row[1]}",
                copy_text=row[1],
                action_key=phone_action_key,
                style="success"
            ),
            row_width=1
        )
    if not prefix_mode and filename:
        markup.add(
            inline_button("Change Number", callback_data=f"refresh|{get_file_token(filename)}", action_key="refresh"),
            inline_button("OTP Channel", url=get_target("otp_group")["link"], action_key="otp"),
            row_width=2
        )
        markup.add(
            inline_button("Change Country", callback_data=f"changecountry|{get_service_token(service_name)}", action_key="country", style="primary"),
            row_width=1
        )
    else:
        markup.add(
            inline_button("Change Number", callback_data="prefixrefresh", action_key="refresh"),
            inline_button("OTP Channel", url=get_target("otp_group")["link"], action_key="otp"),
            row_width=2
        )
    bot.send_message(user_id, msg_text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "prefixrefresh")
def refresh_prefix_numbers(call):
    user_id = call.from_user.id
    prefix = active_prefix_filters.get(user_id)
    if not prefix:
        bot.answer_callback_query(call.id, "No active prefix.", show_alert=True)
        return
    current_time = time.time()
    conn = get_db_connection()
    cursor = conn.cursor()
    with db_lock:
        rows = fetch_available_numbers_by_prefix(cursor, prefix)
        if not rows:
            rows = fetch_restock_numbers_by_prefix(cursor, prefix)
            if not rows:
                conn.close()
                bot.answer_callback_query(call.id, "No available numbers for this prefix.", show_alert=True)
                return
        mark_numbers_assigned(cursor, rows, user_id, current_time, premium_user=True)
        conn.commit()
    conn.close()
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)
    user_cooldowns[user_id] = current_time
    send_assigned_numbers(user_id, rows, current_time, prefix_mode=True, prefix_value=prefix)


@bot.callback_query_handler(func=lambda call: call.data.startswith('buy|') or call.data.startswith('refresh|'))
def buy_or_refresh(call):
    action, token = call.data.split("|", 1)
    filename = resolve_file_token(token)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    current_time = time.time()
    track_user_profile(call.from_user)

    if chat_id != user_id:
        bot.answer_callback_query(call.id, "Please open the bot in private to get numbers.", show_alert=True)
        return
    if is_user_banned(user_id):
        bot.answer_callback_query(call.id, "Your account is banned.", show_alert=True)
        return

    premium_user = False
    if action == "refresh" and not premium_user:
        elapsed = current_time - user_cooldowns.get(user_id, 0)
        free_cooldown = get_free_change_number_cooldown()
        if elapsed < free_cooldown:
            bot.answer_callback_query(
                call.id,
                f"Wait {int(free_cooldown - elapsed)}s before changing number.",
                show_alert=True
            )
            return

    # Delete previous message when changing number
    if action == "refresh":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass

    missing_groups = get_missing_required_groups(user_id)
    if missing_groups:
        bot.answer_callback_query(call.id, "Please join all required groups first.", show_alert=True)
        send_join_required_message(user_id, missing_groups)
        return

    if not filename:
        bot.answer_callback_query(call.id, "Stock not found. Please open the menu again.", show_alert=True)
        return

    if not premium_user:
        elapsed = current_time - user_cooldowns.get(user_id, 0)
        free_cooldown = get_free_change_number_cooldown()
        if elapsed < free_cooldown:
            bot.answer_callback_query(
                call.id,
                f"Wait {int(free_cooldown - elapsed)}s before getting next numbers.",
                show_alert=True
            )
            return
    
    if action != "refresh" and user_id in user_cooldowns and (current_time - user_cooldowns[user_id] < 10):
        bot.answer_callback_query(call.id, f"⏳ Wait {int(10 - (current_time - user_cooldowns[user_id]))}s", show_alert=True)
        return
    
    bot.answer_callback_query(call.id) 

    conn = get_db_connection()
    cursor = conn.cursor()
    with db_lock:
        rows = fetch_rows_for_user_click(cursor, filename, user_id, premium_user)
    conn.close()

    if not rows and str(filename).startswith(API_SYNC_FILENAME_PREFIX):
        api_fill_stock_for_filename(
            filename,
            get_premium_numbers_per_assignment() if premium_user else get_free_numbers_per_assignment()
        )
        conn = get_db_connection()
        cursor = conn.cursor()
        with db_lock:
            rows = fetch_rows_for_user_click(cursor, filename, user_id, premium_user)
        conn.close()

    conn = get_db_connection()
    cursor = conn.cursor()
    with db_lock:
        if not rows:
            empty_text = f"{live_action_html('warning')} No live numbers available for this service right now."
            bot.send_message(user_id, empty_text, parse_mode="HTML", reply_markup=build_main_menu_markup(user_id))
            conn.close(); return

        mark_numbers_assigned(cursor, rows, user_id, current_time, premium_user=premium_user)
        cursor.execute("SELECT COUNT(*) FROM numbers WHERE filename=? AND status='available' AND COALESCE(otp_received, 0)=0", (filename,))
        stock_left = cursor.fetchone()[0]
        conn.commit()
    conn.close()
    
    user_cooldowns[user_id] = current_time
    send_assigned_numbers(user_id, rows, current_time, refresh_filename=filename)

@bot.callback_query_handler(func=lambda call: call.data == "close")
def close_menu(call):
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("changecountry|"))
def change_country_from_assignment(call):
    service_token = call.data.split("|", 1)[1]
    refresh_live_traffic_cache()
    service_name = resolve_available_service_token(service_token, call.from_user.id)
    if not service_name:
        bot.answer_callback_query(call.id, "Service stock not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_user_country_stock_picker(call.message.chat.id, service_name, page=0, edit_message=call.message, user_id=call.from_user.id)



# =================  OTP DETECTION =================
# OTP matching is handled by otp_matcher.py.
# It supports full numbers and masked/dirty formats such as:
# 01618202470, 01618xxx2470, 01618foj2470, 01618ei2470, 016****02470,
# +8801618202470 vs 01618202470, and numbers hidden inside captions/replies/file names.


def find_matching_assignments(full_text):
    candidates = extract_phone_candidates(full_text)
    if not candidates:
        return []

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, phone, user_id, service, country, filename
               FROM numbers
               WHERE status='taken' AND user_id IS NOT NULL AND COALESCE(otp_received, 0)=0
               ORDER BY id DESC"""
        )
        rows = cursor.fetchall()
        cursor.execute(
            """SELECT n.id, n.phone, a.user_id, n.service, n.country, n.filename
               FROM number_assignments a
               JOIN numbers n ON n.id=a.number_id
               WHERE a.premium=0 AND COALESCE(n.otp_received, 0)=0
               ORDER BY a.assigned_time DESC, a.id DESC"""
        )
        rows.extend(cursor.fetchall())
        conn.close()
    except Exception as e:
        print(f"OTP lookup failed: {e}")
        return []

    # Match per candidate so one OTP message can safely contain more than one number.
    # For each phone-like candidate, only the strongest matching assigned number(s) are selected.
    matches = []
    seen_delivery_keys = set()
    for candidate in candidates:
        best_score = 0
        best_rows = []
        for row_id, phone, user_id, service, country, filename in rows:
            phone_digits = "".join(filter(str.isdigit, str(phone)))
            score = candidate_match_score(candidate, phone_digits)
            if score <= 0:
                continue
            if score > best_score:
                best_score = score
                best_rows = [(user_id, row_id, phone, service, country, filename, score, candidate)]
            elif score == best_score:
                best_rows.append((user_id, row_id, phone, service, country, filename, score, candidate))

        for user_id, row_id, phone, service, country, filename, score, candidate in best_rows:
            delivery_key = (row_id, user_id)
            if delivery_key in seen_delivery_keys:
                continue
            seen_delivery_keys.add(delivery_key)
            matches.append((user_id, row_id, phone, service, country, filename, score, candidate))

    return matches


OTP_CODE_RE = re.compile(
    r"(?:otp\s*code|code)\s*[:\-]?\s*([A-Za-z0-9]+(?:[-\s][A-Za-z0-9]+)*)",
    re.IGNORECASE
)


def extract_otp_code_from_text(text):
    """Pull the OTP code from the source group's formatted message text."""
    source = str(text or "")
    labeled = OTP_CODE_RE.search(source)
    if labeled:
        return labeled.group(1).strip()

    fallback = re.search(r"\b\d{3,}[-\s]\d{3,}\b", source)
    if fallback:
        return fallback.group(0).strip()

    fallback = re.search(r"\b\d{4,8}\b", source)
    if fallback:
        return fallback.group(0).strip()

    return None


def build_user_delivery_message(message, phone, service, country, filename, source_text):
    content_type = getattr(message, "content_type", "text")
    source_lower = str(source_text or "").lower()
    is_voice = content_type in ("voice", "audio", "video_note") or "recording" in source_lower
    action_key = "voice_otp" if is_voice else "message_otp"
    title = "Voice Message Received" if is_voice else "OTP Message Received"
    service_name = display_service(service, filename)
    country_plain = strip_country_flag(display_country(country, filename))
    otp_code = extract_otp_code_from_text(source_text)

    if not is_voice:
        return build_sms_card(phone, service_name, country_plain, country_plain, otp_code), is_voice

    lines = [
        f"{live_action_html(action_key)} <b>{escape(title)}</b>",
        f"{live_country_html(country_plain)} {masked_number_html(phone)}",
    ]
    if "recording" in source_lower:
        lines.append(f"{live_action_html('waiting_otp')} <b>Status:</b> Recording...")
    return "\n".join(lines), is_voice


def handle_otp_source_message(message):
    # Reads text/caption/file names/contact phone/replied message text.
    # User receives a clean formatted delivery card; voice replaces any prior text notice.
    text = get_message_search_text(message)
    matches = find_matching_assignments(text)
    if not matches:
        return

    delivered_keys = set()
    matched_row_ids = []
    for user_id, row_id, phone, service, country, filename, score, candidate in matches:
        matched_row_ids.append(row_id)
        delivery_key = (user_id, row_id)
        if delivery_key in delivered_keys:
            continue
        try:
            user_message, is_voice = build_user_delivery_message(message, phone, service, country, filename, text)
            content_type = getattr(message, "content_type", "text")
            pending_key = (user_id, row_id)
            if content_type in ("voice", "audio", "video_note"):
                previous_notice_id = pending_delivery_notices.pop(pending_key, None)
                if previous_notice_id:
                    try:
                        bot.delete_message(user_id, previous_notice_id)
                    except Exception:
                        pass
                try:
                    bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id,
                        caption=user_message,
                        parse_mode="HTML"
                    )
                except Exception as copy_error:
                    if content_type in ("voice", "audio"):
                        media = getattr(message, content_type, None)
                        file_info = bot.get_file(media.file_id)
                        file_bytes = bot.download_file(file_info.file_path)
                        try:
                            bot.send_audio(
                                user_id,
                                file_bytes,
                                caption=user_message,
                                parse_mode="HTML",
                            )
                        except Exception:
                            bot.send_document(
                                user_id,
                                file_bytes,
                                caption=user_message,
                                parse_mode="HTML",
                            )
                    else:
                        raise copy_error
            elif content_type in ("photo", "document", "video", "animation"):
                bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    caption=user_message,
                    parse_mode="HTML",
                    reply_markup=otp_button_rows(extract_otp_code_from_text(text), text)
                )
            else:
                sent = bot.send_message(
                    user_id,
                    user_message,
                    parse_mode="HTML",
                    reply_markup=otp_button_rows(extract_otp_code_from_text(text), text),
                )
                if is_voice:
                    pending_delivery_notices[pending_key] = sent.message_id
            delivered_keys.add(delivery_key)
            counter_column = "total_voice_otps" if content_type in ("voice", "audio", "video_note") else "total_text_otps"
            conn = get_db_connection()
            conn.execute(
                f"""INSERT INTO users (user_id, {counter_column})
                    VALUES (?, 1)
                    ON CONFLICT(user_id) DO UPDATE SET {counter_column}={counter_column}+1""",
                (user_id,)
            )
            conn.commit(); conn.close()

            print("\n==========================================")
            print(f"Number: {phone}")
            print(f"Matched Text: {candidate}")
            print(f"Match Score: {score}")
            print("OTP Received Successfully!")
            print("==========================================\n")

        except Exception as e:
            print(f"OTP copy failed for user {user_id}: {e}")

    if matched_row_ids:
        try:
            conn = get_db_connection()
            unique_row_ids = list(dict.fromkeys(matched_row_ids))
            placeholders = ",".join("?" for _ in unique_row_ids)
            conn.execute(
                f"UPDATE numbers SET otp_received=1, otp_time=? WHERE id IN ({placeholders})",
                [time.time(), *unique_row_ids]
            )
            conn.commit(); conn.close()
        except Exception as e:
            print(f"OTP status update failed: {e}")


@bot.channel_post_handler(func=lambda m: m.chat.id in get_otp_source_chat_ids(), content_types=OTP_CONTENT_TYPES)
def handle_channel_otp(message):
    handle_otp_source_message(message)


@bot.message_handler(func=lambda m: m.chat.id in get_otp_source_chat_ids(), content_types=OTP_CONTENT_TYPES)
def handle_group_otp(message):
    handle_otp_source_message(message)


@bot.message_handler(commands=['testotp'])
def test_otp_match_command(message):
    """Admin test: /testotp 01618xxx2470"""
    if message.from_user.id != ADMIN_ID:
        return
    sample = (message.text or '').split(' ', 1)
    if len(sample) < 2:
        bot.reply_to(message, "Usage: /testotp 01618xxx2470")
        return
    candidates = extract_phone_candidates(sample[1])
    matches = find_matching_assignments(sample[1])
    lines = [f"{live_action_html('message_otp')} <b>OTP Match Test</b>", "", "<b>Candidates:</b>"]
    lines.extend(f"<code>{escape(c)}</code>" for c in candidates[:20])
    lines.append("")
    lines.append("<b>Matches:</b>")
    if not matches:
        lines.append("No matching assigned number found.")
    for user_id, row_id, phone, service, country, filename, score, candidate in matches[:20]:
        lines.append(f"<code>{escape(str(phone))}</code> → user <code>{user_id}</code> score <b>{score}</b> via <code>{escape(str(candidate))}</code>")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")


# ================= ADMIN LIVE EMOJI COMMANDS =================


def extract_custom_emoji_ids_from_message(message):
    found = []
    entities = list(getattr(message, "entities", None) or []) + list(getattr(message, "caption_entities", None) or [])
    for entity in entities:
        if getattr(entity, "type", "") == "custom_emoji":
            emoji_id = getattr(entity, "custom_emoji_id", "")
            if emoji_id:
                found.append(str(emoji_id))
    return found


def set_custom_emoji(kind, key, emoji_id, alt):
    kind = normalize_emoji_key(kind)
    emoji_id = str(emoji_id or "").strip()
    alt = str(alt or "").strip()[:8]

    if kind.startswith("default "):
        kind = "default_" + kind.split(" ", 1)[1]

    if kind in ("default_service", "default_country", "default_action"):
        default_kind = kind.replace("default_", "")
        old_alt = CUSTOM_EMOJI_CONFIG.get("defaults", {}).get(default_kind, {}).get("alt", LIVE_TEXT_FALLBACK)
        CUSTOM_EMOJI_CONFIG.setdefault("defaults", {})[default_kind] = {"id": emoji_id, "alt": alt or old_alt}
        save_custom_emoji_config()
        return f"Default {default_kind} live emoji saved."

    if kind not in ("service", "country", "action"):
        return None

    collection_name = f"{kind}s"
    normalized_key = normalize_emoji_key(key)
    if not normalized_key:
        return None
    old_item = CUSTOM_EMOJI_CONFIG.setdefault(collection_name, {}).get(normalized_key, {})
    fallback_alt = old_item.get("alt") or CUSTOM_EMOJI_CONFIG.get("defaults", {}).get(kind, {}).get("alt", LIVE_TEXT_FALLBACK)
    CUSTOM_EMOJI_CONFIG[collection_name][normalized_key] = {"id": emoji_id, "alt": alt or fallback_alt}
    save_custom_emoji_config()
    return f"{kind.title()} live emoji saved for: {normalized_key}"


@bot.message_handler(commands=['getemojiid'])
def get_emoji_id_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    ids = extract_custom_emoji_ids_from_message(message)
    if not ids:
        bot.reply_to(
            message,
            "Send a Telegram custom emoji with the command. Example:\n/getemojiid <live custom emoji>"
        )
        return
    bot.reply_to(message, "Custom emoji ID:\n" + "\n".join(f"<code>{escape(i)}</code>" for i in ids), parse_mode="HTML")


@bot.message_handler(commands=['setemoji'])
def set_emoji_command(message):
    if message.from_user.id != ADMIN_ID:
        return

    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        bot.reply_to(
            message,
            "Usage:\n"
            "/setemoji service WhatsApp 5368324170671202286 •\n"
            "/setemoji country Bangladesh 5368324170671202286 •\n"
            "/setemoji action get_number 5368324170671202286 •\n"
            "/setemoji default_service 5368324170671202286 •\n"
            "/setemoji default_country 5368324170671202286 •\n"
            "/setemoji default_action 5368324170671202286 •"
        )
        return

    args = raw[1].strip()
    first_parts = args.split(maxsplit=1)
    kind = first_parts[0].strip().lower()
    rest = first_parts[1].strip() if len(first_parts) > 1 else ""

    id_match = re.search(r"\b(\d{8,})\b", rest)
    if not id_match:
        bot.reply_to(message, "Custom emoji ID not found. Use /getemojiid to extract it first.")
        return

    emoji_id = id_match.group(1)
    name = rest[:id_match.start()].strip()
    alt = rest[id_match.end():].strip()

    if kind.startswith("default_"):
        name = kind
    elif not name:
        bot.reply_to(message, "Name missing. Example: /setemoji service WhatsApp 5368324170671202286 •")
        return

    result = set_custom_emoji(kind, name, emoji_id, alt)
    if not result:
        bot.reply_to(message, "Type must be service, country, action, default_service, default_country, or default_action.")
        return

    bot.reply_to(message, f"{escape(result)}", parse_mode="HTML")


@bot.message_handler(commands=['emojistats'])
def emoji_stats_command(message):
    if message.from_user.id != ADMIN_ID:
        return

    lines = [f"{live_action_html('stats')} <b>Live Emoji Status</b>", ""]
    for kind, collection_name in (("service", "services"), ("country", "countries"), ("action", "actions")):
        collection = CUSTOM_EMOJI_CONFIG.get(collection_name, {})
        total = len(collection)
        filled = sum(1 for item in collection.values() if str(item.get("id", "")).strip())
        default_ok = "OK" if CUSTOM_EMOJI_CONFIG.get("defaults", {}).get(kind, {}).get("id") else "Missing"
        lines.append(f"<b>{kind.title()}:</b> {filled}/{total} specific IDs, default {default_ok}")

    lines.append("\nIf a specific ID is missing, the bot uses the default live emoji ID for that type.")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=['emojihelp'])
def emoji_help_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(
        message,
        "1) Send a custom emoji with /getemojiid to get its ID.\n"
        "2) Save it:\n"
        "<code>/setemoji service WhatsApp 5368324170671202286 •</code>\n"
        "<code>/setemoji country Bangladesh 5368324170671202286 •</code>\n"
        "<code>/setemoji default_service 5368324170671202286 •</code>\n"
        "<code>/setemoji default_country 5368324170671202286 •</code>\n\n"
        "For custom/new services, set default_service so every service still shows a live emoji.",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['testcountrybuttons'])
def test_country_buttons_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    sample = [
        "Abkhazia", "Afghanistan", "Albania", "Algeria", "Bangladesh", "Benin",
        "Guyana", "India", "Nigeria", "United States", "United Kingdom", "Zimbabwe"
    ]
    markup = LiveInlineKeyboardMarkup(row_width=2)
    for country in sample:
        markup.add(inline_button(country, callback_data="noop", emoji_kind="country", emoji_key=country), row_width=2)
    lines = [f"{live_action_html('country')} <b>Country button test</b>", ""]
    for country in sample:
        lines.append(f"<code>{escape(country)}</code> = <code>{escape(str(get_exact_live_emoji_id('country', country)))}</code>")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'noop')
def noop_callback(call):
    bot.answer_callback_query(call.id)

# ================= ADMIN PANEL =================

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID: return
    markup = LiveReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(reply_button("Upload File", action_key="upload"), reply_button("Stock Stats", action_key="stats"))
    markup.add(reply_button("Manage Files", action_key="manage"), reply_button("Broadcast Center", action_key="broadcast"))
    markup.add(reply_button("Link Settings", action_key="manage"), reply_button("Pending Broadcasts", action_key="broadcast"))
    markup.add(reply_button("Number Settings", action_key="manage"), row_width=1)
    markup.add(reply_button("API Integrations", action_key="refresh", style="primary"), row_width=1)
    markup.add(reply_button("User Mode", action_key="back"))
    bot.reply_to(message, f"{live_action_html('admin')} <b>Admin Panel</b>", parse_mode="HTML", reply_markup=markup)


def toggle_action_label(title, setting_key):
    is_on = get_bool_setting(setting_key, False)
    return f"{title}: OFF করুন" if is_on else f"{title}: ON করুন"


API_BUTTON_HELP = [
    ("Sync", "Auto Sync ON থাকলে bot শুধু OTP check করবে; number user click ছাড়া আনবে না."),
    ("Interval", "কত সেকেন্ড পরপর auto sync চলবে."),
    ("Agent", "Agent API setting রাখা যাবে; number আগেভাগে sync হবে না."),
    ("FastX", "FastX API থেকে selected range-এর number শুধু user request করলে আসবে."),
    ("Agent Key/Base/CLI", "Agent API key, link, আর filter সেট করার জায়গা."),
    ("FastX Key/Base/Range", "FastX API key, link, আর number range/prefix সেট করার জায়গা."),
    ("Name/Country", "ফাইলের service/country নাম ঠিক করে, যেমন WhatsApp বা Telegram."),
    ("Sync Now / Check OTP", "Number cleanup/status দেখাবে বা OTP message check করবে."),
]


def build_api_integrations_markup():
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button(toggle_action_label("Auto Sync", "api_sync_enabled"), callback_data="apiint|toggle|api_sync_enabled", action_key="refresh"),
        inline_button("Set Interval", callback_data="apiint|set|api_sync_interval_seconds", action_key="manage"),
        row_width=2,
    )
    markup.add(
        inline_button(toggle_action_label("Agent API", "api_agent_enabled"), callback_data="apiint|toggle|api_agent_enabled", action_key="success"),
        inline_button(toggle_action_label("FastX API", "api_fastx_enabled"), callback_data="apiint|toggle|api_fastx_enabled", action_key="success"),
        row_width=2,
    )
    markup.add(
        inline_button("Set Agent Key", callback_data="apiint|set|api_agent_key", action_key="lock"),
        inline_button("Set Agent URL", callback_data="apiint|set|api_agent_base_url", action_key="manage"),
        row_width=2,
    )
    markup.add(
        inline_button("Set Agent Filter", callback_data="apiint|set|api_agent_cli", action_key="phone"),
        inline_button("Set Agent Service", callback_data="apiint|set|api_agent_service", action_key="service"),
        row_width=2,
    )
    markup.add(
        inline_button("Set Agent Country", callback_data="apiint|set|api_agent_country", action_key="country"),
        inline_button("Set FastX Key", callback_data="apiint|set|api_fastx_key", action_key="lock"),
        row_width=2,
    )
    markup.add(
        inline_button("Set FastX URL", callback_data="apiint|set|api_fastx_base_url", action_key="manage"),
        inline_button("Set FastX Range", callback_data="apiint|set|api_fastx_range", action_key="phone"),
        row_width=2,
    )
    markup.add(
        inline_button("Set FastX Service", callback_data="apiint|set|api_fastx_service", action_key="service"),
        inline_button("Set FastX Country", callback_data="apiint|set|api_fastx_country", action_key="country"),
        row_width=2,
    )
    markup.add(
        inline_button("Clean API Stock", callback_data="apiint|run|numbers", action_key="refresh"),
        inline_button("Check OTP Now", callback_data="apiint|run|otp", action_key="otp"),
        row_width=2,
    )
    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    return markup


def send_api_integrations_panel(chat_id, edit_message=None):
    help_lines = "\n".join(
        f"{live_action_html('verify')} <b>{escape(title)}:</b> {escape(details)}"
        for title, details in API_BUTTON_HELP
    )
    text = (
        f"{live_action_html('refresh')} <b>API Integrations</b>\n\n"
        f"<b>Auto Sync:</b> {'ON' if get_bool_setting('api_sync_enabled', False) else 'OFF'} every {get_api_sync_interval()}s\n\n"
        f"<b>Agent API:</b> {'ON' if get_bool_setting('api_agent_enabled', False) else 'OFF'}\n"
        f"Base: <code>{escape(get_app_setting('api_agent_base_url', DEFAULT_AGENT_BASE_URL))}</code>\n"
        f"Key: <code>{escape(mask_secret(get_app_setting('api_agent_key', '')))}</code>\n"
        f"CLI: <code>{escape(get_app_setting('api_agent_cli', '') or 'All')}</code>\n"
        f"Name: <code>{escape(get_app_setting('api_agent_service', 'WhatsApp'))}</code> | Country: <code>{escape(get_app_setting('api_agent_country', 'Unknown'))}</code>\n\n"
        f"<b>FastX OTP:</b> {'ON' if get_bool_setting('api_fastx_enabled', False) else 'OFF'}\n"
        f"Base: <code>{escape(get_app_setting('api_fastx_base_url', DEFAULT_FASTX_BASE_URL))}</code>\n"
        f"Key: <code>{escape(mask_secret(get_app_setting('api_fastx_key', '')))}</code>\n"
        f"Range: <code>{escape(get_app_setting('api_fastx_range', '') or 'Not set')}</code>\n"
        f"Name: <code>{escape(get_app_setting('api_fastx_service', 'WhatsApp'))}</code> | Country: <code>{escape(get_app_setting('api_fastx_country', 'Unknown'))}</code>\n\n"
        f"{help_lines}\n\n"
        "ON button চাপলে চালু হবে, OFF button চাপলে বন্ধ হবে. API endpoint 403/404 হলে bot এখন scary URL error না দেখিয়ে শান্তভাবে skip/report করবে."
    )
    return send_or_edit_message(chat_id, text, reply_markup=build_api_integrations_markup(), parse_mode="HTML", edit_message=edit_message)


def toggle_action_label(title, setting_key):
    is_on = get_bool_setting(setting_key, False)
    return f"{title}: OFF" if is_on else f"{title}: ON"


def build_api_integrations_markup():
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button(toggle_action_label("Auto Sync", "api_sync_enabled"), callback_data="apiint|toggle|api_sync_enabled", action_key="refresh"),
        inline_button("Live Traffic", callback_data="apiint|open|traffic", action_key="stats"),
        row_width=2,
    )
    markup.add(
        inline_button("Clean API Stock", callback_data="apiint|run|numbers", action_key="refresh"),
        inline_button("Check OTP", callback_data="apiint|run|otp", action_key="otp"),
        row_width=2,
    )
    markup.add(
        inline_button("FastX Settings", callback_data="apiint|panel|fastx", action_key="manage"),
        inline_button("Agent Settings", callback_data="apiint|panel|agent", action_key="manage"),
        row_width=2,
    )
    markup.add(
        inline_button("Extra APIs", callback_data="apiint|providers|list", action_key="manage"),
        inline_button("Add API", callback_data="apiint|addapi|start", action_key="success"),
        row_width=2,
    )
    markup.add(inline_button("Set Interval", callback_data="apiint|set|api_sync_interval_seconds", action_key="manage"), row_width=1)
    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    return markup


def build_api_provider_markup(provider):
    markup = LiveInlineKeyboardMarkup(row_width=2)
    if provider == "fastx":
        markup.add(
            inline_button(toggle_action_label("FastX API", "api_fastx_enabled"), callback_data="apiint|toggle|api_fastx_enabled", action_key="success"),
            inline_button("Live Traffic", callback_data="apiint|open|traffic", action_key="stats"),
            row_width=2,
        )
        markup.add(
            inline_button("API Key", callback_data="apiint|set|api_fastx_key", action_key="lock"),
            inline_button("Base URL", callback_data="apiint|set|api_fastx_base_url", action_key="manage"),
            row_width=2,
        )
        markup.add(
            inline_button("Range", callback_data="apiint|set|api_fastx_range", action_key="phone"),
            inline_button("Name", callback_data="apiint|set|api_fastx_service", action_key="service"),
            row_width=2,
        )
        markup.add(inline_button("Country", callback_data="apiint|set|api_fastx_country", action_key="country"), row_width=1)
    else:
        markup.add(inline_button(toggle_action_label("Agent API", "api_agent_enabled"), callback_data="apiint|toggle|api_agent_enabled", action_key="success"), row_width=1)
        markup.add(
            inline_button("API Key", callback_data="apiint|set|api_agent_key", action_key="lock"),
            inline_button("Base URL", callback_data="apiint|set|api_agent_base_url", action_key="manage"),
            row_width=2,
        )
        markup.add(
            inline_button("Filter", callback_data="apiint|set|api_agent_cli", action_key="phone"),
            inline_button("Name", callback_data="apiint|set|api_agent_service", action_key="service"),
            row_width=2,
        )
        markup.add(inline_button("Country", callback_data="apiint|set|api_agent_country", action_key="country"), row_width=1)
    markup.add(
        inline_button("Back", callback_data="apiint|panel|main", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close"),
        row_width=2,
    )
    return markup


def send_api_provider_panel(chat_id, provider, edit_message=None):
    if provider == "fastx":
        text = (
            f"{live_action_html('manage')} <b>FastX Settings</b>\n\n"
            f"<b>Status:</b> {'ON' if get_bool_setting('api_fastx_enabled', False) else 'OFF'}\n"
            f"<b>Base:</b> <code>{escape(get_app_setting('api_fastx_base_url', DEFAULT_FASTX_BASE_URL))}</code>\n"
            f"<b>Key:</b> <code>{escape(mask_secret(get_app_setting('api_fastx_key', '')))}</code>\n"
            f"<b>Range:</b> <code>{escape(get_app_setting('api_fastx_range', '') or 'Auto from live traffic')}</code>\n"
            f"<b>Name:</b> <code>{escape(get_app_setting('api_fastx_service', 'WhatsApp'))}</code>\n"
            f"<b>Country:</b> <code>{escape(get_app_setting('api_fastx_country', 'Unknown'))}</code>"
        )
    else:
        text = (
            f"{live_action_html('manage')} <b>Agent Settings</b>\n\n"
            f"<b>Status:</b> {'ON' if get_bool_setting('api_agent_enabled', False) else 'OFF'}\n"
            f"<b>Base:</b> <code>{escape(get_app_setting('api_agent_base_url', DEFAULT_AGENT_BASE_URL))}</code>\n"
            f"<b>Key:</b> <code>{escape(mask_secret(get_app_setting('api_agent_key', '')))}</code>\n"
            f"<b>Filter:</b> <code>{escape(get_app_setting('api_agent_cli', '') or 'All')}</code>\n"
            f"<b>Name:</b> <code>{escape(get_app_setting('api_agent_service', 'WhatsApp'))}</code>\n"
            f"<b>Country:</b> <code>{escape(get_app_setting('api_agent_country', 'Unknown'))}</code>"
        )
    return send_or_edit_message(chat_id, text, reply_markup=build_api_provider_markup(provider), parse_mode="HTML", edit_message=edit_message)


def extra_api_provider_page_count(total):
    return max(1, (int(total or 0) + EXTRA_API_PROVIDERS_PAGE_SIZE - 1) // EXTRA_API_PROVIDERS_PAGE_SIZE)


def clamp_extra_api_page(page, total):
    try:
        page = int(page)
    except Exception:
        page = 0
    return max(0, min(page, extra_api_provider_page_count(total) - 1))


def build_extra_api_providers_markup(page=0):
    markup = LiveInlineKeyboardMarkup(row_width=2)
    providers = get_extra_api_providers(enabled_only=False)
    page = clamp_extra_api_page(page, len(providers))
    start = page * EXTRA_API_PROVIDERS_PAGE_SIZE
    visible_providers = providers[start:start + EXTRA_API_PROVIDERS_PAGE_SIZE]
    for provider in visible_providers:
        token = api_provider_token(provider["id"])
        status = "ON" if provider["enabled"] else "OFF"
        markup.add(
            inline_button(f"{provider['name']}: {status}", callback_data=f"apiint|apitoggle|{token}", action_key="refresh"),
            inline_button("Edit", callback_data=f"apiint|apiview|{token}", action_key="manage"),
            row_width=2,
        )
    if extra_api_provider_page_count(len(providers)) > 1:
        previous_page = max(0, page - 1)
        next_page = min(extra_api_provider_page_count(len(providers)) - 1, page + 1)
        markup.add(
            inline_button("Prev", callback_data=f"apiint|providers|page:{previous_page}", action_key="back"),
            inline_button(f"{page + 1}/{extra_api_provider_page_count(len(providers))}", callback_data=f"apiint|providers|page:{page}", action_key="stats"),
            inline_button("Next", callback_data=f"apiint|providers|page:{next_page}", action_key="next"),
            row_width=3,
        )
    markup.add(
        inline_button("Add API", callback_data="apiint|addapi|start", action_key="success"),
        inline_button("Back", callback_data="apiint|panel|main", action_key="back"),
        row_width=2,
    )
    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    return markup


def send_extra_api_providers_panel(chat_id, edit_message=None, page=0):
    providers = get_extra_api_providers(enabled_only=False)
    page = clamp_extra_api_page(page, len(providers))
    start = page * EXTRA_API_PROVIDERS_PAGE_SIZE
    visible_providers = providers[start:start + EXTRA_API_PROVIDERS_PAGE_SIZE]
    lines = [
        f"{live_action_html('manage')} <b>Extra APIs</b>",
        "",
        f"Add up to <b>{MAX_EXTRA_API_PROVIDERS}</b> FastX-compatible panels/APIs here. Enabled APIs work for Live Traffic, Get Number, and OTP check.",
        f"Total: <b>{len(providers)}</b>/<b>{MAX_EXTRA_API_PROVIDERS}</b> | Page: <b>{page + 1}</b>/<b>{extra_api_provider_page_count(len(providers))}</b>",
    ]
    if visible_providers:
        for provider in visible_providers:
            lines.append(
                f"\n<b>{escape(provider['name'])}</b> - {'ON' if provider['enabled'] else 'OFF'}\n"
                f"Base: <code>{escape(provider['base_url'])}</code>\n"
                f"Key: <code>{escape(mask_secret(provider['api_key']))}</code>\n"
                f"Range: <code>{escape(provider['range'] or 'Auto from live traffic')}</code>"
            )
    else:
        lines.append("\nNo extra API added yet.")
    return send_or_edit_message(chat_id, "\n".join(lines), reply_markup=build_extra_api_providers_markup(page), parse_mode="HTML", edit_message=edit_message)


def build_extra_api_provider_edit_markup(provider):
    token = api_provider_token(provider["id"])
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Turn OFF" if provider["enabled"] else "Turn ON", callback_data=f"apiint|apitoggle|{token}", action_key="refresh"),
        inline_button("Delete", callback_data=f"apiint|apidel|{token}", action_key="warning"),
        row_width=2,
    )
    markup.add(
        inline_button("Name", callback_data=f"apiint|apiset|{token}:name", action_key="manage"),
        inline_button("Base URL", callback_data=f"apiint|apiset|{token}:base_url", action_key="manage"),
        row_width=2,
    )
    markup.add(
        inline_button("API Key", callback_data=f"apiint|apiset|{token}:api_key", action_key="lock"),
        inline_button("Range", callback_data=f"apiint|apiset|{token}:range_prefix", action_key="phone"),
        row_width=2,
    )
    markup.add(
        inline_button("Service", callback_data=f"apiint|apiset|{token}:service", action_key="service"),
        inline_button("Country", callback_data=f"apiint|apiset|{token}:country", action_key="country"),
        row_width=2,
    )
    markup.add(
        inline_button("Back", callback_data="apiint|providers|list", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close"),
        row_width=2,
    )
    return markup


def send_extra_api_provider_edit_panel(chat_id, provider, edit_message=None):
    text = (
        f"{live_action_html('manage')} <b>{escape(provider['name'])}</b>\n\n"
        f"<b>Status:</b> {'ON' if provider['enabled'] else 'OFF'}\n"
        f"<b>Base:</b> <code>{escape(provider['base_url'])}</code>\n"
        f"<b>Key:</b> <code>{escape(mask_secret(provider['api_key']))}</code>\n"
        f"<b>Range:</b> <code>{escape(provider['range'] or 'Auto from live traffic')}</code>\n"
        f"<b>Service:</b> <code>{escape(provider['service'])}</code>\n"
        f"<b>Country:</b> <code>{escape(provider['country'])}</code>"
    )
    return send_or_edit_message(chat_id, text, reply_markup=build_extra_api_provider_edit_markup(provider), parse_mode="HTML", edit_message=edit_message)


def send_api_integrations_panel(chat_id, edit_message=None):
    text = (
        f"{live_action_html('refresh')} <b>API Integrations</b>\n\n"
        f"<b>Auto Sync:</b> {'ON' if get_bool_setting('api_sync_enabled', False) else 'OFF'} every {get_api_sync_interval()}s\n"
        f"<b>FastX:</b> {'ON' if get_bool_setting('api_fastx_enabled', False) else 'OFF'} | Range: <code>{escape(get_app_setting('api_fastx_range', '') or 'Live selected')}</code>\n"
        f"<b>Agent:</b> {'ON' if get_bool_setting('api_agent_enabled', False) else 'OFF'} | Filter: <code>{escape(get_app_setting('api_agent_cli', '') or 'All')}</code>\n\n"
        f"<b>Extra APIs:</b> {len(get_extra_api_providers(enabled_only=False))}\n\n"
        "Live Traffic shows ranges from every enabled API. OTPs from API or uploaded files are delivered to the user who took that number."
    )
    return send_or_edit_message(chat_id, text, reply_markup=build_api_integrations_markup(), parse_mode="HTML", edit_message=edit_message)


@bot.message_handler(func=lambda m: m.text == 'API Integrations' and m.from_user.id == ADMIN_ID)
def api_integrations_menu(message):
    send_api_integrations_panel(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("apiint|"))
def api_integrations_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    _prefix, action, key = call.data.split("|", 2)
    if action == "panel":
        bot.answer_callback_query(call.id)
        if key == "main":
            send_api_integrations_panel(call.message.chat.id, edit_message=call.message)
        else:
            send_api_provider_panel(call.message.chat.id, key, edit_message=call.message)
        return
    if action == "providers":
        bot.answer_callback_query(call.id)
        page = 0
        if key.startswith("page:"):
            page = key.split(":", 1)[1]
        send_extra_api_providers_panel(call.message.chat.id, edit_message=call.message, page=page)
        return
    if action == "addapi":
        if count_extra_api_providers() >= MAX_EXTRA_API_PROVIDERS:
            bot.answer_callback_query(call.id, f"Maximum {MAX_EXTRA_API_PROVIDERS} APIs already added.", show_alert=True)
            return
        pending_api_provider_actions[call.from_user.id] = ("add", None, None)
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id,
            (
                f"{live_action_html('manage')} Send API details.\n\n"
                "Format:\n"
                "<code>https://example.com|API_KEY|API-2|WhatsApp|Bangladesh|optional_range</code>\n\n"
                f"Only URL and API key are required. Limit: {MAX_EXTRA_API_PROVIDERS} panels/APIs."
            ),
            parse_mode="HTML",
        )
        bot.register_next_step_handler(msg, handle_extra_api_provider_step)
        return
    if action == "apiview":
        provider = resolve_extra_api_provider_token(key)
        if not provider:
            bot.answer_callback_query(call.id, "API not found.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        send_extra_api_provider_edit_panel(call.message.chat.id, provider, edit_message=call.message)
        return
    if action == "apitoggle":
        provider = resolve_extra_api_provider_token(key)
        if not provider:
            bot.answer_callback_query(call.id, "API not found.", show_alert=True)
            return
        update_extra_api_provider(provider["id"], "enabled", 0 if provider["enabled"] else 1)
        bot.answer_callback_query(call.id, "Updated.")
        send_extra_api_providers_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "apidel":
        provider = resolve_extra_api_provider_token(key)
        if not provider:
            bot.answer_callback_query(call.id, "API not found.", show_alert=True)
            return
        delete_extra_api_provider(provider["id"])
        bot.answer_callback_query(call.id, "Deleted.")
        send_extra_api_providers_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "apiset":
        token, field = key.split(":", 1) if ":" in key else (key, "")
        provider = resolve_extra_api_provider_token(token)
        if not provider or field not in {"name", "base_url", "api_key", "service", "country", "range_prefix"}:
            bot.answer_callback_query(call.id, "API setting not found.", show_alert=True)
            return
        pending_api_provider_actions[call.from_user.id] = ("set", provider["id"], field)
        labels = {
            "name": "Send API name. Example: <code>API-2</code>",
            "base_url": "Send API base URL. Example: <code>https://fastxotps.com</code>",
            "api_key": "Send API key.",
            "service": "Send default service. Example: <code>WhatsApp</code>",
            "country": "Send default country. Example: <code>Bangladesh</code>",
            "range_prefix": "Send default range/prefix, or <code>off</code> for auto.",
        }
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, f"{live_action_html('manage')} {labels[field]}", parse_mode="HTML")
        bot.register_next_step_handler(msg, handle_extra_api_provider_step)
        return
    if action == "open" and key == "traffic":
        bot.answer_callback_query(call.id)
        send_live_traffic(call.message.chat.id, edit_message=call.message)
        return
    if action == "toggle":
        set_bool_setting(key, not get_bool_setting(key, False))
        bot.answer_callback_query(call.id, "Updated.")
        if key.startswith("api_fastx_"):
            send_api_provider_panel(call.message.chat.id, "fastx", edit_message=call.message)
        elif key.startswith("api_agent_"):
            send_api_provider_panel(call.message.chat.id, "agent", edit_message=call.message)
        else:
            send_api_integrations_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "run":
        bot.answer_callback_query(call.id, "Running...")
        if key == "numbers":
            cleared = cleanup_available_api_stock()
            removed = cleanup_empty_api_files()
            text = (
                f"{live_action_html('success')} On-demand mode is active.\n"
                "Numbers will be fetched only when a user taps Get Number or a live range."
            )
            if cleared:
                text += f"\n{live_action_html('delete')} Cleared <b>{cleared}</b> unused API number(s)."
            if removed:
                text += f"\n{live_action_html('delete')} Removed <b>{removed}</b> empty API file(s)."
            bot.send_message(call.message.chat.id, text, parse_mode="HTML")
        elif key == "otp":
            delivered = poll_api_otps_once()
            bot.send_message(call.message.chat.id, f"{live_action_html('message_otp')} OTP check complete. Delivered <b>{delivered}</b> message(s).", parse_mode="HTML")
        return
    prompts = {
        "api_sync_interval_seconds": "Send sync interval in seconds. Example: <code>20</code>",
        "api_agent_key": "Send Agent API key.",
        "api_agent_base_url": "Send Agent base URL.",
        "api_agent_cli": "Send Agent CLI/name filter. Send <code>off</code> for all.",
        "api_agent_service": "Send default Agent service name. Example: <code>WhatsApp</code>",
        "api_agent_country": "Send default Agent country. Example: <code>Bangladesh</code>",
        "api_fastx_key": "Send FastX API key.",
        "api_fastx_base_url": "Send FastX base URL.",
        "api_fastx_range": "Send FastX range/prefix. Example: <code>26134XXX</code>",
        "api_fastx_service": "Send default FastX service name. Example: <code>WhatsApp</code>",
        "api_fastx_country": "Send default FastX country. Example: <code>Bangladesh</code>",
    }
    if action != "set" or key not in prompts:
        return
    pending_api_setting_actions[call.from_user.id] = key
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, f"{live_action_html('manage')} {prompts[key]}", parse_mode="HTML")
    bot.register_next_step_handler(msg, handle_api_setting_step)


def handle_extra_api_provider_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    action = pending_api_provider_actions.pop(message.from_user.id, None)
    if not action:
        return
    mode, provider_id, field = action
    value = str(message.text or "").strip()
    if mode == "add":
        parts = [part.strip() for part in re.split(r"[|\n]+", value) if part.strip()]
        if len(parts) < 2:
            bot.reply_to(
                message,
                f"{live_action_html('warning')} Send at least URL and API key, separated by <code>|</code> or new line.",
                parse_mode="HTML",
            )
            return
        base_url, api_key = parts[0], parts[1]
        if not re.match(r"^https?://", base_url, re.IGNORECASE):
            bot.reply_to(message, f"{live_action_html('warning')} URL must start with http:// or https://", parse_mode="HTML")
            return
        next_number = len(get_extra_api_providers(enabled_only=False)) + 2
        name = parts[2] if len(parts) > 2 else f"API-{next_number}"
        service = parts[3] if len(parts) > 3 else "WhatsApp"
        country = parts[4] if len(parts) > 4 else "Unknown"
        range_prefix = parts[5] if len(parts) > 5 else ""
        try:
            provider_id = save_extra_api_provider(name, base_url, api_key, service, country, range_prefix)
        except ValueError as exc:
            bot.reply_to(message, f"{live_action_html('warning')} {escape(str(exc))}", parse_mode="HTML")
            return
        provider = get_api_provider_by_key(f"extra:{provider_id}")
        bot.reply_to(message, f"{live_action_html('success')} Added <b>{escape(provider['name'])}</b>. It is ON now.", parse_mode="HTML")
        send_extra_api_provider_edit_panel(message.chat.id, provider)
        return

    if mode == "set":
        if field == "base_url" and not re.match(r"^https?://", value, re.IGNORECASE):
            bot.reply_to(message, f"{live_action_html('warning')} URL must start with http:// or https://", parse_mode="HTML")
            return
        if field == "range_prefix" and value.lower() in {"off", "none", "auto"}:
            value = ""
        if field == "enabled":
            value = 1 if value.lower() in {"1", "on", "yes", "true"} else 0
        update_extra_api_provider(provider_id, field, value)
        provider = get_api_provider_by_key(f"extra:{provider_id}")
        shown = mask_secret(value) if field == "api_key" else (value or "Auto")
        bot.reply_to(message, f"{live_action_html('success')} Saved: <code>{escape(str(shown))}</code>", parse_mode="HTML")
        send_extra_api_provider_edit_panel(message.chat.id, provider)


def handle_api_setting_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    key = pending_api_setting_actions.pop(message.from_user.id, None)
    if not key:
        return
    value = str(message.text or "").strip()
    if value.lower() in {"off", "none", "all"} and key == "api_agent_cli":
        value = ""
    if key == "api_sync_interval_seconds":
        match = re.search(r"\d+", value)
        if not match:
            bot.reply_to(message, f"{live_action_html('warning')} Send a valid number.", parse_mode="HTML")
            return
        value = str(max(5, min(3600, int(match.group(0)))))
    if key.endswith("_base_url") and not re.match(r"^https?://", value, re.IGNORECASE):
        bot.reply_to(message, f"{live_action_html('warning')} URL must start with http:// or https://", parse_mode="HTML")
        return
    set_app_setting(key, value)
    shown = mask_secret(value) if key.endswith("_key") else (value or "All")
    bot.reply_to(message, f"{live_action_html('success')} Saved: <code>{escape(shown)}</code>", parse_mode="HTML")


def build_subscription_admin_markup():
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Add Premium", callback_data="sub|add", action_key="success"),
        inline_button("Remove Premium", callback_data="sub|remove", action_key="warning"),
        row_width=2
    )
    markup.add(
        inline_button("Check User", callback_data="sub|check", action_key="stats"),
        inline_button("Premium Users", callback_data="sub|list", action_key="verify"),
        row_width=2
    )
    markup.add(
        inline_button("Ban User", callback_data="sub|ban", action_key="lock"),
        inline_button("Unban User", callback_data="sub|unban", action_key="success"),
        row_width=2
    )
    markup.add(
        inline_button("Payment Numbers", callback_data="sub|payments", action_key="manage"),
        inline_button("Pending Payments", callback_data="sub|pending", action_key="broadcast"),
        row_width=2
    )
    markup.add(
        inline_button("Plan Price/Days", callback_data="sub|plan", action_key="manage"),
        inline_button("Auto Payment", callback_data="sub|auto_payment", action_key="refresh"),
        row_width=2
    )
    markup.add(
        inline_button("Binance API", callback_data="sub|binance_api", action_key="manage"),
        inline_button("Payment License", callback_data="sub|payment_license", action_key="lock"),
        row_width=2
    )
    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    return markup


def send_subscription_admin_panel(chat_id, edit_message=None):
    amount_text, days_text = get_payment_plan_values()
    local_price = f"{amount_text} BDT" if amount_text else "Not set"
    binance_price = "Not set"
    if amount_text:
        try:
            binance_price = f"${float(amount_text) / BDT_PER_USD:.2f}"
        except Exception:
            binance_price = amount_text
    days_display = f"{days_text} days" if days_text else "Not set"
    text = (
        f"{live_action_html('verify')} <b>Subscription Control</b>\n\n"
        f"<b>Local Price:</b> {escape(local_price)}\n"
        f"<b>Binance Price:</b> {escape(binance_price)}\n"
        f"<b>Plan Days:</b> {escape(days_display)}\n\n"
        f"<b>Payment System:</b> {escape(payment_system_active_text())}\n\n"
        "Payment requests, premium users, and ban/unban controls are managed here."
    )
    return send_or_edit_message(chat_id, text, reply_markup=build_subscription_admin_markup(), parse_mode="HTML", edit_message=edit_message)


def send_auto_payment_admin_panel(chat_id, edit_message=None):
    enabled = "ON" if get_bool_setting("auto_payment_bridge_enabled", True) else "OFF"
    port = get_int_setting("auto_payment_bridge_port", AUTO_PAYMENT_BRIDGE_PORT, 1024, 65535)
    secret = get_app_setting("auto_payment_bridge_secret", "")
    url = f"http://YOUR_PC_IP:{port}/sms?secret={secret}&sender={{sender}}&text={{message}}"
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT txid, method, amount_bdt, matched_request_id, status, created_at
           FROM auto_payment_events
           ORDER BY id DESC
           LIMIT 8"""
    ).fetchall()
    conn.close()
    lines = [
        f"{live_action_html('refresh')} <b>Auto Payment SMS Bridge</b>",
        "",
        f"<b>License:</b> {escape(payment_system_active_text())}",
        f"<b>Status:</b> {enabled}",
        f"<b>Port:</b> <code>{port}</code>",
        f"<b>Secret:</b> <code>{escape(secret)}</code>",
        "",
        "<b>SMS Forwarder URL:</b>",
        f"<code>{escape(url)}</code>",
        "",
        "Phone SMS Forwarder app must send sender/from and text/body/message to this URL.",
    ]
    if rows:
        lines.extend(["", "<b>Recent Auto Logs:</b>"])
        for txid, method, amount_bdt, request_id, status, created_at in rows:
            time_text = time.strftime("%m-%d %H:%M", time.localtime(float(created_at or 0)))
            lines.append(
                f"{escape(time_text)} | {escape(status)} | {escape(payment_method_label(method))} | "
                f"<code>{escape(str(txid or '-'))}</code> | {escape(str(amount_bdt or 0))} BDT | "
                f"<code>{escape(str(request_id or '-'))}</code>"
            )
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Back", callback_data="subadminback", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close"),
        row_width=2
    )
    return send_or_edit_message(chat_id, "\n".join(lines), reply_markup=markup, parse_mode="HTML", edit_message=edit_message)


def build_payment_license_markup():
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Generate 1 Month Key", callback_data="paylic|generate30", action_key="success"),
        inline_button("Redeem Key", callback_data="paylic|redeem", action_key="verify"),
        row_width=2
    )
    markup.add(inline_button("List Keys", callback_data="paylic|list", action_key="stats"), row_width=1)
    markup.add(
        inline_button("Back", callback_data="subadminback", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close"),
        row_width=2
    )
    return markup


def send_payment_license_panel(chat_id, edit_message=None):
    status = get_license_status(DB_FILE_NUMBER)
    if status["active"]:
        active_until = time.strftime("%Y-%m-%d %H:%M", time.localtime(status["active_until"]))
        status_text = f"ACTIVE until {active_until}"
    else:
        status_text = "INACTIVE"
    text = (
        f"{live_action_html('lock')} <b>Payment System License</b>\n\n"
        f"<b>Status:</b> {escape(status_text)}\n"
        f"<b>Time Left:</b> {status['seconds_left'] // 86400} days\n"
        f"<b>Last Key:</b> <code>{escape(mask_secret(status.get('last_key', ''), 6))}</code>\n\n"
        "Generate activation keys here. Redeeming one key activates the auto payment system for 30 days."
    )
    return send_or_edit_message(chat_id, text, reply_markup=build_payment_license_markup(), parse_mode="HTML", edit_message=edit_message)


def build_binance_api_admin_markup():
    settings = get_binance_auto_settings()
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("API Key", callback_data="binanceapi|api_key", action_key="manage"),
        inline_button("API Secret", callback_data="binanceapi|api_secret", action_key="lock"),
        row_width=2
    )
    markup.add(
        inline_button("Binance ID", callback_data="binanceapi|binance_id", action_key="manage"),
        inline_button("Currency", callback_data="binanceapi|currency", action_key="refresh"),
        row_width=2
    )
    markup.add(
        inline_button("Verify Window", callback_data="binanceapi|window", action_key="refresh"),
        inline_button("Turn OFF" if settings["enabled"] else "Turn ON", callback_data="binanceapi|toggle", action_key="refresh"),
        row_width=2
    )
    markup.add(inline_button("USDT Price/Days", callback_data="binanceapi|plan", action_key="manage"), row_width=1)
    markup.add(
        inline_button("Back", callback_data="subadminback", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close"),
        row_width=2
    )
    return markup


def send_binance_api_admin_panel(chat_id, edit_message=None):
    settings = get_binance_auto_settings()
    binance_amount, binance_days = get_binance_plan_values()
    text = (
        f"{live_action_html('manage')} <b>Binance Personal API</b>\n\n"
        f"<b>License:</b> {escape(payment_system_active_text())}\n"
        f"<b>Auto Verify:</b> {'ON' if settings['enabled'] else 'OFF'}\n"
        f"<b>API Key:</b> <code>{escape(mask_secret(settings['api_key']))}</code>\n"
        f"<b>API Secret:</b> <code>{escape(mask_secret(settings['api_secret']))}</code>\n"
        f"<b>Binance ID:</b> <code>{escape(settings['binance_id'] or 'Not set')}</code>\n"
        f"<b>Currency:</b> <code>{escape(settings['currency'])}</code>\n"
        f"<b>Verify Window:</b> {settings['window_minutes']} minutes\n"
        f"<b>Rate:</b> {escape(binance_amount or 'Not set')} {escape(settings['currency'])} = {escape(binance_days or 'Not set')} days\n"
        f"<b>Auto Days:</b> Paid amount will be converted by this rate.\n\n"
        "Keep only Enable Reading ON in Binance API permissions. Trading and Withdrawal must stay OFF."
    )
    return send_or_edit_message(chat_id, text, reply_markup=build_binance_api_admin_markup(), parse_mode="HTML", edit_message=edit_message)


def build_number_settings_markup():
    markup = LiveInlineKeyboardMarkup(row_width=2)
    markup.add(
        inline_button("Set Number Count", callback_data="numset|free_count", action_key="manage"),
        inline_button("Set Wait Time", callback_data="numset|free_cooldown", action_key="refresh"),
        row_width=2
    )
    markup.add(inline_button("File Access", callback_data="num|freefiles", action_key="manage"), row_width=1)
    free_enabled = get_bool_setting("free_used_numbers_enabled", True)
    markup.add(
        inline_button(
            "Turn OFF Free Numbers" if free_enabled else "Turn ON Free Numbers",
            callback_data="numtoggle|free_used_numbers_enabled",
            action_key="refresh"
        ),
        row_width=1
    )
    markup.add(inline_button("Close", callback_data="close", action_key="close"), row_width=1)
    return markup


def send_number_settings_panel(chat_id, edit_message=None):
    free_enabled = "ON" if get_bool_setting("free_used_numbers_enabled", True) else "OFF"
    text = (
        f"{live_action_html('manage')} <b>Number Settings</b>\n\n"
        f"<b>Numbers:</b> {get_free_numbers_per_assignment()} per request\n"
        f"<b>Change Wait Time:</b> {escape(format_seconds(get_free_change_number_cooldown()))}\n"
        f"<b>Live Number System:</b> {free_enabled}\n\n"
        "Users receive numbers from files that are enabled for live access and have no OTP yet."
    )
    return send_or_edit_message(chat_id, text, reply_markup=build_number_settings_markup(), parse_mode="HTML", edit_message=edit_message)


@bot.message_handler(func=lambda m: m.text == 'Number Settings' and m.from_user.id == ADMIN_ID)
def number_settings_menu(message):
    send_number_settings_panel(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "numsettingsback")
def number_settings_back(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_number_settings_panel(call.message.chat.id, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("num|"))
def number_settings_nav_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    action = call.data.split("|", 1)[1]
    bot.answer_callback_query(call.id)
    if action == "premiumfiles":
        text, markup, total = build_premium_files_content()
        if total <= 0:
            bot.send_message(call.message.chat.id, f"{live_action_html('warning')} No files found.", parse_mode="HTML")
        else:
            send_or_edit_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML", edit_message=call.message)
    if action == "freefiles":
        text, markup, total = build_free_files_content()
        if total <= 0:
            bot.send_message(call.message.chat.id, f"{live_action_html('warning')} No files found.", parse_mode="HTML")
        else:
            send_or_edit_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML", edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("numtoggle|"))
def number_settings_toggle_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    setting_key = call.data.split("|", 1)[1]
    current = get_bool_setting(setting_key, True)
    set_bool_setting(setting_key, not current)
    bot.answer_callback_query(call.id, "Updated.")
    send_number_settings_panel(call.message.chat.id, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("numset|"))
def number_settings_set_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    action = call.data.split("|", 1)[1]
    prompts = {
        "free_count": "Send how many numbers users get per request. Example: <code>2</code>",
        "free_cooldown": "Send user wait time. Example: <code>60</code> seconds or <code>1m</code>.",
    }
    if action not in prompts:
        return
    pending_number_setting_actions[call.from_user.id] = action
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, f"{live_action_html('manage')} {prompts[action]}", parse_mode="HTML")
    bot.register_next_step_handler(msg, handle_number_setting_step)


def handle_number_setting_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    action = pending_number_setting_actions.pop(message.from_user.id, None)
    if not action:
        return
    raw = str(message.text or "").strip()
    if action in {"free_count", "premium_count"}:
        match = re.search(r"\d+", raw)
        if not match:
            bot.reply_to(message, f"{live_action_html('warning')} Send a valid number.", parse_mode="HTML")
            return
        value = int(match.group(0))
        if action == "free_count":
            value = max(1, min(20, value))
            set_app_setting("free_numbers_per_assignment", str(value))
            label = "User numbers"
        else:
            value = max(1, min(50, value))
            set_app_setting("premium_numbers_per_assignment", str(value))
            label = "Premium user numbers"
        bot.reply_to(message, f"{live_action_html('success')} {label} set to <b>{value}</b> per request.", parse_mode="HTML")
        return
    if action == "free_cooldown":
        seconds = parse_duration_seconds(raw)
        if seconds is None:
            bot.reply_to(message, f"{live_action_html('warning')} Send seconds or minutes. Example: <code>60</code> or <code>1m</code>.", parse_mode="HTML")
            return
        seconds = max(0, min(86400, seconds))
        set_app_setting("free_change_number_cooldown", str(seconds))
        bot.reply_to(message, f"{live_action_html('success')} Wait time set to <b>{escape(format_seconds(seconds))}</b>.", parse_mode="HTML")


def build_payment_numbers_admin_markup():
    markup = LiveInlineKeyboardMarkup(row_width=2)
    buttons = []
    for method, info in PAYMENT_METHODS.items():
        buttons.append(inline_button(f"Edit {info['label']}", callback_data=f"payset|{method}", action_key="manage"))
    markup.add(*buttons, row_width=2)
    markup.add(inline_button("Back", callback_data="subadminback", action_key="back"), row_width=1)
    return markup


def send_payment_numbers_admin_panel(chat_id, edit_message=None):
    lines = [f"{live_action_html('manage')} <b>Payment Numbers</b>", ""]
    for method, info in PAYMENT_METHODS.items():
        lines.append(f"<b>{escape(info['label'])}:</b> <code>{escape(get_app_setting(info['setting'], 'Not set'))}</code>")
    amount_text, days_text = get_payment_plan_values()
    lines.append("")
    lines.append(f"<b>Plan Amount:</b> {escape((amount_text + ' BDT') if amount_text else 'Not set')}")
    lines.append(f"<b>Plan Days:</b> {escape((days_text + ' days') if days_text else 'Not set')}")
    return send_or_edit_message(
        chat_id,
        "\n".join(lines),
        reply_markup=build_payment_numbers_admin_markup(),
        parse_mode="HTML",
        edit_message=edit_message
    )


def send_pending_payment_requests(chat_id):
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT request_id, user_id, method, payment_identifier, requested_at
           FROM subscription_requests
           WHERE status='pending'
           ORDER BY requested_at ASC
           LIMIT 20"""
    ).fetchall()
    conn.close()
    if not rows:
        bot.send_message(chat_id, f"{live_action_html('warning')} No pending payment requests.", parse_mode="HTML")
        return
    for request_id, user_id, method, payment_identifier, requested_at in rows:
        send_subscription_request_to_admin(request_id)


@bot.message_handler(func=lambda m: m.text == 'Subscriptions' and m.from_user.id == ADMIN_ID)
def subscriptions_menu(message):
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.reply_to(message, f"{live_action_html('get_number')} Subscription control is disabled.", parse_mode="HTML")
        return
    send_subscription_admin_panel(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "subadminback")
def subscription_admin_back(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Subscription control is disabled.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_subscription_admin_panel(call.message.chat.id, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("payset|"))
def payment_number_edit_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Payment settings are disabled.", show_alert=True)
        return
    method = call.data.split("|", 1)[1]
    info = PAYMENT_METHODS.get(method)
    if not info:
        bot.answer_callback_query(call.id, "Payment method not found.", show_alert=True)
        return
    pending_payment_setting_updates[call.from_user.id] = method
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        f"{live_action_html('manage')} Send new <b>{escape(info['label'])}</b> number/ID:",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, save_payment_number_step)


def save_payment_number_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    method = pending_payment_setting_updates.pop(message.from_user.id, None)
    info = PAYMENT_METHODS.get(method)
    if not info:
        return
    value = str(message.text or "").strip()
    if len(value) < 2:
        bot.reply_to(message, f"{live_action_html('warning')} Number/ID is too short.", parse_mode="HTML")
        return
    set_app_setting(info["setting"], value)
    bot.reply_to(
        message,
        f"{live_action_html('success')} {escape(info['label'])} number updated:\n<code>{escape(value)}</code>",
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("paylic|"))
def payment_license_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Payment system is disabled.", show_alert=True)
        return
    action = call.data.split("|", 1)[1]
    if action == "generate30":
        key = generate_activation_key(DB_FILE_NUMBER, 30)
        bot.answer_callback_query(call.id, "Key generated.")
        bot.send_message(
            call.message.chat.id,
            (
                f"{live_action_html('success')} <b>1 Month Activation Key</b>\n\n"
                f"<code>{escape(key)}</code>\n\n"
                "Give this key to activate the payment system for 30 days."
            ),
            parse_mode="HTML"
        )
        send_payment_license_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "redeem":
        pending_payment_license_updates[call.from_user.id] = "redeem"
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, f"{live_action_html('verify')} Send activation key:", parse_mode="HTML")
        bot.register_next_step_handler(msg, redeem_payment_license_step)
        return
    if action == "list":
        rows = list_activation_keys(DB_FILE_NUMBER, 20)
        if not rows:
            bot.answer_callback_query(call.id, "No keys found.", show_alert=True)
            return
        lines = [f"{live_action_html('stats')} <b>Activation Keys</b>"]
        for key, days, status, created_at, redeemed_at, redeemed_by in rows:
            created = time.strftime("%m-%d %H:%M", time.localtime(float(created_at or 0)))
            lines.append(
                f"<code>{escape(mask_secret(key, 6))}</code> | {days}d | {escape(status)} | {escape(created)}"
            )
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="HTML")
        return


def redeem_payment_license_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    pending_payment_license_updates.pop(message.from_user.id, None)
    ok, result, active_until = redeem_activation_key(DB_FILE_NUMBER, message.text, message.from_user.id)
    if not ok:
        bot.reply_to(message, f"{live_action_html('warning')} {escape(result)}", parse_mode="HTML")
        return
    until_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(active_until))
    bot.reply_to(
        message,
        f"{live_action_html('success')} Payment system activated until <b>{escape(until_text)}</b>.",
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("binanceapi|"))
def binance_api_setting_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Payment system is disabled.", show_alert=True)
        return
    action = call.data.split("|", 1)[1]
    if action == "toggle":
        current = get_bool_setting("binance_auto_verify_enabled", True)
        set_bool_setting("binance_auto_verify_enabled", not current)
        bot.answer_callback_query(call.id, "Updated.")
        send_binance_api_admin_panel(call.message.chat.id, edit_message=call.message)
        return
    prompts = {
        "api_key": "Send Binance API Key.",
        "api_secret": "Send Binance API Secret.",
        "binance_id": "Send your Binance ID.",
        "currency": "Send payment currency. Example: <code>USDT</code>",
        "window": "Send verification window in minutes. Example: <code>15</code>",
        "plan": "Send Binance rate. Example: <code>1/15</code> means 1 USDT = 15 days; 1.5 USDT becomes 22 days automatically.",
    }
    if action not in prompts:
        bot.answer_callback_query(call.id, "Unknown setting.", show_alert=True)
        return
    pending_binance_api_updates[call.from_user.id] = action
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, f"{live_action_html('manage')} {prompts[action]}", parse_mode="HTML")
    bot.register_next_step_handler(msg, save_binance_api_setting_step)


def save_binance_api_setting_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    action = pending_binance_api_updates.pop(message.from_user.id, None)
    if not action:
        return
    value = str(message.text or "").strip()
    setting_map = {
        "api_key": "binance_api_key",
        "api_secret": "binance_api_secret",
        "binance_id": "binance_id",
        "currency": "binance_payment_currency",
        "window": "binance_verify_window_minutes",
        "plan": "binance_plan_usdt",
    }
    if action == "plan":
        plan_match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*[/\-\s,|]+\s*(\d+)\s*$", value)
        if not plan_match:
            bot.reply_to(message, f"{live_action_html('warning')} Send amount and days. Example: <code>5/30</code> or <code>5 30</code>", parse_mode="HTML")
            return
        amount = plan_match.group(1)
        days = plan_match.group(2)
        set_app_setting("binance_plan_usdt", amount)
        set_app_setting("binance_plan_days", days)
        bot.reply_to(
            message,
            (
                f"{live_action_html('success')} Binance rate updated:\n"
                f"<b>{escape(amount)} {escape(get_app_setting('binance_payment_currency', 'USDT') or 'USDT')}</b> = <b>{escape(days)} days</b>\n"
                "Paid amount will be converted automatically by this rate."
            ),
            parse_mode="HTML"
        )
        return
    if action == "window":
        match = re.search(r"\d+", value)
        if not match:
            bot.reply_to(message, f"{live_action_html('warning')} Send minutes as a number.", parse_mode="HTML")
            return
        value = str(max(1, min(1440, int(match.group(0)))))
    elif action == "currency":
        value = re.sub(r"[^A-Za-z0-9]+", "", value).upper() or "USDT"
    elif len(value) < 2:
        bot.reply_to(message, f"{live_action_html('warning')} Value is too short.", parse_mode="HTML")
        return
    set_app_setting(setting_map[action], value)
    shown = mask_secret(value) if action in {"api_key", "api_secret"} else value
    bot.reply_to(message, f"{live_action_html('success')} Binance setting updated: <code>{escape(shown)}</code>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("subtoggle|"))
def subscription_toggle_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Subscription control is disabled.", show_alert=True)
        return
    setting_key = call.data.split("|", 1)[1]
    current = get_bool_setting(setting_key, True)
    set_bool_setting(setting_key, not current)
    bot.answer_callback_query(call.id, "Updated.")
    send_subscription_admin_panel(call.message.chat.id, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith("sub|"))
def subscription_action_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Subscription control is disabled.", show_alert=True)
        return
    action = call.data.split("|", 1)[1]
    bot.answer_callback_query(call.id)
    if action == "payments":
        send_payment_numbers_admin_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "pending":
        send_pending_payment_requests(call.message.chat.id)
        return
    if action == "auto_payment":
        send_auto_payment_admin_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "binance_api":
        send_binance_api_admin_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "payment_license":
        send_payment_license_panel(call.message.chat.id, edit_message=call.message)
        return
    if action == "freefiles":
        text, markup, total = build_free_files_content()
        if total <= 0:
            bot.send_message(call.message.chat.id, f"{live_action_html('warning')} No files found.", parse_mode="HTML")
        else:
            send_or_edit_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML", edit_message=call.message)
        return
    if action == "list":
        conn = get_db_connection()
        rows = conn.execute(
            """SELECT user_id, premium_until FROM users
               WHERE plan='premium'
               ORDER BY premium_until DESC, user_id ASC
               LIMIT 50"""
        ).fetchall()
        conn.close()
        if not rows:
            bot.send_message(call.message.chat.id, f"{live_action_html('warning')} No premium users found.", parse_mode="HTML")
            return
        lines = [f"{live_action_html('verify')} <b>Premium Users</b>"]
        for user_id, premium_until in rows:
            status, expire_text, _text_count, _voice_count = format_subscription_status(user_id)
            lines.append(f"<code>{user_id}</code> - {escape(status)} - {escape(expire_text)}")
        bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="HTML")
        return

    prompts = {
        "add": "Send user ID or @username. You can also add days in the same message. Example: <code>123456789 30</code>. Use <code>0</code> days for lifetime.",
        "remove": "Send user ID or @username to remove premium.",
        "check": "Send user ID or @username to check subscription.",
        "ban": "Send user ID or @username to ban. Premium will also be removed.",
        "unban": "Send user ID or @username to unban.",
        "plan": "Send amount and days. Example: <code>120 1</code> or <code>600 7</code>. Amount is BDT; Binance will show USD automatically.",
    }
    if action not in prompts:
        return
    pending_subscription_actions[call.from_user.id] = action
    msg = bot.send_message(call.message.chat.id, f"{live_action_html('manage')} {prompts[action]}", parse_mode="HTML")
    bot.register_next_step_handler(msg, handle_subscription_admin_step)


def handle_subscription_admin_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    action = pending_subscription_actions.pop(message.from_user.id, None)
    if not action:
        return
    parts = str(message.text or "").strip().split()
    if action == "plan":
        plan_match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*[/\-\s,|]+\s*(\d+)\s*$", str(message.text or ""))
        if not plan_match:
            bot.reply_to(message, f"{live_action_html('warning')} Send amount and days. Example: <code>120/15</code> or <code>120 15</code>", parse_mode="HTML")
            return
        amount = plan_match.group(1)
        days = plan_match.group(2)
        if not amount or not days:
            bot.reply_to(message, f"{live_action_html('warning')} Amount or days is invalid.", parse_mode="HTML")
            return
        set_app_setting("payment_plan_bdt", amount)
        set_app_setting("payment_plan_days", days)
        try:
            usd_amount = float(amount) / BDT_PER_USD
            usd_text = f"${usd_amount:.2f}"
        except Exception:
            usd_text = amount
        bot.reply_to(
            message,
            (
                f"{live_action_html('success')} Plan updated.\n"
                f"Bkash/Nagad/Rocket: <b>{escape(amount)} BDT</b>\n"
                f"Binance: <b>{escape(usd_text)}</b>\n"
                f"Subscription: <b>{escape(days)} days</b>"
            ),
            parse_mode="HTML"
        )
        return
    if not parts:
        bot.reply_to(message, f"{live_action_html('warning')} Send a valid user ID or @username.", parse_mode="HTML")
        return
    target_user_id = resolve_user_reference(parts[0])
    if target_user_id is None:
        bot.reply_to(message, f"{live_action_html('warning')} User not found. Use numeric user ID if username is not saved yet.", parse_mode="HTML")
        return
    if action == "add":
        if len(parts) >= 2 and re.fullmatch(r"\d+", parts[1]):
            activate_premium_from_admin(message, target_user_id, int(parts[1]))
            return
        pending_add_premium_days[message.from_user.id] = target_user_id
        msg = bot.reply_to(
            message,
            (
                f"{live_action_html('manage')} User selected: <code>{target_user_id}</code>\n"
                "Now send subscription days. Example: <code>30</code>. Send <code>0</code> for lifetime."
            ),
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, handle_add_premium_days_step)
        return
    if action == "remove":
        remove_premium_user(target_user_id)
        bot.reply_to(message, f"{live_action_html('success')} Premium removed for <code>{target_user_id}</code>.", parse_mode="HTML")
        return
    if action == "ban":
        set_user_banned(target_user_id, True)
        bot.reply_to(message, f"{live_action_html('lock')} User banned and premium removed: <code>{target_user_id}</code>.", parse_mode="HTML")
        try:
            bot.send_message(target_user_id, f"{live_action_html('lock')} Your account has been banned.", parse_mode="HTML")
        except Exception:
            pass
        return
    if action == "unban":
        set_user_banned(target_user_id, False)
        bot.reply_to(message, f"{live_action_html('success')} User unbanned: <code>{target_user_id}</code>.", parse_mode="HTML")
        return
    if action == "check":
        status, expire_text, text_count, voice_count = format_subscription_status(target_user_id)
        banned_text = "Yes" if is_user_banned(target_user_id) else "No"
        bot.reply_to(
            message,
            (
                f"{live_action_html('stats')} <b>User Subscription</b>\n"
                f"User: <code>{target_user_id}</code>\n"
                f"Banned: <b>{banned_text}</b>\n"
                f"Plan: <b>{escape(status)}</b>\n"
                f"Premium Until: <b>{escape(expire_text)}</b>\n"
                f"Text OTPs: <b>{text_count}</b>\n"
                f"Voice OTPs: <b>{voice_count}</b>"
            ),
            parse_mode="HTML"
        )


def activate_premium_from_admin(message, target_user_id, days):
    premium_until = activate_premium_user(target_user_id, days)
    expire_text = "Lifetime" if premium_until <= 0 else time.strftime("%Y-%m-%d %H:%M", time.localtime(premium_until))
    bot.reply_to(
        message,
        (
            f"{live_action_html('success')} Premium activated.\n"
            f"User: <code>{target_user_id}</code>\n"
            f"Days: <b>{days}</b>\n"
            f"Until: <b>{escape(expire_text)}</b>"
        ),
        parse_mode="HTML"
    )
    try:
        bot.send_message(
            target_user_id,
            (
                f"{live_action_html('success')} Your premium subscription is active.\n"
                f"Plan: <b>Premium</b>\n"
                f"Until: <b>{escape(expire_text)}</b>"
            ),
            parse_mode="HTML",
            reply_markup=build_main_menu_markup(target_user_id)
        )
    except Exception:
        pass


def handle_add_premium_days_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    target_user_id = pending_add_premium_days.get(message.from_user.id)
    if not target_user_id:
        return
    days_text = str(message.text or "").strip()
    if not re.fullmatch(r"\d+", days_text):
        msg = bot.reply_to(
            message,
            f"{live_action_html('warning')} Send days as a number, like <code>30</code>. Send <code>0</code> for lifetime.",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, handle_add_premium_days_step)
        return
    pending_add_premium_days.pop(message.from_user.id, None)
    activate_premium_from_admin(message, target_user_id, int(days_text))


@bot.callback_query_handler(func=lambda call: call.data.startswith("subreq|"))
def subscription_request_decision_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not SUBSCRIPTION_FEATURES_ENABLED:
        bot.answer_callback_query(call.id, "Subscription control is disabled.", show_alert=True)
        return
    _prefix, decision, request_id = call.data.split("|", 2)
    row = get_subscription_request(request_id)
    if not row:
        bot.answer_callback_query(call.id, "Request not found.", show_alert=True)
        return
    _request_id, user_id, method, payment_identifier, status, _requested_at, _days = row
    if status != "pending":
        bot.answer_callback_query(call.id, "Request already handled.", show_alert=True)
        return
    if decision == "reject":
        update_subscription_request_status(request_id, "rejected", call.from_user.id, 0)
        bot.answer_callback_query(call.id, "Rejected.", show_alert=True)
        bot.send_message(
            user_id,
            f"{live_action_html('warning')} Your premium payment request was rejected.\nRequest ID: <code>{escape(request_id)}</code>",
            parse_mode="HTML",
            reply_markup=build_main_menu_markup(user_id)
        )
        try:
            bot.edit_message_text(
                f"{live_action_html('warning')} <b>Rejected</b>\nRequest ID: <code>{escape(request_id)}</code>\nUser ID: <code>{user_id}</code>",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML"
            )
        except Exception:
            pass
        return
    if decision == "accept":
        pending_payment_approvals[call.from_user.id] = request_id
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id,
            (
                f"{live_action_html('manage')} Send subscription days for this request.\n"
                "Example: <code>30</code>. Send <code>0</code> for lifetime."
            ),
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, handle_payment_approval_days)


def handle_payment_approval_days(message):
    if message.from_user.id != ADMIN_ID:
        return
    request_id = pending_payment_approvals.pop(message.from_user.id, None)
    if not request_id:
        return
    days_text = str(message.text or "").strip()
    if not re.fullmatch(r"\d+", days_text):
        bot.reply_to(message, f"{live_action_html('warning')} Send days as a number, like <code>30</code>.", parse_mode="HTML")
        return
    days = int(days_text)
    row = get_subscription_request(request_id)
    if not row:
        bot.reply_to(message, f"{live_action_html('warning')} Request not found.", parse_mode="HTML")
        return
    _request_id, user_id, method, payment_identifier, status, _requested_at, _old_days = row
    if status != "pending":
        bot.reply_to(message, f"{live_action_html('warning')} Request already handled.", parse_mode="HTML")
        return
    premium_until = activate_premium_user(user_id, days)
    update_subscription_request_status(request_id, "approved", message.from_user.id, days)
    expire_text = "Lifetime" if premium_until <= 0 else time.strftime("%Y-%m-%d %H:%M", time.localtime(premium_until))
    bot.reply_to(
        message,
        (
            f"{live_action_html('success')} Request approved.\n"
            f"User: <code>{user_id}</code>\n"
            f"Days: <b>{days}</b>\n"
            f"Until: <b>{escape(expire_text)}</b>"
        ),
        parse_mode="HTML"
    )
    bot.send_message(
        user_id,
        (
            f"{live_action_html('success')} Your premium subscription is active.\n"
            f"Plan: <b>Premium</b>\n"
            f"Until: <b>{escape(expire_text)}</b>"
        ),
        parse_mode="HTML",
        reply_markup=build_main_menu_markup(user_id)
    )


@bot.message_handler(func=lambda m: m.text == 'Broadcast Center' and m.from_user.id == ADMIN_ID)
def broadcast_center(message):
    pending_manual_broadcasts.add(message.from_user.id)
    msg = bot.reply_to(
        message,
        (
            f"{live_action_html('broadcast')} <b>Manual Broadcast</b>\n\n"
            "এখন যে message পাঠাবে সেটাই সব user এবং active group/channel-এ যাবে।\n"
            "Text, photo, video, document—সব পাঠানো যাবে।"
        ),
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, send_manual_broadcast_step)


def send_manual_broadcast_step(message):
    if message.from_user.id != ADMIN_ID or message.from_user.id not in pending_manual_broadcasts:
        return
    pending_manual_broadcasts.discard(message.from_user.id)
    user_ids = get_broadcast_user_ids()
    sent_count = failed_count = 0
    for user_id in user_ids:
        if user_id == ADMIN_ID:
            continue
        try:
            bot.copy_message(user_id, message.chat.id, message.message_id)
            sent_count += 1
            time.sleep(0.05)
        except Exception as e:
            failed_count += 1
            print(f"Manual broadcast failed for {user_id}: {e}")
    group_sent = group_failed = 0
    for group in get_active_required_groups():
        chat_ref = resolve_group_chat_ref(group)
        try:
            bot.copy_message(chat_ref, message.chat.id, message.message_id)
            group_sent += 1
            time.sleep(0.05)
        except Exception as e:
            group_failed += 1
            print(f"Manual group broadcast failed for {group['name']}: {e}")
    bot.reply_to(
        message,
        (
            f"{live_action_html('success')} <b>Manual broadcast sent.</b>\n"
            f"Users delivered: <b>{sent_count}</b>\n"
            f"Users failed: <b>{failed_count}</b>\n"
            f"Groups delivered: <b>{group_sent}</b>\n"
            f"Groups failed: <b>{group_failed}</b>"
        ),
        parse_mode="HTML"
    )


@bot.message_handler(func=lambda m: m.text == 'Link Settings' and m.from_user.id == ADMIN_ID)
def link_settings_menu(message):
    markup = LiveInlineKeyboardMarkup(row_width=1)
    markup.add(
        inline_button(
            "Edit Support Link",
            callback_data="shoplink|support",
            action_key="support",
            style="success"
        ),
        row_width=1
    )
    for target in get_all_targets():
        markup.add(
            inline_button(
                target["name"],
                callback_data=f"targetedit|{target['key']}",
                action_key="manage",
                style="primary"
            ),
            row_width=1
        )
    lines = [f"{live_action_html('manage')} <b>Link Settings</b>", ""]
    active_keys = {target["key"] for target in get_active_required_groups()}
    for target in get_all_targets():
        lines.append(
            f"<b>{escape(target['name'])}</b>\n"
            f"Link: <code>{escape(target['link'] or 'Not set')}</code>\n"
            f"Status: <b>{'Active' if target['key'] in active_keys else 'Inactive'}</b>\n"
        )
    lines.append(f"<b>Support Link</b>\nLink: <code>{escape(get_app_setting('support_link', 'Not set'))}</code>\n")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == 'Shop Links' and m.from_user.id == ADMIN_ID)
def shop_links_menu(message):
    return link_settings_menu(message)
    markup = LiveInlineKeyboardMarkup(row_width=2)
    premium_btn = inline_button("Edit Premium Link", callback_data="shoplink|premium", action_key="buy_premium", style="primary")
    support_btn = inline_button("Edit Support Link", callback_data="shoplink|support", action_key="support", style="success")
    proxy_btn = inline_button("Edit Proxy Link", callback_data="shoplink|proxy", style="primary")
    proxy_btn["icon_custom_emoji_id"] = BUTTON_EMOJI_IDS["buy_proxy"]
    vpn_btn = inline_button("Edit VPN Link", callback_data="shoplink|vpn", style="success")
    vpn_btn["icon_custom_emoji_id"] = BUTTON_EMOJI_IDS["buy_vpn"]
    markup.add(premium_btn, support_btn, row_width=2)
    markup.add(proxy_btn, vpn_btn, row_width=2)
    bot.reply_to(
        message,
        (
            f"{live_action_html('manage')} <b>Shop Links</b>\n\n"
            f"<b>Premium:</b> <code>{escape(get_app_setting('premium_link'))}</code>\n"
            f"<b>Support:</b> <code>{escape(get_app_setting('support_link'))}</code>\n"
            f"<b>Proxy:</b> <code>{escape(get_app_setting('proxy_link'))}</code>\n"
            f"<b>VPN:</b> <code>{escape(get_app_setting('vpn_link'))}</code>"
        ),
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("shoplink|"))
def shop_link_edit_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    kind = call.data.split("|", 1)[1]
    pending_shop_link_updates.add((call.from_user.id, kind))
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        f"{live_action_html('manage')} Send new <b>{escape(kind.title())}</b> bot link:",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, save_shop_link_step, kind)


def save_shop_link_step(message, kind):
    pending_shop_link_updates.discard((message.from_user.id, kind))
    if message.from_user.id != ADMIN_ID:
        return
    link = str(message.text or "").strip()
    if kind == "central_payment_client_id":
        set_app_setting(kind, link)
        bot.reply_to(message, f"{live_action_html('success')} Client ID updated:\n<code>{escape(link)}</code>", parse_mode="HTML")
        return
    if not re.match(r"^https?://t\.me/", link):
        bot.reply_to(message, "শুধু Telegram link দাও, যেমন: <code>https://t.me/example_bot</code>", parse_mode="HTML")
        return
    setting_key = "central_payment_bot_link" if kind == "central_payment_bot" else f"{kind}_link"
    set_app_setting(setting_key, link)
    bot.reply_to(
        message,
        f"{live_action_html('success')} <b>{escape(kind.title())} link updated.</b>\n<code>{escape(link)}</code>",
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("targetedit|"))
def target_edit_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    target_key = call.data.split("|", 1)[1]
    target = get_target(target_key)
    if not target:
        bot.answer_callback_query(call.id, "Target not found.", show_alert=True)
        return
    pending_target_updates[call.from_user.id] = target_key
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        (
            f"{live_action_html('manage')} <b>Update {escape(target['name'])}</b>\n\n"
            "শুধু group/channel username পাঠাও।\n"
            "খালি রাখতে চাইলে <code>off</code> লিখে পাঠাও।\n\n"
            "উদাহরণ:\n"
            "<code>example_group</code>\n"
            "বা <code>@example_group</code>"
        ),
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, save_target_settings_step)


def save_target_settings_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    target_key = pending_target_updates.pop(message.from_user.id, None)
    if not target_key:
        bot.reply_to(message, "No pending target update found.")
        return
    raw = str(message.text or "").strip()
    username = normalize_public_username(raw)
    if raw.lower() not in {"off", "none", "disable", "disabled"} and not username:
        bot.reply_to(message, "শুধু valid public username দাও, যেমন: <code>example_group</code> বা <code>@example_group</code>", parse_mode="HTML")
        return
    if not username:
        chat_id = ""
        link = ""
    else:
        try:
            chat = bot.get_chat(f"@{username}")
        except Exception as e:
            bot.reply_to(message, f"এই username resolve করা যায়নি: <code>@{escape(username)}</code>", parse_mode="HTML")
            print(f"Target resolve failed for @{username}: {e}")
            return
        chat_id = str(chat.id)
        link = f"https://t.me/{username}"
    conn = get_db_connection()
    conn.execute(
        "UPDATE bot_targets SET chat_id=?, link=? WHERE target_key=?",
        (chat_id, link, target_key)
    )
    conn.commit(); conn.close()
    target = get_target(target_key)
    bot.reply_to(
        message,
        (
            f"{live_action_html('success')} <b>Updated</b>\n"
            f"<b>{escape(target['name'])}</b>\n"
            f"ID: <code>{escape(str(target['chat_id'] or 'Not set'))}</code>\n"
            f"Link: <code>{escape(target['link'] or 'Not set')}</code>"
        ),
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: m.text in ('Pending Broadcasts', 'Pending Broadcasts') and m.from_user.id == ADMIN_ID)
def show_pending_group_broadcasts(message):
    if not pending_group_broadcasts:
        bot.reply_to(message, f"{live_action_html('success')} No pending group broadcasts.", parse_mode="HTML")
        return

    for request_id, data in list(pending_group_broadcasts.items()):
        send_pending_group_broadcast(message.chat.id, request_id, data["message_text"])

@bot.callback_query_handler(func=lambda call: call.data.startswith('groupbc|'))
def handle_group_broadcast_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return

    _, action, request_id = call.data.split("|", 2)
    data = pending_group_broadcasts.get(request_id)
    if not data:
        bot.answer_callback_query(call.id, "This broadcast request is no longer available.", show_alert=True)
        return

    if action == "decline":
        pending_group_broadcasts.pop(request_id, None)
        bot.answer_callback_query(call.id, "Declined.")
        try:
            bot.edit_message_text(f"{live_action_html('warning')} Group broadcast declined.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except:
            bot.send_message(call.message.chat.id, f"{live_action_html('warning')} Group broadcast declined.", parse_mode="HTML")
        return

    if action != "approve":
        bot.answer_callback_query(call.id, "Unknown action.", show_alert=True)
        return

    sent_targets, failed_targets = broadcast_to_required_groups(data["message_text"], data.get("channel_message_text"))
    pending_group_broadcasts.pop(request_id, None)
    bot.answer_callback_query(call.id, "Broadcast processed.")

    result_text = (
        f"{live_action_html('success')} <b>Group broadcast approved.</b>\n\n"
        f"Sent: {len(sent_targets)}/{len(get_required_groups())}"
    )
    if sent_targets:
        result_text += "\n\n<b>Delivered:</b>\n" + "\n".join(f"• {escape(name)}" for name in sent_targets)
    if failed_targets:
        result_text += "\n\n<b>Failed:</b>\n" + "\n".join(f"• {escape(name)}" for name in failed_targets)
    try:
        bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
    except:
        bot.send_message(call.message.chat.id, result_text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text in ('Upload File', 'Upload File') and m.from_user.id == ADMIN_ID)
def ask_file(message):
    msg = bot.reply_to(message, f"{live_action_html('upload')} Send your .txt or .xlsx number file.", parse_mode="HTML")
    bot.register_next_step_handler(msg, handle_upload_step)

def handle_upload_step(message):
    if message.content_type == 'document':
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        file_name = str(getattr(message.document, "file_name", "") or "").lower()
        if file_name.endswith(".xlsx"):
            try:
                valid_numbers = extract_numbers_from_excel(downloaded_file)
            except Exception as e:
                print(f"Excel upload read failed: {e}")
                valid_numbers = []
        else:
            try: content = downloaded_file.decode('utf-8').splitlines()
            except: content = downloaded_file.decode('latin-1').splitlines()
            valid_numbers = [clean_phone_number(line) for line in content if len(line.strip()) > 5]
        if valid_numbers:
            pending_uploads[ADMIN_ID] = {"numbers": valid_numbers}
            send_service_picker(message.chat.id, len(valid_numbers))
        else:
            bot.reply_to(message, f"{live_action_html('warning')} No valid numbers were found.", parse_mode="HTML")
    else: bot.reply_to(message, f"{live_action_html('warning')} Please send a .txt or .xlsx file.", parse_mode="HTML")

def send_service_picker(chat_id, total_numbers, page=0, edit_message=None):
    pending_uploads.setdefault(ADMIN_ID, {})["total_numbers"] = total_numbers
    markup, page = build_service_picker_markup(page)
    pages = total_pages(len(SERVICE_OPTIONS), SERVICE_PAGE_SIZE)
    text = (
        f"{live_action_html('success')} <b>{total_numbers}</b> numbers found.\n"
        f"{live_action_html('service')} <b>Select a service:</b> "
        f"Page <b>{page + 1}/{pages}</b> — Total <b>{len(SERVICE_OPTIONS)}</b> services"
    )
    send_or_edit_message(chat_id, text, reply_markup=markup, parse_mode="HTML", edit_message=edit_message)

@bot.callback_query_handler(func=lambda call: call.data.startswith('svcpage|'))
def service_page_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    pending = pending_uploads.get(ADMIN_ID)
    if not pending:
        bot.answer_callback_query(call.id, "Upload a number file first.", show_alert=True)
        return
    page = int(call.data.split("|", 1)[1])
    bot.answer_callback_query(call.id)
    send_service_picker(call.message.chat.id, len(pending.get("numbers", [])), page=page, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data == 'back_to_services')
def back_to_services_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    pending = pending_uploads.get(ADMIN_ID)
    if not pending:
        bot.answer_callback_query(call.id, "Upload a number file first.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_service_picker(call.message.chat.id, len(pending.get("numbers", [])), page=0, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith('svc|'))
def handle_service_selection(call):
    if call.from_user.id != ADMIN_ID: return
    pending = pending_uploads.get(ADMIN_ID)
    if not pending:
        bot.answer_callback_query(call.id, "Upload a number file first.", show_alert=True)
        return

    service_key = call.data.split("|", 1)[1]
    bot.answer_callback_query(call.id)

    if service_key == "custom":
        msg = bot.send_message(call.message.chat.id, f"{live_action_html('custom')} Enter the service name:", parse_mode="HTML")
        bot.register_next_step_handler(msg, handle_custom_service)
        return

    try:
        service_name = SERVICE_OPTIONS[int(service_key)]
    except (ValueError, IndexError):
        bot.send_message(call.message.chat.id, "Service not found. Please start the upload again.")
        return
    pending["service"] = service_name
    send_country_picker(call.message.chat.id, page=0, edit_message=call.message)


def handle_custom_service(message):
    if message.from_user.id != ADMIN_ID: return
    pending = pending_uploads.get(ADMIN_ID)
    if not pending:
        bot.reply_to(message, "Upload a number file first.")
        return
    pending["service"] = clean_label(message.text, "Custom")
    send_country_picker(message.chat.id, page=0)


def send_country_picker(chat_id, page=0, edit_message=None):
    pending = pending_uploads.get(ADMIN_ID, {})
    service_name = pending.get("service", "Unknown")
    markup, page = build_country_picker_markup(page)
    pages = total_pages(len(COUNTRY_OPTIONS), COUNTRY_PAGE_SIZE)
    text = (
        f"{live_action_html('service')} <b>Service:</b> {live_service_html(service_name)} {escape(service_name)}\n"
        f"{live_action_html('country')} <b>Select country:</b> "
        f"Page <b>{page + 1}/{pages}</b> — Total <b>{len(COUNTRY_OPTIONS)}</b> countries"
    )
    send_or_edit_message(chat_id, text, reply_markup=markup, parse_mode="HTML", edit_message=edit_message)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cpage|'))
def country_page_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if not pending_uploads.get(ADMIN_ID):
        bot.answer_callback_query(call.id, "Upload a number file first.", show_alert=True)
        return
    page = int(call.data.split("|", 1)[1])
    bot.answer_callback_query(call.id)
    send_country_picker(call.message.chat.id, page=page, edit_message=call.message)


@bot.callback_query_handler(func=lambda call: call.data.startswith('ctry|'))
def handle_country_selection(call):
    if call.from_user.id != ADMIN_ID: return
    pending = pending_uploads.get(ADMIN_ID)
    if not pending:
        bot.answer_callback_query(call.id, "Upload a number file first.", show_alert=True)
        return

    country_key = call.data.split("|", 1)[1]
    bot.answer_callback_query(call.id)

    if country_key == "custom":
        msg = bot.send_message(call.message.chat.id, f"{live_action_html('country')} Enter the country name:", parse_mode="HTML")
        bot.register_next_step_handler(msg, save_numbers_to_db)
        return

    try:
        country_name = COUNTRY_OPTIONS[int(country_key)]
    except (ValueError, IndexError):
        bot.send_message(call.message.chat.id, "Country not found. Please select again.")
        return

    save_pending_numbers(call.message.chat.id, country_name)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


def ask_country_name(chat_id):
    send_country_picker(chat_id, page=0)


def save_pending_numbers(chat_id, country_name):
    pending = pending_uploads.get(ADMIN_ID)
    if not pending:
        bot.send_message(chat_id, "Upload a number file first.")
        return

    service_name = clean_label(pending.get("service"), "Custom")
    country_name = clean_label(country_name, "Unknown")
    country_display = add_flag_to_name(country_name)
    final_filename = f"{service_name} - {strip_country_flag(country_display)}.txt"
    numbers_list = pending.get("numbers", [])
    if not numbers_list:
        bot.send_message(chat_id, "No numbers found in pending upload.")
        pending_uploads.pop(ADMIN_ID, None)
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    data = [(num, final_filename, service_name, country_name, 'available', None, 0, 0, 0) for num in numbers_list]
    cursor.executemany(
        """INSERT OR IGNORE INTO numbers
           (phone, filename, service, country, status, user_id, otp_received, otp_time, assigned_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        data
    )
    saved_count = cursor.rowcount
    cursor.execute(
        "INSERT OR IGNORE INTO file_settings (filename, free_enabled) VALUES (?, 0)",
        (final_filename,)
    )
    conn.commit(); conn.close()
    stock_message = build_stock_saved_message(service_name, country_display, saved_count)
    bot.send_message(
        chat_id,
        build_file_free_access_message(final_filename, header_text=stock_message),
        parse_mode="HTML",
        reply_markup=build_file_free_access_markup(final_filename)
    )
    if saved_count > 0:
        threading.Thread(
            target=broadcast_stock_update,
            args=(stock_message, final_filename, chat_id),
            daemon=True
        ).start()
        create_group_broadcast_request(chat_id, stock_message, final_filename, service_name, country_display, saved_count)
    pending_uploads.pop(ADMIN_ID, None)


def save_numbers_to_db(message):
    if message.from_user.id != ADMIN_ID: return
    save_pending_numbers(message.chat.id, message.text)

@bot.message_handler(func=lambda m: m.text in ('Stock Stats', 'Stock Stats') and m.from_user.id == ADMIN_ID)
def show_stats(message):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT filename, service, country, COUNT(*) FROM numbers
           WHERE status='available' AND COALESCE(otp_received, 0)=0
           GROUP BY filename, service, country
           ORDER BY service, country"""
    )
    data = cursor.fetchall(); conn.close()
    msg = f"{live_action_html('stats')} <b>Current Stock:</b>\n\n"
    if not data: msg += "Stock Empty."
    for filename, service, country, count in data:
        service_name = display_service(service, filename)
        country_name = display_country(country, filename)
        country_plain = strip_country_flag(country_name)
        msg += f"{live_action_html('stock')} {live_service_html(service_name)} {escape(service_name)} | {live_country_html(country_plain)} {escape(country_plain)} : <b>{count}</b>\n"
    bot.reply_to(message, msg, parse_mode="HTML")


def build_premium_files_content():
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT filename, service, country,
                  COUNT(*) AS total_count,
                  SUM(CASE WHEN status='available' AND COALESCE(otp_received, 0)=0 THEN 1 ELSE 0 END) AS available_count,
                  SUM(CASE WHEN COALESCE(otp_received, 0)=1 THEN 1 ELSE 0 END) AS otp_count
           FROM numbers
           GROUP BY filename, service, country
           ORDER BY service, country, filename"""
    ).fetchall()
    conn.close()
    markup = LiveInlineKeyboardMarkup(row_width=1)
    lines = [f"{live_action_html('manage')} <b>Premium File Stock</b>", ""]
    for index, (filename, service, country, total_count, available_count, otp_count) in enumerate(rows, start=1):
        service_name = display_service(service, filename)
        country_name = strip_country_flag(display_country(country, filename))
        available_count = int(available_count or 0)
        total_count = int(total_count or 0)
        otp_count = int(otp_count or 0)
        lines.append(
            f"<b>{index}.</b> {live_service_html(service_name)} {escape(service_name)} | "
            f"{live_country_html(country_name)} {escape(country_name)} - "
            f"Fresh: <b>{available_count}</b>/<b>{total_count}</b> | OTP: <b>{otp_count}</b>"
        )
        markup.add(
            inline_button(
                f"Delete {index}. {service_name} | {country_name} ({available_count}/{total_count})",
                callback_data=f"del|{get_file_token(filename)}",
                action_key="delete"
            )
        )
    markup.row(
        inline_button("Back", callback_data="numsettingsback", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close")
    )
    return "\n".join(lines), markup, len(rows)


@bot.message_handler(func=lambda m: m.text in ('Manage Files', 'Premium Files') and m.from_user.id == ADMIN_ID)
def manage_files(message):
    text, markup, total = build_premium_files_content()
    if total <= 0:
        bot.reply_to(message, f"{live_action_html('warning')} No files found.", parse_mode="HTML")
        return
    bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup)
    return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT filename, service, country,
                  COUNT(*) AS total_count,
                  SUM(CASE WHEN status='available' THEN 1 ELSE 0 END) AS available_count
           FROM numbers
           GROUP BY filename, service, country
           ORDER BY service, country, filename"""
    )
    files = cursor.fetchall(); conn.close()
    if not files:
        bot.reply_to(message, f"{live_action_html('warning')} No files found.", parse_mode="HTML")
        return

    markup = LiveInlineKeyboardMarkup(row_width=1)
    lines = [f"{live_action_html('manage')} <b>Manage Files</b>", ""]
    for index, (filename, service, country, total_count, available_count) in enumerate(files, start=1):
        service_name = display_service(service, filename)
        country_name = strip_country_flag(display_country(country, filename))
        available_count = int(available_count or 0)
        total_count = int(total_count or 0)
        lines.append(
            f"<b>{index}.</b> {live_service_html(service_name)} {escape(service_name)} | "
            f"{live_country_html(country_name)} {escape(country_name)} "
            f"— <b>{available_count}</b>/<b>{total_count}</b> available"
        )
        button_text = f"Delete {index}. {service_name} | {country_name} ({available_count}/{total_count})"
        markup.add(
            inline_button(
                button_text,
                callback_data=f"del|{get_file_token(filename)}",
                action_key="delete"
            )
        )

    bot.reply_to(message, "\n".join(lines), parse_mode="HTML", reply_markup=markup)

def build_free_files_content():
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT n.filename, n.service, n.country,
                  COUNT(*) AS total_count,
                  SUM(CASE WHEN COALESCE(n.otp_received, 0)=0 THEN 1 ELSE 0 END) AS free_ready_count,
                  COALESCE(fs.free_enabled, 0) AS free_enabled
           FROM numbers n
           LEFT JOIN file_settings fs ON fs.filename=n.filename
           GROUP BY n.filename, n.service, n.country, fs.free_enabled
           ORDER BY n.service, n.country, n.filename"""
    ).fetchall()
    conn.close()
    markup = LiveInlineKeyboardMarkup(row_width=1)
    lines = [f"{live_action_html('manage')} <b>Free File Access</b>", ""]
    for index, (filename, service, country, total_count, free_ready_count, free_enabled) in enumerate(rows, start=1):
        service_name = display_service(service, filename)
        country_name = strip_country_flag(display_country(country, filename))
        free_ready_count = int(free_ready_count or 0)
        total_count = int(total_count or 0)
        free_enabled = bool(int(free_enabled or 0))
        status = "YES" if free_enabled else "NO"
        lines.append(
            f"<b>{index}.</b> {live_service_html(service_name)} {escape(service_name)} | "
            f"{live_country_html(country_name)} {escape(country_name)} - "
            f"Free: <b>{status}</b> | Ready: <b>{free_ready_count}</b>/<b>{total_count}</b>"
        )
        markup.add(
            inline_button(
                f"{'Turn OFF' if free_enabled else 'Turn ON'} Free {index}. {service_name} | {country_name}",
                callback_data=f"freefile|{get_file_token(filename)}",
                action_key="refresh"
            )
        )
    markup.row(
        inline_button("Back", callback_data="numsettingsback", action_key="back"),
        inline_button("Close", callback_data="close", action_key="close")
    )
    return "\n".join(lines), markup, len(rows)


@bot.message_handler(func=lambda m: m.text == 'Free Files' and m.from_user.id == ADMIN_ID)
def free_files_menu(message):
    return send_number_settings_panel(message.chat.id)
    text, markup, total = build_free_files_content()
    if total <= 0:
        bot.reply_to(message, f"{live_action_html('warning')} No files found.", parse_mode="HTML")
        return
    bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('freefile|'))
def toggle_file_free_access(call):
    if call.from_user.id != ADMIN_ID: return
    filename = resolve_file_token(call.data.split("|", 1)[1])
    if not filename:
        bot.answer_callback_query(call.id, "Stock not found.", show_alert=True)
        return
    enabled = not is_file_free_enabled(filename)
    set_file_free_enabled(filename, enabled)
    bot.answer_callback_query(call.id, f"Free access {'ON' if enabled else 'OFF'}.", show_alert=True)
    text, markup, _total = build_free_files_content()
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('quickfreefile|'))
def toggle_quick_file_free_access(call):
    if call.from_user.id != ADMIN_ID: return
    filename = resolve_file_token(call.data.split("|", 1)[1])
    if not filename:
        bot.answer_callback_query(call.id, "Stock not found.", show_alert=True)
        return
    enabled = not is_file_free_enabled(filename)
    set_file_free_enabled(filename, enabled)
    bot.answer_callback_query(call.id, f"Free access {'ON' if enabled else 'OFF'}.", show_alert=True)
    text = build_file_free_access_message(filename)
    markup = build_file_free_access_markup(filename)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('del|'))
def delete_file(call):
    if call.from_user.id != ADMIN_ID: return
    filename = resolve_file_token(call.data.split("|", 1)[1])
    if not filename:
        bot.answer_callback_query(call.id, "Stock not found.", show_alert=True)
        return
    conn = get_db_connection()
    conn.execute("DELETE FROM number_assignments WHERE number_id IN (SELECT id FROM numbers WHERE filename=?)", (filename,))
    conn.execute("DELETE FROM numbers WHERE filename=?", (filename,))
    conn.execute("DELETE FROM file_settings WHERE filename=?", (filename,))
    conn.commit(); conn.close()
    bot.answer_callback_query(call.id, "Deleted!", show_alert=True)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass

@bot.message_handler(func=lambda m: m.text in ('User Mode', 'User Mode') and m.from_user.id == ADMIN_ID)
def back_home(message):
    send_main_menu(message.chat.id)


# ================= RUN BOT =================
if __name__ == "__main__":
    print("Bot Started (Live-only + Advanced OTP Matcher version)...")
    if not acquire_single_instance_lock():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        sys.exit(1)
    if not verify_bot_token_or_exit():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        sys.exit(1)
    threading.Thread(target=heartbeat_worker, daemon=True).start()
    if SUBSCRIPTION_FEATURES_ENABLED:
        threading.Thread(target=start_auto_payment_bridge, daemon=True).start()
    threading.Thread(target=api_sync_worker, daemon=True).start()
    threading.Thread(target=live_traffic_auto_refresh_worker, daemon=True).start()
    
    conflict_sleep = 15
    while True:
        try:
            bot.remove_webhook()
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            if is_get_updates_conflict_error(e):
                print("")
                print("Telegram 409 conflict: another bot instance is polling this token.")
                print("Close the other bot copy, then this one will retry automatically.")
                time.sleep(conflict_sleep)
                conflict_sleep = min(conflict_sleep + 15, 120)
                continue
            conflict_sleep = 15
            print(f"Polling Error: {e}")
            time.sleep(5)
