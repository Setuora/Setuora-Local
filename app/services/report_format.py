from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


IST = timezone(timedelta(hours=5, minutes=30))
REPORT_DATE_FORMAT = "%d-%m-%Y"
EXCEL_DATE_FORMAT = "DD-MM-YYYY"


def report_date(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return _local_datetime(value).strftime(REPORT_DATE_FORMAT)
    if isinstance(value, date):
        return value.strftime(REPORT_DATE_FORMAT)
    if isinstance(value, str):
        parsed = _parse_date_text(value)
        if parsed:
            return report_date(parsed)
    return str(value)


def batch_voucher_number(batch: Any, override: str | None = None) -> int | str:
    fallback = getattr(batch, "id", "") or ""
    return numeric_voucher_number(
        override,
        getattr(batch, "tally_voucher_number", None),
        getattr(batch, "batch_number", None),
        fallback=fallback,
    )


def numeric_voucher_number(*values: object, fallback: object = "") -> int | str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if text.isdecimal():
            return int(text)
        match = re.search(r"(\d+)$", text)
        if match:
            return int(match.group(1))
    if isinstance(fallback, int):
        return fallback
    fallback_text = str(fallback).strip()
    return int(fallback_text) if fallback_text.isdecimal() else fallback_text


def _local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def _parse_date_text(value: str) -> date | datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
