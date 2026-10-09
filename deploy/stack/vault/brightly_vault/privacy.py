"""Rules every review output passes through, whatever the model wrote.

* No tax file numbers, ever: any number passing the ATO check digit is replaced.
* Long account / member / ID numbers are cut to their last 4 digits.
* Health and medical details never go in the review: the client record sent to the model has
  them removed, and the model is told to flag "health information present" instead.
"""

from __future__ import annotations

import re
from typing import Any

_TFN_WEIGHTS = {9: (1, 4, 3, 7, 5, 8, 6, 9, 10), 8: (10, 7, 8, 4, 6, 3, 5, 1)}
_TFN_CANDIDATE = re.compile(r"(?<![\d])(\d{3}[ -]?\d{3}[ -]?\d{2,3})(?![\d])")
TFN_MARK = "[TFN removed]"
# 9+ digits (spaces/hyphens allowed), not part of an amount like $1,234,567.00; a full stop
# or comma straight after is fine as long as no digit follows it.
_LONG_NUMBER = re.compile(r"(?<![\d$.,])(\d[\d -]{7,}\d)(?!\d|[.,]\d)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[ -]\d{1,2}[ -]\d{4}")
_PHONE = re.compile(r"(?:0[2-478]\d{8}|61[2-478]\d{8}|1[38]00\d{6})")
HEALTH_KEY = re.compile(r"health|medic|smok|condition|illness|disab|diagnos|mental|alcohol|"
                        r"drug|height|weight|bmi|pregnan|hospital|symptom|treatment", re.I)


def is_tfn(digits: str) -> bool:
    digits = re.sub(r"\D", "", digits)
    weights = _TFN_WEIGHTS.get(len(digits))
    if not weights or len(set(digits)) == 1:
        return False
    return sum(int(d) * w for d, w in zip(digits, weights)) % 11 == 0


def _mask(m: re.Match) -> str:
    raw = m.group(1)
    digits = re.sub(r"\D", "", raw)
    if _DATE.fullmatch(raw.strip()):
        return raw
    if is_tfn(digits):
        return TFN_MARK
    if len(digits) >= 9 and not _PHONE.fullmatch(digits):
        return "****" + digits[-4:]
    return raw


def clean_text(text: str) -> str:
    text = _TFN_CANDIDATE.sub(lambda m: TFN_MARK if is_tfn(m.group(1)) else m.group(1), text)
    return _LONG_NUMBER.sub(_mask, text)


def clean(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def without_health(value: Any) -> Any:
    """The client record minus health fields, before it goes to the model."""
    if isinstance(value, dict):
        return {k: without_health(v) for k, v in value.items() if not HEALTH_KEY.search(str(k))}
    if isinstance(value, list):
        return [without_health(v) for v in value]
    return value
