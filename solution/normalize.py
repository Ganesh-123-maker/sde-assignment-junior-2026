"""Value normalisation / validation for Assignment A.

Pure functions, stdlib only. Each parser returns ``(value, ok)`` where
``value`` is the normalised value (or ``None``) and ``ok`` says whether the
raw input was a valid value of that type. An empty/missing input is handled
by the caller, not here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def normalise_header(header: str) -> str:
    """Lowercase, trim, collapse every run of _ / whitespace to one space."""
    return re.sub(r"[\s_]+", " ", header.strip().lower())


def normalise_phone(raw: str) -> str | None:
    """Return canonical ``+91XXXXXXXXXX`` or ``None`` if invalid.

    Strip every non-digit; drop a leading 91 (12 digits) or 0 (11 digits);
    valid only as exactly 10 digits starting 6/7/8/9.
    """
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y")


def parse_date(raw: str) -> str | None:
    """Parse a day-first date; return ISO ``YYYY-MM-DD`` or ``None``.

    Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (month name
    case-insensitive). Impossible dates are invalid (strptime rejects them).
    """
    s = raw.strip()
    # Normalise the month token for %b (e.g. "01-dec-2026" -> "01-Dec-2026").
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3,9})-(\d{4})$", s)
    if m:
        s = f"{m.group(1)}-{m.group(2).title()}-{m.group(3)}"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


_DECIMAL_RE = re.compile(
    r"^(?:(?:₹|Rs\.?|INR)\s*)?([0-9][0-9,]*(\.[0-9]+)?)$", re.IGNORECASE
)


def parse_decimal(raw: str) -> str | None:
    """Parse a non-negative decimal; return a string with 2 places or None.

    Optional prefix ``₹`` / ``Rs`` / ``Rs.`` / ``INR`` (case-insensitive,
    optional space); commas are thousands separators; must be >= 0;
    rounded to 2 places. Stored as a string so ``12000.10`` stays exact.
    """
    s = raw.strip()
    m = _DECIMAL_RE.match(s)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        val = float(num)
    except ValueError:
        return None
    if val < 0:
        return None
    return f"{round(val + 1e-9, 2):.2f}"


def parse_enum(raw: str, values: list[str]) -> str | None:
    """Case-insensitive enum match; stored lower-case, else None."""
    v = raw.strip().lower()
    return v if v in [x.lower() for x in values] else None


def parse_timestamp(raw) -> datetime | None:
    """Parse an event timestamp to an aware UTC datetime, or None.

    Forms: UTC (``...Z``), explicit offset (``...+05:30``), naive
    (``2026-10-03 10:01:00``) which means IST.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    try:
        if s.endswith("Z"):
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            return dt
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Naive "YYYY-MM-DD HH:MM:SS" without a T is not fromisoformat-safe
        # on every input; try the explicit form.
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def utc_iso(dt: datetime) -> str:
    """Store timestamps as UTC ISO-8601 with an explicit +00:00 offset."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
