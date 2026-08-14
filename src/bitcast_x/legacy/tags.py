"""Frozen legacy connection-tag grammar and referral sanitation."""

import re
from typing import NamedTuple

from bitcast_x.legacy.connections import decode_referral_code


class ParsedTag(NamedTuple):
    """One connection tag extracted from a tweet."""

    tag_type: str
    full_tag: str
    referred_by: str | None
    referral_code: str | None


_HOTKEY = r"([1-9A-HJ-NP-Za-km-z]{47,48})"
_REFERRAL = r"(?:-([a-z0-9_-]+))?"
_SPECS = (
    ("HK", re.compile(rf"Stitch-hk:{_HOTKEY}{_REFERRAL}", re.IGNORECASE), "Stitch-hk:"),
    ("X", re.compile(rf"Stitch3-([a-z0-9]+){_REFERRAL}", re.IGNORECASE), "Stitch3-"),
    ("HK", re.compile(rf"bitcast-hk:{_HOTKEY}{_REFERRAL}", re.IGNORECASE), "bitcast-hk:"),
    ("X", re.compile(rf"bitcast-x([a-z0-9]+){_REFERRAL}", re.IGNORECASE), "bitcast-x"),
)


def extract_tags(text: str) -> tuple[ParsedTag, ...]:
    """Extract all four live v2 tag forms in registry order."""

    output: list[ParsedTag] = []
    for tag_type, pattern, prefix in _SPECS:
        for match in pattern.finditer(text):
            identifier, code = match.group(1), match.group(2)
            full_tag = f"{prefix}{identifier}" + (f"-{code}" if code else "")
            output.append(ParsedTag(tag_type, full_tag, decode_referral_code(code), code))
    return tuple(output)


def sanitize_referral(
    author: str, referred_by: str | None, referral_code: str | None
) -> tuple[str | None, str | None]:
    """Drop self-referrals and normalize handles."""

    normalized = referred_by.casefold().lstrip("@") if referred_by else None
    if normalized == author.casefold().lstrip("@"):
        return None, None
    return normalized, referral_code
