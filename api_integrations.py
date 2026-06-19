import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests


DEFAULT_AGENT_BASE_URL = "http://203.161.58.20:3001/api/functions/agent-api"
DEFAULT_FASTX_BASE_URL = "https://fastxotps.com"
DEFAULT_FASTX_API_KEY = "MURAD_F69836D22F85120643454FB2"


@dataclass
class ApiNumber:
    phone: str
    service: str = "API OTP"
    country: str = "Unknown"
    provider: str = "api"
    range_prefix: str = ""


@dataclass
class ApiOtp:
    number: str
    service: str = "API OTP"
    country: str = "Unknown"
    code: str = ""
    message: str = ""
    provider: str = "api"
    event_id: str = ""
    created_at: str = ""


class ApiRequestError(Exception):
    def __init__(self, provider, status_code=None, message="API request failed"):
        self.provider = provider
        self.status_code = status_code
        super().__init__(message)


def clean_phone(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw
    if not re.fullmatch(r"\+?[\d\s().-]+", raw):
        match = re.search(r"(?:\+?\d[\d\s().-]{6,}\d)", raw)
        candidate = match.group(0) if match else raw
    digits = re.sub(r"\D+", "", candidate)
    if len(digits) < 5:
        return ""
    if len(digits) > 16:
        return ""
    return "+" + digits if candidate.strip().startswith("+") else digits


def _first_text(item: dict, keys, default=""):
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _walk_records(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "numbers", "otps", "messages", "result", "results", "rows", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _walk_records(value)
                if nested:
                    return nested
        return [payload]
    return []


def _walk_values(payload):
    if isinstance(payload, dict):
        for value in payload.values():
            yield value
            yield from _walk_values(value)
    elif isinstance(payload, list):
        for value in payload:
            yield value
            yield from _walk_values(value)


def _request_json(method, url, api_key="", params=None, json_body=None, timeout=25, provider="api", quiet_statuses=None):
    quiet_statuses = set(quiet_statuses or ())
    headers = {"User-Agent": "BDX-Top-OTP-Bot/1.0", "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
        headers["X-API-Key"] = api_key
    response = requests.request(
        method,
        url,
        params=params,
        json=json_body,
        headers=headers,
        timeout=timeout,
    )
    if response.status_code in quiet_statuses:
        return None
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        code = response.status_code
        if code in (401, 403):
            message = f"{provider} access denied. Check API key/base URL."
        elif code == 404:
            message = f"{provider} endpoint not found. Check base URL."
        else:
            message = f"{provider} API error {code}."
        raise ApiRequestError(provider, code, message) from exc
    if not response.text.strip():
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ApiRequestError(provider, response.status_code, f"{provider} returned non-JSON response.") from exc


def _join_url(base_url, path):
    return str(base_url or "").rstrip("/") + "/" + str(path or "").lstrip("/")


def normalize_number_record(record, provider, service_default="API OTP", country_default="Unknown"):
    if not isinstance(record, dict):
        phone = clean_phone(record)
        return ApiNumber(phone=phone, service=service_default, country=country_default, provider=provider) if phone else None
    phone = clean_phone(_first_text(record, ("phone", "number", "full_number", "national_number", "msisdn", "cli", "mobile", "sim", "user_number", "whatsapp", "whatsapp_number", "wa_number", "telegram", "telegram_number", "tg_number", "account_number")))
    if not phone:
        return None
    service = _first_text(record, ("service", "platform", "app", "site", "sid", "name", "cli_name", "product"), service_default)
    country = _first_text(record, ("country", "country_name", "countryCode", "country_code", "region"), country_default)
    return ApiNumber(
        phone=phone,
        service=service or service_default,
        country=country or country_default,
        provider=provider,
        range_prefix=_first_text(record, ("range", "prefix", "cli_range")),
    )


def normalize_otp_record(record, provider, service_default="API OTP", country_default="Unknown"):
    if not isinstance(record, dict):
        message = str(record or "").strip()
        number = clean_phone(message)
        code = extract_otp_code(message)
        return ApiOtp(number=number, code=code, message=message, provider=provider) if number and (code or message) else None
    message = _first_text(record, ("message", "sms", "text", "body", "otp", "code", "content", "raw"))
    code = _first_text(record, ("code", "otp_code", "otp", "pin"), "") or extract_otp_code(message)
    number = clean_phone(_first_text(record, ("number", "phone", "msisdn", "cli", "mobile", "user_number", "to", "whatsapp", "whatsapp_number", "wa_number", "telegram", "telegram_number", "tg_number", "account_number")))
    if not number and message:
        number = clean_phone(message)
    if not number or not (code or message):
        return None
    service = _first_text(record, ("service", "platform", "app", "site", "cli_name"), service_default)
    country = _first_text(record, ("country", "country_name", "countryCode", "country_code", "region"), country_default)
    event_id = _first_text(record, ("id", "_id", "message_id", "uuid", "created_at", "time", "date"))
    return ApiOtp(
        number=number,
        service=service or service_default,
        country=country or country_default,
        code=code,
        message=message or code,
        provider=provider,
        event_id=event_id or f"{provider}:{number}:{code}:{hash(message)}",
        created_at=_first_text(record, ("created_at", "time", "date", "timestamp")),
    )


def extract_otp_code(text):
    source = str(text or "")
    for pattern in (
        r"(?:otp\s*code|otp|code|pin)\s*[:\-]?\s*([A-Za-z0-9]+(?:[-\s][A-Za-z0-9]+)*)",
        r"\b\d{3,}[-\s]\d{3,}\b",
        r"\b\d{4,8}\b",
    ):
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            return (match.group(1) if match.lastindex else match.group(0)).strip()
    return ""


def extract_live_ranges(payload):
    ranges = []
    if isinstance(payload, dict) and isinstance(payload.get("services"), list):
        records = payload["services"]
    else:
        records = _walk_records(payload)
    for record in records:
        if not isinstance(record, dict):
            text = str(record or "").strip()
            if text:
                ranges.append({"range": text, "service": "WhatsApp", "country": "Unknown", "count": "", "traffic": ""})
            continue
        service = _first_text(record, ("service", "sid", "name", "platform", "app", "project"), "WhatsApp")
        country = _first_text(record, ("country", "country_name", "region", "location"), "Unknown")
        traffic = _first_text(record, ("traffic", "load", "priority", "status", "live"), "")
        count = _first_text(record, ("count", "available", "stock", "numbers", "total", "quantity"), "")
        raw_ranges = record.get("ranges") or record.get("range") or record.get("prefixes") or record.get("cli_ranges") or record.get("cli")
        if isinstance(raw_ranges, list):
            for item in raw_ranges:
                if isinstance(item, dict):
                    range_text = _first_text(item, ("range", "prefix", "cli", "name", "value", "id"))
                    item_service = _first_text(item, ("service", "sid", "name", "platform", "app", "project"), service)
                    item_country = _first_text(item, ("country", "country_name", "region", "location"), country)
                    item_count = _first_text(item, ("count", "available", "stock", "numbers", "total", "quantity"), count)
                    item_traffic = _first_text(item, ("traffic", "load", "priority", "status", "live"), traffic)
                else:
                    range_text = str(item or "").strip()
                    item_service = service
                    item_country = country
                    item_count = count
                    item_traffic = traffic
                if range_text:
                    ranges.append({
                        "range": range_text,
                        "service": item_service or "WhatsApp",
                        "country": item_country or "Unknown",
                        "count": item_count,
                        "traffic": item_traffic,
                    })
        elif raw_ranges:
            ranges.append({
                "range": str(raw_ranges).strip(),
                "service": service,
                "country": country,
                "count": count,
                "traffic": traffic,
            })
    seen = set()
    unique = []
    for item in ranges:
        key = (item.get("range", ""), item.get("service", ""), item.get("country", ""))
        if item.get("range") and key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


class OtpApiClient:
    def __init__(self, provider, base_url, api_key="", service_default="API OTP", country_default="Unknown"):
        self.provider = provider
        self.base_url = str(base_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.service_default = service_default or "API OTP"
        self.country_default = country_default or "Unknown"

    def enabled(self):
        return bool(self.base_url and self.api_key)

    def agent_numbers(self, limit=100, cli="", status="assigned"):
        payload = _request_json(
            "GET",
            _join_url(self.base_url, "numbers"),
            self.api_key,
            params={"page": 1, "limit": limit, "cli": cli, "status": status},
            provider=self.provider,
        )
        return [n for n in (normalize_number_record(r, self.provider, self.service_default, self.country_default) for r in _walk_records(payload)) if n]

    def agent_otps(self, limit=100, since="", number="", platform=""):
        params = {"page": 1, "limit": limit}
        if since:
            params["since"] = since
        if number:
            params["number"] = number
        if platform:
            params["platform"] = platform
        payload = _request_json("GET", _join_url(self.base_url, "otp"), self.api_key, params=params, provider=self.provider)
        return [o for o in (normalize_otp_record(r, self.provider, self.service_default, self.country_default) for r in _walk_records(payload)) if o]

    def fastx_get_number(self, range_prefix):
        payload = _request_json(
            "POST",
            _join_url(self.base_url, "api/getnum"),
            self.api_key,
            json_body={"range": str(range_prefix or "").strip()},
            provider=self.provider,
            quiet_statuses={403, 404},
        )
        records = _walk_records(payload)
        if not records:
            records = [payload]
        return [n for n in (normalize_number_record(r, self.provider, self.service_default, self.country_default) for r in records) if n]

    def fastx_live_access(self):
        payload = _request_json("GET", _join_url(self.base_url, "api/console"), self.api_key, provider=self.provider, quiet_statuses={403, 404})
        ranges = extract_live_ranges(payload)
        if ranges:
            return ranges
        payload = _request_json("GET", _join_url(self.base_url, "api/liveaccess"), self.api_key, provider=self.provider, quiet_statuses={403, 404})
        return extract_live_ranges(payload)

    def fastx_default_range(self, service_hint=""):
        payload = _request_json("GET", _join_url(self.base_url, "api/liveaccess"), self.api_key, provider=self.provider, quiet_statuses={403, 404})
        service_hint = str(service_hint or "").strip().lower()
        candidates = []
        for item in _walk_records(payload):
            if not isinstance(item, dict):
                continue
            item_text = " ".join(str(v) for v in item.values()).lower()
            ranges = item.get("ranges")
            if isinstance(ranges, list):
                for value in ranges:
                    text = str(value).strip()
                    if len(text) >= 3 and re.search(r"\d", text):
                        candidates.append((text, item_text))
                continue
            for key in ("range", "prefix", "cli", "cli_range", "name", "id"):
                value = item.get(key)
                if value is None:
                    continue
                text = str(value).strip()
                if len(text) >= 3 and re.search(r"\d", text) and not re.fullmatch(r"\d{11,}", text):
                    candidates.append((text, item_text))
        if not candidates:
            for value in _walk_values(payload):
                text = str(value or "").strip()
                if len(text) >= 3 and re.search(r"\d", text) and re.search(r"[Xx*]", text) and re.fullmatch(r"[+\dXx*\-\s()]+", text):
                    candidates.append((text, ""))
        if service_hint:
            for text, item_text in candidates:
                if service_hint in item_text:
                    return text
        return candidates[0][0] if candidates else ""

    def fastx_otps(self):
        payload = _request_json("GET", _join_url(self.base_url, "api/otps"), self.api_key, provider=self.provider, quiet_statuses={403, 404})
        return [o for o in (normalize_otp_record(r, self.provider, self.service_default, self.country_default) for r in _walk_records(payload)) if o]


def env_default(name, fallback=""):
    return os.getenv(name, fallback).strip()
