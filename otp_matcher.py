# otp_matcher.py
# Advanced OTP phone-number matcher for Telegram bot messages.
# Supports full, local, country-code, masked, dirty keyboard/letter mixed formats.

import re
from typing import Any, Iterable, List, Set

PHONE_TOKEN_RE = re.compile(r"(?<!\w)\+?\d[\dA-Za-z*#xX/\\\-_.()\s]{2,45}\d(?!\w)")
DIGIT_RE = re.compile(r"\d+")


def _collect_strings(value: Any, max_depth: int = 2) -> List[str]:
    """Small safe collector for useful Telegram message dict strings."""
    if max_depth < 0 or value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out = []
        # Only keys where Telegram normally keeps human-written searchable text.
        allowed = {
            "text", "caption", "phone_number", "file_name", "title",
            "description", "performer", "mime_type"
        }
        for k, v in value.items():
            if k in allowed:
                out.extend(_collect_strings(v, max_depth - 1))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value[:20]:
            out.extend(_collect_strings(item, max_depth - 1))
        return out
    return []


def get_message_search_text(message) -> str:
    """Extract all text that can realistically contain the target phone number.

    Voice/audio/video_note can only be matched if the number is in caption/reply/file metadata;
    bots cannot understand raw voice audio without speech-to-text.
    """
    parts: List[str] = []

    for attr in ("text", "caption"):
        value = getattr(message, attr, None)
        if value:
            parts.append(str(value))

    # Contact messages can contain a phone number directly.
    contact = getattr(message, "contact", None)
    if contact is not None:
        for attr in ("phone_number", "first_name", "last_name"):
            value = getattr(contact, attr, None)
            if value:
                parts.append(str(value))

    # File names from media messages often contain the number.
    for attr in ("audio", "document", "video", "voice", "animation", "video_note"):
        media = getattr(message, attr, None)
        if media is None:
            continue
        file_name = getattr(media, "file_name", None)
        if file_name:
            parts.append(str(file_name))

    # If a voice/media message replies to a text message containing the phone, match that too.
    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        for attr in ("text", "caption"):
            value = getattr(reply, attr, None)
            if value:
                parts.append(str(value))
        for attr in ("audio", "document", "video", "voice", "animation", "video_note"):
            media = getattr(reply, attr, None)
            if media is not None and getattr(media, "file_name", None):
                parts.append(str(media.file_name))

    # Last fallback: selected string fields from raw message json.
    raw = getattr(message, "json", None)
    if raw:
        parts.extend(_collect_strings(raw, max_depth=2))

    cleaned = []
    seen = set()
    for part in parts:
        part = str(part).replace("\u200b", " ").replace("\xa0", " ").strip()
        if part and part not in seen:
            seen.add(part)
            cleaned.append(part)
    return "\n".join(cleaned)


def extract_phone_candidates(text: str) -> List[str]:
    text = (text or "").replace("\u200b", " ").replace("\xa0", " ")
    candidates: Set[str] = set()

    for match in PHONE_TOKEN_RE.finditer(text):
        token = match.group(0).strip(".,:;|[]{}<>")
        if 4 <= sum(ch.isdigit() for ch in token) <= 25:
            candidates.add(token)

    for line in text.splitlines():
        clean_line = line.strip()
        digit_count = sum(ch.isdigit() for ch in clean_line)
        if 5 <= digit_count <= 25:
            # Whole line handles: 01618 xxx 2470 / 01618 - 02470 / 016****02470
            candidates.add(clean_line.strip(".,:;|[]{}<>"))

    # Longest first usually gives safer full-number matches before short suffixes.
    return sorted([c for c in candidates if c], key=lambda x: (-sum(ch.isdigit() for ch in x), x))


def _phone_variants(phone_digits: str) -> List[str]:
    phone_digits = "".join(filter(str.isdigit, str(phone_digits or "")))
    variants: Set[str] = set()
    if not phone_digits:
        return []
    variants.add(phone_digits)
    variants.add(phone_digits.lstrip("0"))

    for n in (7, 8, 9, 10, 11, 12):
        if len(phone_digits) > n:
            suffix = phone_digits[-n:]
            variants.add(suffix)
            variants.add(suffix.lstrip("0"))

    # Common local format: +8801618202470 stored, OTP text shows 01618202470.
    if len(phone_digits) > 10:
        variants.add("0" + phone_digits[-10:])

    return sorted([v for v in variants if len(v) >= 5], key=lambda x: (-len(x), x))


def _score_against_variant(candidate: str, variant: str) -> int:
    candidate = str(candidate or "")
    variant = "".join(filter(str.isdigit, str(variant or "")))
    if not candidate or not variant:
        return 0

    digit_matches = list(DIGIT_RE.finditer(candidate))
    if not digit_matches:
        return 0

    groups = [m.group(0) for m in digit_matches]
    visible_digits = "".join(groups)
    visible_count = len(visible_digits)

    if visible_count < 5 or visible_count > len(variant):
        return 0

    number_span = candidate[digit_matches[0].start():digit_matches[-1].end()]
    has_mask_or_letters = bool(re.search(r"[A-Za-z*#xX?/\\_.()\-]", number_span)) or len(groups) > 1

    # Avoid sending OTP codes like 123456 to a phone just because it appears in the number.
    if len(groups) == 1 and visible_count < 8 and not has_mask_or_letters:
        return 0

    if visible_digits == variant:
        return 1000 + visible_count

    # Full local number may be a suffix of a stored country-code number.
    if visible_count >= 8 and variant.endswith(visible_digits):
        return 900 + visible_count

    if visible_count >= 9 and visible_digits in variant:
        return 780 + visible_count

    # Masked/dirty middle: 01618xxx2470, 01618foj2470, 016****02470
    if len(groups) < 2:
        return 0

    first_group = groups[0]
    last_group = groups[-1]
    if len(first_group) < 2 or len(last_group) < 2:
        return 0
    if not variant.startswith(first_group):
        return 0
    if not variant.endswith(last_group):
        return 0

    position = len(first_group)
    for group in groups[1:-1]:
        next_position = variant.find(group, position)
        if next_position == -1:
            return 0
        position = next_position + len(group)

    # The more visible digits, the stronger the match.
    return 520 + visible_count + len(groups) * 10


def candidate_match_score(candidate: str, phone_digits: str) -> int:
    """Return 0 if no match, higher score if stronger match."""
    best = 0
    for variant in _phone_variants(phone_digits):
        best = max(best, _score_against_variant(candidate, variant))
    return best
