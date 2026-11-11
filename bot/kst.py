# SQLite에는 UTC로 저장하고(datetime('now')) 화면에는 한국 시간으로 보여 준다
from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def to_kst(utc_text: str | None) -> datetime | None:
    if not utc_text:
        return None
    try:
        parsed = datetime.fromisoformat(utc_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KST)


def format_kst(utc_text: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    moment = to_kst(utc_text)
    return moment.strftime(fmt) if moment else (utc_text or "-")
