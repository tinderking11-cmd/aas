import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BDT_METHODS = {"bkash", "nagad", "rocket"}
ALL_METHODS = {"bkash", "nagad", "rocket", "binance"}

OFFICIAL_SMS_SENDERS = {
    "bkash": {"BKASH"},
    "nagad": {"NAGAD"},
    "rocket": {"ROCKET", "DBBL", "16216"},
}


@dataclass
class SmsPayment:
    ok: bool
    method: str = ""
    amount: float = 0.0
    txids: tuple = ()
    reason: str = ""


def normalize_payment_id(value):
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def mask_secret(value, keep=4):
    value = str(value or "")
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def make_owner_sms_secret(bot_token, user_id):
    return hashlib.sha1(f"{bot_token}|{int(user_id)}|owner-sms".encode()).hexdigest()[:24]


def make_client_integration_key(bot_token, client_id, owner_id=None, created_at=None):
    seed = f"{bot_token}|{client_id}|{owner_id or ''}|{created_at or time.time()}|client-gateway"
    return "pg_" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def make_order_id(client_id, user_id, method, payment_id):
    seed = f"{client_id}|{user_id}|{method}|{normalize_payment_id(payment_id)}|{time.time()}"
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def detect_sms_method(sender, text):
    sender_key = normalize_payment_id(sender)
    text_key = normalize_payment_id(text)
    for method, allowed_senders in OFFICIAL_SMS_SENDERS.items():
        if any(token in sender_key for token in allowed_senders):
            return method
    if "BKASH" in text_key:
        return "bkash"
    if "NAGAD" in text_key:
        return "nagad"
    if "ROCKET" in text_key or "DBBL" in text_key:
        return "rocket"
    return ""


def is_official_sms_sender(method, sender):
    sender_key = normalize_payment_id(sender)
    if not sender_key:
        return False
    return any(token in sender_key for token in OFFICIAL_SMS_SENDERS.get(method, set()))


def extract_amount(text):
    text = str(text or "")
    patterns = [
        r"(?:tk|bdt|taka|amount|received|cash\s*in|payment)\.?\s*[:=]?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)",
        r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(?:tk|bdt|taka)\.?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1).replace(",", ""))
    return 0.0


def extract_txids(text):
    text = str(text or "")
    found = set()
    patterns = [
        r"(?:trxid|txnid|transaction\s*id|trans\s*id|ref\s*id|reference)\s*[:#\-]?\s*([A-Za-z0-9]{5,30})",
        r"\b(?:trx|txn)\s*[:#\-]\s*([A-Za-z0-9]{5,30})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            found.add(normalize_payment_id(match.group(1)))
    for token in re.findall(r"\b[A-Za-z0-9]{7,24}\b", text):
        normalized = normalize_payment_id(token)
        if any(c.isdigit() for c in normalized) and any(c.isalpha() for c in normalized):
            found.add(normalized)
    return tuple(sorted(found))


def parse_official_payment_sms(sender, text):
    method = detect_sms_method(sender, text)
    if method not in BDT_METHODS:
        return SmsPayment(False, reason="unknown_payment_sms")
    if not is_official_sms_sender(method, sender):
        return SmsPayment(False, method=method, reason="fake_or_unofficial_sender")
    amount = extract_amount(text)
    if amount <= 0:
        return SmsPayment(False, method=method, reason="amount_not_found")
    txids = extract_txids(text)
    if not txids:
        return SmsPayment(False, method=method, amount=amount, reason="txid_not_found")
    return SmsPayment(True, method=method, amount=amount, txids=txids)


def build_client_integration_payload(client_id, name, api_url, integration_key, methods, plan, features):
    return {
        "client_id": client_id,
        "name": name,
        "gateway_api_url": api_url.rstrip("/"),
        "integration_key": integration_key,
        "methods": methods,
        "plan": {
            "bdt_amount": plan[0],
            "bdt_days": plan[1],
            "usdt_amount": plan[2],
            "usdt_days": plan[3],
        },
        "features": {
            "premium": bool(features[0]),
            "balance": bool(features[1]),
            "product": bool(features[2]),
            "product_name": features[3],
        },
    }


class BinancePaymentVerifier:
    def __init__(self, api_key, api_secret, currency="USDT", window_minutes=15):
        self.api_key = str(api_key or "").strip()
        self.api_secret = str(api_secret or "").strip()
        self.currency = str(currency or "USDT").strip().upper()
        self.window_minutes = int(window_minutes or 15)

    def signed_request(self, path, params=None):
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Binance API key/secret is not set")
        payload = dict(params or {})
        payload["timestamp"] = int(time.time() * 1000)
        query = urlencode(payload)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"https://api.binance.com{path}?{query}&signature={signature}"
        request = Request(url, headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "PaymentGateway/1.0"})
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8", "ignore") or "[]")

    def payment_matches(self, order_id):
        needle = normalize_payment_id(order_id)
        start_ms = int((time.time() - self.window_minutes * 60) * 1000)
        paths = [
            "/sapi/v1/pay/transactions",
            "/sapi/v1/pay/transactions?type=0",
        ]
        last_error = ""
        for path in paths:
            try:
                data = self.signed_request(path, {"startTimestamp": start_ms})
            except Exception as exc:
                last_error = str(exc)
                continue
            items = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            for item in items:
                blob = normalize_payment_id(" ".join(str(value) for value in item.values() if not isinstance(value, (dict, list))))
                if needle not in blob:
                    continue
                currency_blob = str(item.get("currency") or item.get("fiatCurrency") or item.get("asset") or "").upper()
                if currency_blob and self.currency not in currency_blob:
                    continue
                amount = 0.0
                for key in ("amount", "quantity", "qty", "total", "totalPrice", "payAmount", "paymentAmount"):
                    try:
                        amount = float(item.get(key) or 0)
                    except Exception:
                        amount = 0.0
                    if amount > 0:
                        break
                return True, item, amount
        return False, last_error or "Order ID not found in Binance history", 0.0


class PaymentGatewayClient:
    def __init__(self, gateway_url, client_key):
        self.gateway_url = str(gateway_url or "").rstrip("/")
        self.client_key = str(client_key or "").strip()

    def _post(self, path, payload):
        body = urlencode(dict(payload or {}, client_key=self.client_key)).encode("utf-8")
        request = Request(
            self.gateway_url + path,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8", "ignore") or "{}")

    def get_config(self):
        return self._post("/api/client/config", {})

    def create_order(self, user_id, purpose, method, payment_id, amount):
        return self._post("/api/order/create", {
            "user_id": user_id,
            "purpose": purpose,
            "method": method,
            "payment_id": payment_id,
            "amount": amount,
        })

    def order_status(self, order_id):
        return self._post("/api/order/status", {"order_id": order_id})
