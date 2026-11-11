from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from ..kst import KST

# KB국민은행 입출금 알림 문자 모양
#   [Web발신]
#   [KB]03/15 14:30
#   110234**890      가려진 계좌번호
#   홍길동            입금자명
#   입금              거래 종류
#   50,000           금액
#   잔액12,345        없을 수도 있음
_KB_HEADER = re.compile(r"\[KB\]\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})")
_ACCOUNT = re.compile(r"[\d*-]{6,}")
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
# 가려지지 않은 자리가 이만큼은 있어야 계좌를 구분할 수 있다
_MIN_SHOWN_DIGITS = 4


@dataclass(frozen=True)
class BankSms:
    name: str
    amount: int
    # 은행이 가린 계좌번호 (예: 110234**890)
    account: str
    # 은행 시계 기준 입금 시각 (분 단위)
    sent_at: datetime
    # 문자마다 고유한 키. 같은 문자가 두 번 와도 한 번만 센다
    key: str
    # 은행이 적은 거래 종류: 입금, 타행입금, 입금취소 …
    kind: str = "입금"


def _lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line and line != "[Web발신]"]


def _sent_at(month: int, day: int, hour: int, minute: int, now: datetime) -> datetime | None:
    # 문자에는 연도가 없다. 지금과 가장 가까운 연도로 본다
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, minute, tzinfo=KST))
        except ValueError:  # 없는 날짜
            continue
    return min(candidates, key=lambda moment: abs(moment - now), default=None)


def parse_kb_alert(text: str, *, now: datetime | None = None) -> BankSms | None:
    """KB 알림 문자를 종류에 상관없이 읽는다. 모르는 형식이면 None."""
    lines = _lines(text)
    if len(lines) < 5:
        return None
    header, account, name, kind, amount = lines[:5]
    match = _KB_HEADER.fullmatch(header)
    if not (match and _ACCOUNT.fullmatch(account) and _AMOUNT.fullmatch(amount)):
        return None
    sent_at = _sent_at(*map(int, match.groups()), (now or datetime.now(KST)).astimezone(KST))
    if sent_at is None:
        return None
    # 키: 연도를 채운 시각 + 나머지 줄 전부 ([Web발신] 줄은 빼고)
    key = hashlib.sha256(
        "\n".join([sent_at.strftime("%Y-%m-%d %H:%M"), *lines[1:]]).encode()
    ).hexdigest()
    return BankSms(
        name=name, amount=int(amount.replace(",", "")), account=account, sent_at=sent_at, key=key,
        kind=kind,
    )


def is_deposit_kind(kind: str) -> bool:
    # 입금, 타행입금 …
    return kind.endswith("입금")


def is_reversal_kind(kind: str) -> bool:
    """계좌에서 돈이 도로 빠지는 종류: 입금취소, 입금정정, 반환, 지급정지. 일반 입금과 해제(지급정지해제)는 아니다."""
    if is_deposit_kind(kind) or "해제" in kind:
        return False
    return (
        ("입금" in kind and any(word in kind for word in ("취소", "정정")))
        or "반환" in kind
        or "지급정지" in kind
    )


def is_payment_stop(text: str) -> bool:
    # 지급정지해제는 해당 없음
    return "지급정지" in text and "해제" not in text


def mentions_account(text: str, account: str) -> bool:
    """글 속의 계좌번호(가려졌든 아니든) 중에 account일 수 있는 게 있는지."""
    return any(account_matches(token, account) for token in _ACCOUNT.findall(text))


def parse_bank_sms(text: str, *, now: datetime | None = None) -> BankSms | None:
    sms = parse_kb_alert(text, now=now)
    return sms if sms is not None and is_deposit_kind(sms.kind) else None


def account_matches(masked: str, account: str) -> bool:
    """가려진 번호(110234**890)가 account일 수 있는지.

    별표 앞뒤로 보이는 자리가 둘 다 맞아야 하고, 합쳐서 _MIN_SHOWN_DIGITS자리 이상이어야 한다.
    """
    digits = "".join(ch for ch in account if ch.isdigit())
    masked = masked.replace("-", "")
    if "*" not in masked:
        return masked == digits
    head, tail = masked.split("*", 1)[0], masked.rsplit("*", 1)[1]
    return (
        bool(digits) and _MIN_SHOWN_DIGITS <= len(head) + len(tail) < len(digits)
        and digits.startswith(head) and digits.endswith(tail)
    )


def looks_like_deposit(text: str) -> bool:
    # 해석은 못 했어도 입금 얘기가 있으면 사람이 봐야 한다
    return any(line.endswith("입금") for line in _lines(text))


def looks_like_reversal(text: str) -> bool:
    # 취소·지급정지 얘기라면 더더욱. 어느 줄이든 맞으면 되므로 은행 안내 문자도 걸릴 수 있다
    return any(is_reversal_kind(line) for line in _lines(text))
