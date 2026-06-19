import os
import sys
import time
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException
import re
import phonenumbers
import json
import threading
import subprocess
sys.path.append(os.path.join(os.path.dirname(__file__), "namber"))
from panel_live_assets import build_sms_card, button_row, looks_like_phone_number, looks_like_otp_code
from sender_bot_config import OTP_TARGET_CHAT_ID, SENDER_BOT_TOKEN
from otp_router import route_text_otp
from runtime_tools import ask_and_maybe_start_number_bot

# ================= CONFIGURATION =================
BOT_TOKEN = SENDER_BOT_TOKEN
CHAT_ID = OTP_TARGET_CHAT_ID
MAX_TELEGRAM_MESSAGES = 20
REFRESH_INTERVAL_SECONDS = 15
PAGE_READY_TIMEOUT_SECONDS = 25

last_messages = set()
sent_message_ids = []
recent_codes_cache = {} 
lock = threading.Lock()
panel_threads = []

# ================= HELPER FUNCTIONS =================

def get_flag_emoji(country_code):
    if not country_code: return "🌍"
    return "".join([chr(ord(c.upper()) + 127397) for c in country_code])

def delete_telegram_message(message_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    payload = {"chat_id": CHAT_ID, "message_id": message_id}
    try: requests.post(url, data=payload, timeout=5)
    except: pass

def send_to_telegram(text: str):
    return None

def format_phone_number(number):
    clean_num = str(number).replace(" ", "").strip()
    if len(clean_num) > 8: return clean_num[:5] + "xxx" + clean_num[-3:]
    return clean_num

def get_service_short_code(service_name):
    s = service_name.upper().replace(" ", "")
    mapping = {
        "FACEBOOK": "FB", "WHATSAPP": "WA", "TELEGRAM": "TG",
        "GOOGLE": "GO", "INSTAGRAM": "IG", "TIKTOK": "TT",
        "TWITTER": "TW", "APPLE": "AP", "AMAZON": "AM",
        "MICROSOFT": "MS", "NETFLIX": "NF", "SPOTIFY": "SP",
        "IMO": "IM", "VIBER": "VB", "SNAPCHAT": "SC",
        "PAYPAL": "PP", "BINANCE": "BN", "COINBASE": "CB",
        "ALIBABA": "AB", "ALIEXPRESS": "AE", "UBER": "UB",
        "PATHAO": "PA", "BKASH": "BK", "NAGAD": "NG",
        "ROCKET": "RK", "FOODPANDA": "FP", "KUCOIN": "KC",
        "BYBIT": "BB", "OKX": "OK", "TRUSTWALLET": "TW",
        "AIRBNB": "AB", "BOOKING": "BK", "AGODA": "AG",
        "EBAY": "EB", "LINE": "LN", "WECHAT": "WC",
        "KAKAO": "KK", "ZALO": "ZL", "GRAB": "GB",
        "GOJEK": "GJ", "TRUECALLER": "TC", "TINDER": "TN",
        "BUMBLE": "BM", "BADOO": "BD"
    }
    if s in mapping: return mapping[s]
    return s[:2] if len(s) >= 2 else s

# --- FIXED SERVICE DETECTION LOGIC ---
def detect_service_from_message(message):
    message_lower = message.lower()
    
    # Expanded List (Updated)
    service_patterns = {
        'FACEBOOK': ['facebook', 'fb', 'mfacebook', 'meta'],
        'WHATSAPP': ['whatsapp', 'wa', 'business'],
        'TELEGRAM': ['telegram', 'tg', 'login code'],
        'GOOGLE': ['google', 'gmail', 'youtube', 'g-'],
        'TWITTER': ['twitter', 'x.com'],
        'INSTAGRAM': ['instagram', 'ig'],
        'TIKTOK': ['tiktok', 'douyin'],
        'AMAZON': ['amazon', 'aws'],
        'PAYPAL': ['paypal'],
        'APPLE': ['apple', 'icloud', 'itunes', 'appleid'],
        'MICROSOFT': ['microsoft', 'outlook', 'office', 'msft'],
        'YAHOO': ['yahoo'],
        'LINKEDIN': ['linkedin'],
        'SNAPCHAT': ['snapchat'],
        'DISCORD': ['discord'],
        'VIBER': ['viber'],
        'IMO': ['imo'],
        'NETFLIX': ['netflix'],
        'SPOTIFY': ['spotify'],
        'UBER': ['uber'],
        'PATHAO': ['pathao'],
        'DARAZ': ['daraz'],
        'BKASH': ['bkash'],
        'NAGAD': ['nagad'],
        'ROCKET': ['rocket'],
        'FOODPANDA': ['foodpanda'],
        'BINANCE': ['binance'],
        'COINBASE': ['coinbase'],
        'KUCOIN': ['kucoin'],
        'BYBIT': ['bybit'],
        'OKX': ['okx'],
        'TRUST WALLET': ['trust wallet', 'trustwallet'],
        'AIRBNB': ['airbnb'],
        'BOOKING': ['booking.com', 'booking'],
        'AGODA': ['agoda'],
        'ALIBABA': ['alibaba'],
        'ALIEXPRESS': ['aliexpress'],
        'EBAY': ['ebay'],
        'LINE': ['line'],
        'WECHAT': ['wechat'],
        'KAKAO': ['kakao'],
        'ZALO': ['zalo'],
        'GRAB': ['grab'],
        'GOJEK': ['gojek'],
        'TRUECALLER': ['truecaller'],
        'TINDER': ['tinder'],
        'BUMBLE': ['bumble'],
        'BADOO': ['badoo'],
        'BIGOLIVE': ['bigo', 'bigolive'],
        'LIKE': ['likee', 'like'],
        'REDDIT': ['reddit'],
        'STEAM': ['steam']
    }

    for service, keywords in service_patterns.items():
        for keyword in keywords:
            if keyword in message_lower: return service.upper()
    
    # Removed the logic that picks the first random word.
    # Now defaults to OTP if no known service is found.
    return "OTP"

def extract_pure_code(text):
    match_hyphen = re.search(r'\b\d{3,}[- ]\d{3,}\b', text)
    if match_hyphen: return match_hyphen.group(0)
    match_digit = re.search(r'\b\d{4,8}\b', text)
    if match_digit: return match_digit.group(0)
    return None

# ================= CORE LOGIC =================

def extract_sms(driver, sms_url, ip_label):
    global last_messages, sent_message_ids, recent_codes_cache
    try:
        if driver.current_url != sms_url:
            driver.get(sms_url)
        else:
            driver.refresh()
        wait_until_page_ready(driver, ip_label)
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        headers = soup.find_all('th')
        
        number_idx = service_idx = sms_idx = None
        for idx, th in enumerate(headers):
            label = th.get('aria-label', '').lower()
            if 'number' in label: number_idx = idx
            elif 'cli' in label or 'service' in label: service_idx = idx
            elif 'sms' in label: sms_idx = idx

        if None in (number_idx, service_idx, sms_idx): return
        rows = soup.find_all('tr')[1:]
        current_time = time.time()
        
        for row in rows:
            cols = row.find_all('td')
            if len(cols) <= max(number_idx, service_idx, sms_idx): continue

            number = cols[number_idx].get_text(strip=True) or "Unknown"
            service_from_column = cols[service_idx].get_text(strip=True) or "0"
            message = cols[sms_idx].get_text(strip=True)

            with lock:
                if not message or message in last_messages: continue
            
            final_code = extract_pure_code(message)
            if not looks_like_phone_number(number):
                continue
            
            # Anti-Duplicate Check
            dedup_key = f"{number}_{final_code or message}"
            if dedup_key in recent_codes_cache:
                if current_time - recent_codes_cache[dedup_key] < 30: continue

            with lock:
                last_messages.add(message)
                if len(last_messages) > 300: last_messages = set(list(last_messages)[-100:])
                recent_codes_cache[dedup_key] = current_time

            try:
                p_num = "+" + number if not number.startswith('+') else number
                parsed = phonenumbers.parse(p_num, None)
                region_code = phonenumbers.region_code_for_number(parsed)
                flag = get_flag_emoji(region_code)
                short_country = region_code if region_code else "UN"
            except: flag, short_country = "🌍", "UN"

            if service_from_column != "0": detected_service = service_from_column.upper()
            else: detected_service = detect_service_from_message(message)
            
            short_service = get_service_short_code(detected_service)
            hidden_number = format_phone_number(number)
            
            msg_text = build_sms_card(hidden_number, detected_service, short_country, short_country, final_code, message)
            
            result = route_text_otp(number, detected_service, short_country, short_country, final_code, msg_text, message)
            sent_msg_id = (result or {}).get("message_id")
            if sent_msg_id:
                print(f"[{ip_label}] ✅ Sent: {final_code}")
                with lock:
                    sent_message_ids.append(sent_msg_id)
                    if len(sent_message_ids) > MAX_TELEGRAM_MESSAGES:
                        oldest_msg_id = sent_message_ids.pop(0)
                        delete_telegram_message(oldest_msg_id)
            
    except Exception: pass

def wait_for_login(driver, ip_label, timeout=180):
    print(f"[*] [{ip_label}] Waiting for login...")
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        try:
            if "login" not in driver.current_url.lower():
                print(f"[✅] [{ip_label}] Login successful!")
                return True
        except: pass
    return False

def launch_browser():
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    service = Service(log_path=os.devnull)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(25)
    return driver


def safe_get(driver, url, label):
    try:
        driver.get(url)
        return True
    except TimeoutException:
        print(f"[⚠️] [{label}] Page load slow, continuing with current page...")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        return False
    except WebDriverException as e:
        short_error = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        print(f"[⚠️] [{label}] Page load issue, continuing: {short_error}")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        return False


def wait_until_page_ready(driver, label, timeout=PAGE_READY_TIMEOUT_SECONDS):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if driver.execute_script("return document.readyState") == "complete":
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print(f"[⚠️] [{label}] Page not fully ready within {timeout}s, continuing safely...")
    return False

def start_bot_for_panel(login_url, sms_url, label):
    driver = launch_browser()
    try:
        safe_get(driver, login_url, label)
        wait_until_page_ready(driver, label)
        if not wait_for_login(driver, label):
            return
        if driver.current_url.rstrip("/") != sms_url.rstrip("/"):
            safe_get(driver, sms_url, label)
            wait_until_page_ready(driver, label)
        time.sleep(2)
        print(f"[✅] [{label}] Monitoring started...")
        while True:
            cycle_started = time.time()
            extract_sms(driver, sms_url, label)
            elapsed = time.time() - cycle_started
            time.sleep(max(0, REFRESH_INTERVAL_SECONDS - elapsed))
    except Exception as e:
        print(f"[⚠️] [{label}] Panel stopped: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

def main():
    ask_and_maybe_start_number_bot()
    print("[*] Live multi-panel manager started.")
    print("[*] নতুন panel যোগ করতে Y লিখুন।")
    print("[*] N লিখলে নতুন panel নেওয়া বন্ধ হবে, কিন্তু আগের panelগুলো চলতেই থাকবে।")

    panel_count = 0
    while True:
        add_more = input("👉 নতুন panel চালু করতে চান? (Y/N): ").strip().lower()
        if add_more in ("n", "no"):
            print("[*] নতুন panel নেওয়া বন্ধ। চালু panelগুলো চলতেই থাকবে।")
            break
        if add_more not in ("y", "yes"):
            print("⚠️ Y বা N লিখুন।")
            continue

        login_url = input("👉 Login panel link দিন: ").strip()
        sms_url = input("👉 SMS page link দিন: ").strip()
        panel_count += 1
        label = f"Panel-{panel_count}"
        thread = threading.Thread(
            target=start_bot_for_panel,
            args=(login_url, sms_url, label),
            daemon=False,
        )
        thread.start()
        panel_threads.append(thread)
        print(f"[✅] [{label}] চালু করা হয়েছে।")

    for thread in panel_threads:
        thread.join()

if __name__ == "__main__":
    main()
