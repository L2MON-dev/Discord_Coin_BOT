from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from discord.utils import escape_markdown

from .. import config
from ..api.bank_sms import is_payment_stop
from ..database import (
    NOTIFICATION_STATUS,
    SYSTEM_ACTOR,
    BankNotification,
    Database,
    Deposit,
    DepositorName,
    DepositorNameRemovedError,
    DepositorNameTakenError,
    DuplicateNotificationError,
)
from ..errors import UserFacingError
from ..kst import KST, format_kst, to_kst
from .event_log import SKIP_EVENT, EventLog, NullEventLog

log = logging.getLogger(__name__)

MAX_DEPOSITOR_NAME_LENGTH = 20
# 인증 유저의 입금자명 뒤에 붙이는 전화번호 끝자리 수
VERIFIED_PHONE_DIGITS = 4
# 은행 문자의 시각은 분 단위라, 요청 직후에 보낸 돈이 요청보다 최대 이만큼 이르게 찍힐 수 있다
# 그보다 늦게 열린 요청은 그 돈의 요청이 아니다
# 수동 충전에도 같은 여유를 쓴다. 돈을 보낸 뒤에 한 같은 금액의 수동 충전은 그 돈을 이미 넣어 준 것일 수
# 있어서 자동 충전을 멈추고 관리자가 판단한다. 돈을 보내기 전에 한 충전은 해당 없다
SMS_TIME_TOLERANCE = timedelta(minutes=2)
# 신청과 취소는 공짜라 반복하면 로그가 도배된다. 이만큼 취소하면 새 요청을 잠시 막는다
MAX_CANCELLED_REQUESTS = 5
CANCEL_WINDOW_MINUTES = 10
# 요청은 한 번에 하나만 열리므로 열려 있는 동안 다른 사람은 기다린다
# 한 유저가 입금 없이 닫은 요청들이 최근 (UNPAID_HOLD_WINDOW_TIMEOUTS × 마감 시간) 동안
# (UNPAID_HOLD_TIMEOUTS × 마감 시간)만큼 자리를 잡고 있었으면 새 요청을 막는다
UNPAID_HOLD_TIMEOUTS = 3
UNPAID_HOLD_WINDOW_TIMEOUTS = 12
# 미확인 입금을 충전할 때, 입금 이만큼 전부터의 같은 금액 수동 충전을 경고한다 (문자가 늦게 왔을 수 있다)
RESOLVE_LOOKBACK = timedelta(hours=24)
# 이 시간 안에 같은 수동 충전을 또 하면 한 번 더 묻는다
REPEAT_CREDIT_WINDOW = timedelta(hours=24)
# 입금 취소·반환 문자와 대조할 충전 기록의 범위
REVERSAL_LOOKBACK = timedelta(days=7)
# 수동 충전 때 주인 없는 미확인 입금을 경고하는 범위 (유저 본인 이름의 입금은 기간 제한 없음)
UNOWNED_LOOKBACK = timedelta(days=7)


def _sqlite_utc(moment: datetime) -> str:
    # SQLite의 datetime('now')와 같은 형식
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _kst(utc_text: str | None) -> str:
    return format_kst(utc_text, "%m-%d %H:%M")


def _credit_line(credit: dict, *, notification_id: int | None = None) -> str:
    """수동 충전 한 건을 관리자용 한 줄로. notification_id는 지금 보고 있는 입금."""
    tied = credit.get("notification_id")
    note = ""
    if tied is not None:
        note = " · **이 입금에 연결된 충전**" if tied == notification_id else f" · 입금 알림 #{tied}에 연결됨"
    return (
        f"`{_kst(credit['created_at'])}` <@{credit['admin_id']}>님이 **{credit['amount']:,}원** 수동 충전 · "
        f"사유: {escape_markdown(str(credit['reason']))}{note}"
    )


def _notification_line(n: BankNotification) -> str:
    state = NOTIFICATION_STATUS.get(n.status, n.status)
    if n.credited_user_id is not None and n.status in ("matched", "resolved"):
        state = f"<@{n.credited_user_id}>님에게 {state}"
    elif n.status in ("unmatched", "reversal"):
        state = f"**{state}**" + (" (아직 아무에게도 충전 안 됨)" if n.status == "unmatched" else "")
    return (f"입금 알림 `#{n.id}` `{_kst(n.received_at)}` · **{escape_markdown(n.depositor_name)}** "
            f"{n.amount:,}원 · {state}")


def unpaid_hold_free_at(holds: list[tuple[str, int]], timeout_minutes: int) -> datetime | None:
    """다시 신청할 수 있는 시각. 지금 가능하면 None (UNPAID_HOLD_TIMEOUTS 참고).

    holds는 Database.list_unpaid_holds 결과(오래된 순). 오래된 요청부터 기간 밖으로 빠지므로
    충분히 빠졌을 때가 기다림이 끝나는 시각이다.
    """
    budget = UNPAID_HOLD_TIMEOUTS * timeout_minutes * 60
    window = timedelta(minutes=UNPAID_HOLD_WINDOW_TIMEOUTS * timeout_minutes)
    total = sum(held for _, held in holds)
    if total < budget:
        return None
    for created_at, held in holds:
        total -= held
        if total < budget:
            return (to_kst(created_at) or datetime.now(KST)) + window
    return datetime.now(KST) + window  # 여기까지 오지 않는다


def normalize_depositor_name(name: str) -> str:
    """입금자명 비교용 키: NFKC, 소문자, 공백 제거."""
    return "".join(unicodedata.normalize("NFKC", name).split()).casefold()


def verified_depositor_name(real_name: str, phone_last4: str) -> str | None:
    """인증된 실명 + 전화번호 뒷 4자리 (홍길동, 5678 → 홍길동5678).

    숫자가 4자리가 아니거나, 이름이 글자만이 아니거나 2자 미만이거나, 합쳐서 너무 길면 None.
    """
    name = "".join(unicodedata.normalize("NFKC", real_name).split())
    digits = phone_last4.strip()
    if not (digits.isascii() and digits.isdigit() and len(digits) == VERIFIED_PHONE_DIGITS):
        return None
    if len(name) < 2 or not name.isalpha() or len(name) + len(digits) > MAX_DEPOSITOR_NAME_LENGTH:
        return None
    return name + digits


@dataclass
class VerifiedAssignment:
    # assigned 지금 지정 / exists 이미 이름 있음 / removed 관리자가 해제했던 유저
    # taken 남이 쓰는 이름 / invalid 이름을 만들 수 없음 / blocked 정지된 유저
    status: str
    # assigned일 때와, exists인데 인증된 본인 이름과 같을 때만 채운다
    name: str | None = None


@dataclass
class IncomingTransfer:
    notification: BankNotification
    deposit: Deposit | None
    new_balance: int | None
    # 이미 받은 문자. notification은 처음 저장된 것
    duplicate: bool = False
    # 수동 충전으로 이미 받은 돈일 수 있어 충전하지 않고 관리자에게 넘긴 경우
    held: bool = False


class DepositService:
    """충전 요청과 입금 매칭. 입금은 관리자가 지정한 입금자명의 주인에게만 충전된다."""

    def __init__(self, db: Database, events: EventLog | NullEventLog | None = None) -> None:
        self._db = db
        self._events = events or NullEventLog()
        # 입금 취소 문자는 한 번에 하나씩 처리한다. 같은 문자가 동시에 두 번 오면 경고도 두 번 나간다
        self._reversal_lock = asyncio.Lock()

    async def get_depositor_name(self, discord_id: int) -> str | None:
        record = await self._db.get_depositor_name(discord_id)
        return record.name if record else None

    async def get_pending_deposit(self, discord_id: int) -> Deposit | None:
        return await self._db.get_pending_deposit(
            discord_id, config.DEPOSIT_TIMEOUT_MINUTES
        )

    async def other_pending_since(self, discord_id: int) -> str | None:
        return await self._db.other_pending_since(discord_id, config.DEPOSIT_TIMEOUT_MINUTES)

    async def request_deposit(
        self, discord_id: int, amount: int, channel_id: int | None = None
    ) -> Deposit:
        """다른 유저의 요청이 열려 있으면 DepositSlotBusyError."""
        record = await self._db.get_depositor_name(discord_id)
        if record is None:
            raise UserFacingError(
                "충전하려면 관리자가 입금자명을 먼저 지정해야 해요.",
                title="입금자명이 필요해요",
                tone="warning",
                hint="은행 입금 때 표시될 이름을 관리자에게 알려 주세요",
            )
        cancelled = await self._db.count_cancelled_deposits(discord_id, CANCEL_WINDOW_MINUTES)
        if cancelled >= MAX_CANCELLED_REQUESTS:
            await self._events.record(
                "deposit.rate_limited",
                f"{CANCEL_WINDOW_MINUTES}분 안에 충전 요청을 {cancelled}번 취소해서 새 요청을 잠시 막았어요.\n"
                "같은 유저가 계속 반복하면 운영 로그를 도배하려는 시도일 수 있어요.",
                level="warning",
                user_id=discord_id,
                dedup=f"deposit.rate_limited:{discord_id}",
            )
            raise UserFacingError(
                "짧은 시간에 충전 요청을 여러 번 취소해서 잠시 새로 신청할 수 없어요.",
                title="잠시 후 다시 신청해 주세요",
                tone="warning",
                facts=[(f"최근 {CANCEL_WINDOW_MINUTES}분 취소", f"{cancelled}번")],
                hint=f"{CANCEL_WINDOW_MINUTES}분 안에 {MAX_CANCELLED_REQUESTS}번까지 취소할 수 있어요",
            )
        await self._refuse_if_held_too_long(discord_id)
        deposit = await self._db.create_deposit(
            discord_id=discord_id,
            amount=amount,
            depositor_name=record.name,
            timeout_minutes=config.DEPOSIT_TIMEOUT_MINUTES,
            channel_id=channel_id,
        )
        await self._events.record(
            "deposit.request",
            f"**{amount:,}원** 충전을 신청했어요 · 입금자명 **{escape_markdown(record.name)}**",
            user_id=discord_id,
            ref=("deposit", deposit.id),
        )
        return deposit

    async def _refuse_if_held_too_long(self, discord_id: int) -> None:
        """입금 없이 자리를 오래 잡았던 유저는 잠시 기다리게 한다 (UNPAID_HOLD_TIMEOUTS)."""
        timeout = config.DEPOSIT_TIMEOUT_MINUTES
        window_minutes = UNPAID_HOLD_WINDOW_TIMEOUTS * timeout
        holds = await self._db.list_unpaid_holds(discord_id, window_minutes, timeout)
        free_at = unpaid_hold_free_at(holds, timeout)
        if free_at is None:
            return
        held_minutes = sum(held for _, held in holds) // 60
        free_text = f"{free_at.astimezone(KST):%H:%M}"
        await self._events.record(
            "deposit.hold_limited",
            f"최근 {window_minutes}분 동안 입금 없이 끝난 충전 요청(마감·취소)이 모두 {held_minutes}분 동안 "
            f"충전 자리를 잡고 있어서, {free_text}까지 새 요청을 막았어요.\n"
            "충전은 한 번에 한 명만 받아서 그동안 다른 유저는 신청하지 못했어요. 계속 반복하면 다른 유저의 "
            "충전을 일부러 막는 걸 수 있으니 '유저 보기'로 확인하고, 필요하면 `/관리자 이용정지`로 막으세요.",
            level="warning",
            user_id=discord_id,
            dedup=f"deposit.hold_limited:{discord_id}",
        )
        raise UserFacingError(
            "입금하지 않고 끝난 충전 요청이 최근에 많아서 잠시 새로 신청할 수 없어요.",
            title="잠시 후 다시 신청해 주세요",
            tone="warning",
            facts=[("다시 신청", f"{free_text}부터")],
            hint="충전은 한 번에 한 분씩 받아서, 요청이 열려 있는 동안 다른 분은 기다려요 · "
                 "입금할 준비가 된 뒤에 신청해 주세요",
        )

    async def cancel_deposit(self, discord_id: int, deposit_id: int) -> bool:
        cancelled = await self._db.cancel_deposit(deposit_id, discord_id)
        if cancelled:
            await self._events.record(
                "deposit.cancel", "충전 요청을 직접 취소했어요.",
                user_id=discord_id, ref=("deposit", deposit_id),
            )
        return cancelled

    async def collect_expired(self) -> list[Deposit]:
        expired = await self._db.collect_expired_deposits(config.DEPOSIT_TIMEOUT_MINUTES)
        for deposit in expired:
            await self._events.record(
                "deposit.expire",
                f"**{deposit.amount:,}원** 충전 요청이 {config.DEPOSIT_TIMEOUT_MINUTES}분 안에 "
                "입금되지 않아 마감됐어요.",
                user_id=deposit.discord_id,
                ref=("deposit", deposit.id),
            )
        return expired

    async def process_incoming(
        self,
        depositor_name: str,
        amount: int,
        *,
        sent_at: datetime | None = None,
        sms_key: str | None = None,
    ) -> IncomingTransfer:
        """입금 알림을 기록하고, 맞는 충전 요청이 있으면 충전한다.

        sent_at(문자 시각)이 있으면 그 뒤에 열린 요청은 맞추지 않는다. sms_key가 같으면 같은 문자라 무시한다.
        돈을 보낸 뒤에 같은 금액의 수동 충전이 있었으면 자동 충전하지 않고 관리자에게 넘긴다.
        """
        paid_at = sent_at or datetime.now(timezone.utc)
        try:
            notification, deposit, new_balance, held = await self._db.record_incoming_transfer(
                depositor_name=depositor_name,
                name_key=normalize_depositor_name(depositor_name),
                amount=amount,
                timeout_minutes=config.DEPOSIT_TIMEOUT_MINUTES,
                opened_by=_sqlite_utc(sent_at + SMS_TIME_TOLERANCE) if sent_at else None,
                sms_key=sms_key,
                manual_since=_sqlite_utc(paid_at - SMS_TIME_TOLERANCE),
            )
        except DuplicateNotificationError as exc:
            await self._record_duplicate(exc.notification)
            return IncomingTransfer(exc.notification, None, None, duplicate=True)
        name = escape_markdown(depositor_name)
        if deposit is not None and held is None:
            log.info(
                "Deposit completed: user=%s amount=%d new_balance=%s notification=%s",
                deposit.discord_id, deposit.amount, new_balance, notification.id,
            )
            await self._events.record(
                "deposit.credit",
                f"**{name}** 입금 **{amount:,}원**이 자동 충전됐어요 · 잔액 {new_balance:,}원",
                level="success",
                user_id=deposit.discord_id,
                ref=("deposit", deposit.id),
                details={"입금 알림": f"#{notification.id}"},
            )
            return IncomingTransfer(notification, deposit, new_balance)
        if held is not None:
            owner_id = held["discord_id"]
            reason = (
                "관리자가 이 유저에게 같은 금액을 이미 수동 충전해서, 같은 돈을 두 번 충전하지 않도록 "
                "자동 충전하지 않았어요"
                + (f" (충전 요청 #{deposit.id}은 그 수동 충전으로 마감했어요)" if deposit else "")
                + ".\n" + _credit_line(held, notification_id=notification.id) + "\n"
                "은행 앱에서 이 입금이 그 수동 충전과 **다른 돈**일 때만 충전하고, 같은 돈이면 "
                "'봇과 무관 · 제외'를 눌러 주세요."
            )
        else:
            owner_id, reason = await self._unmatched_reason(notification.name_key, amount, sent_at)
        await self._events.record(
            "deposit.unmatched",
            f"**{name}** 님이 **{amount:,}원**을 보냈지만 자동 충전하지 못했어요.\n"
            f"이유: {reason}",
            level="warning",
            user_id=owner_id,
            ref=("notification", notification.id),
        )
        return IncomingTransfer(notification, deposit, new_balance, held=held is not None)

    async def _record_duplicate(self, original: BankNotification) -> None:
        log.info("Bank SMS received again: notification=%s", original.id)
        owner_id = original.credited_user_id or await self._db.get_depositor_owner(original.name_key)
        await self._events.record(
            "deposit.duplicate",
            f"이미 받은 입금 문자가 다시 들어와 무시했어요 · **{escape_markdown(original.depositor_name)}** "
            f"{original.amount:,}원 (처음 받은 입금 알림 #{original.id})\n"
            "단축어가 같은 문자로 두 번 실행된 경우예요. 정말로 같은 시각에 같은 금액이 두 번 "
            "들어왔다면 은행 앱에서 확인한 뒤 `/관리자 잔액충전`으로 충전하세요.",
            level="warning",
            user_id=owner_id,
            ref=("notification", original.id),
            dedup=f"duplicate:{original.id}",
        )

    async def _unmatched_reason(
        self, name_key: str, amount: int, sent_at: datetime | None
    ) -> tuple[int | None, str]:
        """(입금자명 주인, 자동 충전되지 않은 이유)"""
        owner_id = await self._db.get_depositor_owner(name_key)
        if owner_id is None:
            return None, "어느 유저에게도 지정되지 않은 입금자명이에요."
        pending = await self.get_pending_deposit(owner_id)
        if pending is not None and pending.amount == amount and sent_at is not None:
            return owner_id, (
                f"입금 시각(문자 기준 {sent_at.astimezone(KST):%H:%M})이 충전 요청보다 빨라요. "
                "요청 전에 보낸 돈이거나, 예전 입금 문자가 다시 들어온 경우예요."
            )
        if pending is not None:
            return owner_id, f"충전 요청 금액({pending.amount:,}원)과 입금액이 달라요."
        last = await self._db.get_last_deposit(owner_id)
        if last is not None and last.status == "expired" and last.amount == amount:
            return owner_id, "충전 요청이 이미 마감된 뒤에 입금됐어요."
        if last is not None and last.status == "cancelled" and last.amount == amount:
            return owner_id, "유저가 취소한 충전 요청의 금액이에요."
        return owner_id, "진행 중인 충전 요청이 없어요."

    async def assign_depositor_name(self, discord_id: int, name: str, admin_id: int) -> str:
        cleaned = " ".join(name.split())
        key = normalize_depositor_name(cleaned)
        if not key:
            raise UserFacingError(
                "은행 입금 알림에 표시되는 이름을 그대로 입력해 주세요.",
                title="입금자명이 비어 있어요",
                tone="warning",
            )
        if len(cleaned) > MAX_DEPOSITOR_NAME_LENGTH:
            raise UserFacingError(
                f"입금자명은 {MAX_DEPOSITOR_NAME_LENGTH}자까지 쓸 수 있어요.",
                title="입금자명이 너무 길어요",
                tone="warning",
            )
        newly_usable = (
            discord_id not in config.ALLOWED_USERS and discord_id not in config.ADMIN_USERS
            and discord_id not in config.BLOCKED_USERS
        )
        try:
            await self._db.set_depositor_name(discord_id, cleaned, key, admin_id)
        except DepositorNameTakenError as exc:
            raise UserFacingError(
                f"이 입금자명은 이미 <@{exc.owner_id}>님에게 지정돼 있어요.",
                title="이미 쓰고 있는 입금자명이에요",
                tone="warning",
                hint="다른 이름을 쓰거나, 먼저 /관리자 입금자명해제 로 해제해 주세요",
            ) from exc
        # config.ALLOWED_USERS가 실제 허용 목록이다 (.env + 입금자명을 받은 유저)
        config.ALLOWED_USERS.add(discord_id)
        log.info("Depositor name assigned: user=%s admin=%s", discord_id, admin_id)
        await self._events.record(
            "admin.depositor_set",
            f"<@{admin_id}>님이 입금자명을 지정했어요 (**{escape_markdown(cleaned)}**)."
            + (" 봇 사용 권한도 함께 부여했어요." if newly_usable else ""),
            user_id=discord_id,
        )
        return cleaned

    async def assign_verified_name(
        self, discord_id: int, real_name: str, phone_last4: str
    ) -> VerifiedAssignment:
        """본인인증을 마친 유저에게 입금자명(실명+전화 뒷자리)을 주고 봇을 쓸 수 있게 한다.

        이미 이름이 있으면 바꾸지 않는다. 연동 키가 새더라도 남의 이름(과 그 이름으로 들어오는 돈)을
        다른 계정으로 옮길 수 없게 하려는 것이다. 정지된 유저와 관리자가 이름을 해제한 유저에게는
        주지 않고, 다른 유저가 쓰는 이름이면 관리자에게 넘긴다.
        """
        if discord_id in config.BLOCKED_USERS:
            log.info("Verified user %s is stopped; no depositor name assigned", discord_id)
            return VerifiedAssignment("blocked")
        name = verified_depositor_name(real_name, phone_last4)
        key = normalize_depositor_name(name) if name is not None else None
        existing = await self._db.get_depositor_name(discord_id)
        if existing is not None:
            return self._kept_name(existing, key)
        if await self._db.was_given_name(discord_id):
            return await self._refuse_removed(discord_id)
        if name is None:
            # 확인되지 않은 이름이라 로그에는 적지 않는다
            await self._events.record(
                "deposit.verify_invalid",
                "본인인증을 마쳤지만 입금자명을 자동으로 만들지 못했어요. 이름이 한글·영문 글자만이 "
                f"아니거나, 전화번호 뒷 {VERIFIED_PHONE_DIGITS}자리를 붙이면 "
                f"{MAX_DEPOSITOR_NAME_LENGTH}자를 넘어요.\n"
                "인증 봇의 `/정보`로 이름을 확인한 뒤 `/관리자 입금자명지정`으로 직접 지정해 주세요.",
                level="warning",
                user_id=discord_id,
                dedup=f"deposit.verify_invalid:{discord_id}",
            )
            return VerifiedAssignment("invalid")
        newly_usable = (
            discord_id not in config.ALLOWED_USERS and discord_id not in config.ADMIN_USERS
        )
        try:
            record, created = await self._db.claim_depositor_name(discord_id, name, key, SYSTEM_ACTOR)
        except DepositorNameRemovedError:  # 위 조회와 claim 사이에 해제된 경우
            return await self._refuse_removed(discord_id)
        except DepositorNameTakenError as exc:
            await self._events.record(
                "deposit.verify_conflict",
                f"본인인증을 마쳤지만, 입금자명 **{escape_markdown(name)}**은 이미 <@{exc.owner_id}>님이 "
                "쓰고 있어서 지정하지 않았어요.\n"
                "같은 사람이 다른 계정으로 인증했거나, 이름과 전화번호 뒷자리가 같은 다른 사람이에요. "
                "확인한 뒤 `/관리자 입금자명지정`으로 직접 지정해 주세요.",
                level="warning",
                user_id=discord_id,
                dedup=f"deposit.verify_conflict:{discord_id}",
            )
            return VerifiedAssignment("taken")
        if not created:  # 그사이 이름이 지정된 경우
            return self._kept_name(record, key)
        config.ALLOWED_USERS.add(discord_id)
        log.info("Depositor name assigned after verification: user=%s", discord_id)
        await self._events.record(
            "deposit.verified",
            f"본인인증을 마쳐서 입금자명을 자동으로 지정했어요 (**{escape_markdown(record.name)}**)."
            + (" 봇 사용 권한도 함께 부여했어요." if newly_usable else ""),
            level="success",
            user_id=discord_id,
        )
        return VerifiedAssignment("assigned", record.name)

    @staticmethod
    def _kept_name(record: DepositorName, verified_key: str | None) -> VerifiedAssignment:
        """이름이 이미 있으면 그대로 둔다. 인증된 본인 이름과 같을 때만 이름을 알려 준다 (남의 계정 이름 조회에 쓰이지 않게)."""
        return VerifiedAssignment("exists", record.name if record.name_key == verified_key else None)

    async def _refuse_removed(self, discord_id: int) -> VerifiedAssignment:
        await self._events.record(
            "deposit.verify_removed",
            "관리자가 입금자명을 해제한 유저가 본인인증 버튼을 다시 눌렀어요. 입금자명은 자동으로 "
            "다시 지정하지 않았어요.\n"
            "다시 쓰게 하려면 `/관리자 입금자명지정`으로 직접 지정해 주세요.",
            user_id=discord_id,
            dedup=f"deposit.verify_removed:{discord_id}",
        )
        return VerifiedAssignment("removed")

    async def remove_depositor_name(self, discord_id: int, admin_id: int) -> bool:
        removed = await self._db.remove_depositor_name(discord_id)
        if removed:
            log.info("Depositor name removed: user=%s admin=%s", discord_id, admin_id)
            await self._events.record(
                "admin.depositor_remove", f"<@{admin_id}>님이 입금자명 지정을 해제했어요.",
                user_id=discord_id,
            )
        return removed

    async def list_depositor_names(self) -> list[DepositorName]:
        return await self._db.list_depositor_names()

    async def block_user(self, discord_id: int, reason: str, admin_id: int) -> bool:
        """유저를 바로 정지한다. 잔액은 그대로 둔다. 이미 정지돼 있었으면 False."""
        reason = " ".join(reason.split())
        if not reason:
            raise UserFacingError(
                "나중에 확인할 수 있게 정지 사유를 적어 주세요.",
                title="정지 사유가 비어 있어요",
                tone="warning",
            )
        if discord_id in config.ADMIN_USERS:
            raise UserFacingError(
                "관리자 권한은 서버 설정(.env)의 ADMIN_USERS에서 빼야 없어져요.",
                title="관리자는 정지할 수 없어요",
                tone="warning",
            )
        newly = await self._db.block_user(discord_id, reason, admin_id)
        config.BLOCKED_USERS.add(discord_id)
        log.info("User blocked: user=%s admin=%s", discord_id, admin_id)
        await self._events.record(
            "admin.block",
            f"<@{admin_id}>님이 봇 이용을 정지했어요."
            + ("" if newly else " (이미 정지된 유저라 사유만 바꿨어요)")
            + f"\n사유: {escape_markdown(reason)}",
            level="warning",
            user_id=discord_id,
        )
        return newly

    async def unblock_user(self, discord_id: int, admin_id: int) -> bool:
        unblocked = await self._db.unblock_user(discord_id)
        config.BLOCKED_USERS.discard(discord_id)
        if unblocked:
            log.info("User unblocked: user=%s admin=%s", discord_id, admin_id)
            await self._events.record(
                "admin.unblock", f"<@{admin_id}>님이 봇 이용 정지를 풀었어요.",
                level="success", user_id=discord_id,
            )
        return unblocked

    async def list_unmatched(self, limit: int = 15) -> list[BankNotification]:
        return await self._db.list_unmatched_notifications(limit)

    async def resolve_warnings(self, notification: BankNotification, discord_id: int) -> list[str]:
        """이 미확인 입금을 충전하면 이중 충전이거나 이미 빠져나간 돈일 수 있는 이유들.

        입금 RESOLVE_LOOKBACK 전부터의 같은 금액 수동 충전과 입금 취소 문자를 찾는다.
        """
        if notification.status != "unmatched":
            return []
        received = to_kst(notification.received_at) or datetime.now(timezone.utc)
        since = _sqlite_utc(received - RESOLVE_LOOKBACK)
        lines = [
            _credit_line(c, notification_id=notification.id)
            for c in await self._db.find_manual_credits(discord_id, notification.amount, since)
        ]
        reversals = await self._db.find_notifications(
            statuses=("reversal",), amount=notification.amount, since=since, name_key=notification.name_key
        )
        lines += [_notification_line(r) + " · 이 돈이 빠져나갔을 수 있어요" for r in reversals]
        return lines

    async def credit_warnings(self, discord_id: int, amount: int) -> tuple[list[str], Deposit | None]:
        """이 수동 충전이 이미 받은 돈일 수 있는 이유들과, 같은 금액으로 열려 있는 충전 요청."""
        now = datetime.now(timezone.utc)
        lines = []
        for n in await self._db.find_unmatched_for_credit(
            discord_id, amount, _sqlite_utc(now - UNOWNED_LOOKBACK)
        ):
            whose = "이 유저의 입금자명" if n.owner_id == discord_id else "주인 없는 입금자명"
            lines.append(_notification_line(n) + f" ({whose}) · 이 돈이면 `/관리자 미확인입금충전`으로 처리하세요")
        since = _sqlite_utc(now - REPEAT_CREDIT_WINDOW)
        for n in await self._db.find_notifications(
            statuses=("matched", "resolved"), amount=amount, since=since, credited_user_id=discord_id
        ):
            lines.append(_notification_line(n) + " · 이미 충전된 입금이에요")
        for credit in await self._db.find_manual_credits(discord_id, amount, since):
            lines.append(_credit_line(credit) + " · 이미 한 충전이에요")
        record = await self._db.get_depositor_name(discord_id)
        if record is not None:
            for r in await self._db.find_notifications(
                statuses=("reversal",), amount=amount, since=_sqlite_utc(now - REVERSAL_LOOKBACK),
                name_key=record.name_key,
            ):
                lines.append(_notification_line(r) + " · 이 돈은 빠져나갔을 수 있어요")
        pending = await self.get_pending_deposit(discord_id)
        if pending is not None and pending.amount == amount:
            lines.append(
                f"진행 중인 충전 요청 #{pending.id} {pending.amount:,}원 · 이 요청의 돈이면 충전하면서 요청을 "
                "마감해서, 나중에 들어오는 입금 문자가 또 충전되지 않게 해요"
            )
        else:
            pending = None
        return lines, pending

    async def record_reversal(
        self,
        depositor_name: str,
        amount: int,
        kind: str,
        *,
        sent_at: datetime | None = None,
        sms_key: str | None = None,
    ) -> tuple[BankNotification | None, bool]:
        """입금 취소·정정·반환 문자를 관리자에게 알린다. 잔액은 건드리지 않는다.

        문자는 저장해 둔다 (status 'reversal'). 같은 문자가 재시작 뒤에 또 와도 경고가 한 번만 나가고,
        나중에 그 이름·금액의 입금을 충전하려 하면 경고가 뜬다.
        저장이나 기록 조회가 실패해도 경고는 나간다. 저장만 되고 경고가 못 나간 문자는 다시 오면 경고한다.
        (저장된 알림 또는 None, 이미 경고했던 문자인지)를 돌려준다.
        """
        async with self._reversal_lock:
            return await self._record_reversal(
                depositor_name, amount, kind, sent_at=sent_at, sms_key=sms_key
            )

    async def _record_reversal(
        self, depositor_name: str, amount: int, kind: str, *, sent_at: datetime | None, sms_key: str | None
    ) -> tuple[BankNotification | None, bool]:
        name_key = normalize_depositor_name(depositor_name)
        stored, duplicate = None, False
        try:
            stored, duplicate = await self._db.record_reversal(depositor_name, name_key, amount, sms_key=sms_key)
        except Exception:
            log.exception("Storing a reversal SMS failed", extra={SKIP_EVENT: True})
        if duplicate and await self._alerted(stored):
            log.info("Reversal SMS received again: notification=%s", stored.id)
            return stored, True
        moment = sent_at or datetime.now(timezone.utc)
        try:
            lines, users, balances = await self._reversal_records(name_key, amount, moment, stored)
            records = (f"최근 {REVERSAL_LOOKBACK.days}일 같은 이름·금액 기록:\n" + "\n".join(lines) if lines
                       else f"이 이름·금액으로 최근 {REVERSAL_LOOKBACK.days}일 안에 들어온 입금은 없어요.")
        except Exception:
            log.exception("Reading the records for a reversal SMS failed", extra={SKIP_EVENT: True})
            users, balances = [], []
            records = ("**관련 충전 기록을 불러오지 못했어요 (DB 오류).** `/관리자 미확인입금목록`과 "
                       "'유저 보기'로 이 이름·금액의 충전을 직접 확인하세요.")
        log.warning("Deposit reversal SMS: kind=%r name=%r amount=%d", kind, depositor_name, amount)
        stopped = await self._stop_for_reversal(users, stored, kind)
        await self._events.record(
            "deposit.reversal",
            f"**{escape_markdown(depositor_name)}** {amount:,}원 **{escape_markdown(kind)}** 문자가 왔어요. "
            "이미 들어온 입금이 취소·반환된 거라면 그 돈은 계좌에 없어요.\n"
            + records
            + ("\n" + " · ".join(balances) if balances else "")
            + ("\n" + stopped if stopped else "")
            + ("" if stored is not None else
               "\n**이 문자를 저장하지 못했어요 (DB 오류).** 저장되기 전까지는 이 이름·금액의 입금을 충전할 때 "
               "'빠져나간 돈' 경고가 뜨지 않아요 · 단축어가 다시 보내면 저장되고 이 경고가 한 번 더 떠요")
            + "\n은행 앱에서 확인하고, 충전한 돈이 빠져나간 거라면 `/관리자 이용정지`로 송금부터 막은 뒤 "
            "`/관리자 잔액차감`으로 바로잡으세요. 이미 코인으로 보냈다면 '유저 보기'에서 송금 내역을 확인하세요.",
            level="error",
            user_id=users[0] if users else None,
            ref=("notification", stored.id) if stored is not None else None,
            details={"문자 시각": f"{moment.astimezone(KST):%m-%d %H:%M}", "거래 종류": kind},
        )
        return stored, False  # 저장은 전에 됐어도 경고는 지금 나갔다

    async def _stop_for_reversal(
        self, users: list[int], stored: BankNotification | None, kind: str
    ) -> str:
        """빠져나갔을 수 있는 돈이 코인으로 나가지 않게, 관리자가 보기 전에 관련 유저를 정지한다.

        지급정지 문자면 충전·송금 전체를 멈춘다. 예외를 올리지 않고, 경고에 넣을 줄들을 돌려준다.
        """
        ref = f" · 입금 알림 #{stored.id}" if stored is not None else ""
        reason = f"입금 취소·반환 문자로 자동 정지 ({kind}{ref})"
        stopped, failed = [], []
        for uid in users:
            if uid in config.ADMIN_USERS:
                continue
            try:
                await self._db.auto_block_user(uid, reason)
            except Exception:
                log.exception("Stopping user %s after a reversal SMS failed", uid, extra={SKIP_EVENT: True})
                failed.append(uid)
                continue
            config.BLOCKED_USERS.add(uid)
            stopped.append(uid)
        lines = []
        if stopped:
            lines.append(
                "**코인으로 빠져나가지 않게 자동으로 이용정지했어요:** " + ", ".join(f"<@{u}>" for u in stopped)
                + " · 은행 앱에서 확인한 뒤 문제없으면 `/관리자 이용정지해제`로 풀어 주세요."
            )
        if failed:
            lines.append(
                "**자동 이용정지에 실패했어요 (DB 오류):** " + ", ".join(f"<@{u}>" for u in failed)
                + " · `/관리자 이용정지`로 바로 막아 주세요."
            )
        if is_payment_stop(kind):
            lines.append(await self.pause_for_stop(f"지급정지 문자 ({kind}{ref})"))
        return "\n".join(lines)

    async def pause_for_stop(self, reason: str) -> str:
        """지급정지 문자가 오면 서비스를 멈춘다. 예외를 올리지 않고, 경고에 넣을 한 줄을 돌려준다."""
        try:
            await self.pause_service(reason, SYSTEM_ACTOR)
        except Exception:
            log.exception("Pausing the service for a payment stop failed", extra={SKIP_EVENT: True})
            return ("**지급정지 문자라서 충전·송금을 멈추려 했지만 실패했어요 (DB 오류).** "
                    "`/관리자 서비스중지`로 바로 멈춰 주세요.")
        return ("**지급정지 문자라서 새 충전 요청과 송금을 모두 멈췄어요.** 계좌의 돈이 묶였을 수 있어요 · "
                "은행에 확인한 뒤 `/관리자 서비스재개`로 다시 여세요.")

    async def pause_service(self, reason: str, by: int) -> bool:
        """새 충전 요청과 송금을 막는다. by는 관리자 id 또는 SYSTEM_ACTOR(지급정지 문자). 이미 멈춰 있었으면 False."""
        newly = await self._db.pause_service(reason, by)
        if newly:
            log.warning("Service paused: by=%s reason=%r", by, reason)
            auto = by == SYSTEM_ACTOR
            await self._events.record(
                "deposit.pause" if auto else "admin.pause",
                ("지급정지 문자가 와서 " if auto else f"<@{by}>님이 ")
                + "새 충전 요청과 송금을 모두 멈췄어요. 이미 바이낸스에 접수된 송금은 멈출 수 없어요.\n"
                f"사유: {escape_markdown(reason)}\n"
                "확인이 끝나면 `/관리자 서비스재개`로 다시 여세요.",
                level="error" if auto else "warning",
            )
        return newly

    async def resume_service(self, by: int) -> dict | None:
        pause = await self._db.resume_service()
        if pause is not None:
            log.info("Service resumed: by=%s", by)
            await self._events.record(
                "admin.resume",
                f"<@{by}>님이 충전과 송금을 다시 열었어요.\n"
                f"멈췄던 사유: {escape_markdown(str(pause.get('reason', '-')))}",
                level="success",
            )
        return pause

    async def _alerted(self, stored: BankNotification | None) -> bool:
        """이 취소 문자로 경고가 나갔는지. 확실하지 않으면 False."""
        if stored is None:
            return False
        try:
            event = await self._db.find_event("deposit.reversal", "notification", stored.id)
            if event is None:
                return False
            await self._db.ensure_event_delivery(event.id)
            return True  # 이미 전달됐거나 재전송 대기에 들어가 있다
        except Exception:
            log.warning("Cannot tell whether reversal #%s was alerted", stored.id, exc_info=True,
                        extra={SKIP_EVENT: True})
            return False

    async def _reversal_records(
        self, name_key: str, amount: int, moment: datetime, stored: BankNotification | None
    ) -> tuple[list[str], list[int], list[str]]:
        """취소 경고에 넣을 (관련 충전 기록 줄들, 관련 유저들, 잔액 줄들)"""
        since = _sqlite_utc(moment - REVERSAL_LOOKBACK)
        owner_id = await self._db.get_depositor_owner(name_key)
        related = await self._db.find_notifications(
            statuses=("matched", "resolved", "unmatched"), amount=amount, since=since,
            name_key=name_key, exclude_id=stored.id if stored is not None else None,
        )
        lines = [
            _notification_line(n) + (" · **충전하지 말고 제외하세요**" if n.status == "unmatched" else "")
            for n in related
        ]
        if owner_id is not None:
            lines += [_credit_line(c) for c in await self._db.find_manual_credits(owner_id, amount, since, 3)]
        users = list(dict.fromkeys(
            [n.credited_user_id for n in related if n.credited_user_id is not None]
            + ([owner_id] if owner_id is not None else [])
        ))
        balances = [f"<@{uid}> 지금 잔액 **{(await self._db.get_user(uid)).balance:,}원**" for uid in users]
        return lines, users, balances

    async def resolve_notification(
        self, notification_id: int, discord_id: int, admin_id: int,
        *, expected_revision: int | None = None,
    ) -> tuple[BankNotification, int]:
        notification, new_balance = await self._db.resolve_notification(
            notification_id, discord_id, admin_id, expected_revision=expected_revision,
        )
        log.info(
            "Unmatched transfer #%s credited: user=%s amount=%d admin=%s",
            notification_id, discord_id, notification.amount, admin_id,
        )
        await self._events.record(
            "admin.resolve",
            f"<@{admin_id}>님이 미확인 입금 #{notification_id} "
            f"(**{escape_markdown(notification.depositor_name)}** {notification.amount:,}원)을 "
            f"충전했어요 · 잔액 {new_balance:,}원",
            level="success",
            user_id=discord_id,
            ref=("notification", notification_id),
        )
        return notification, new_balance

    async def manual_credit(
        self, discord_id: int, amount: int, reason: str, admin_id: int, *,
        other_money: bool = False, for_request: int | None = None,
        expected_revision: int | None = None,
    ) -> tuple[int, Deposit | None]:
        """수동 충전. 사유와 함께 기록된다.

        같은 금액의 열린 충전 요청은 이 충전으로 닫는다 (other_money면 닫지 않음).
        for_request 등 자세한 규칙은 Database.credit_by_hand 참고.
        """
        reason = " ".join(reason.split())
        if not reason:
            raise UserFacingError(
                "나중에 확인할 수 있게 충전 사유를 적어 주세요.",
                title="충전 사유가 비어 있어요",
                tone="warning",
            )
        new_balance, settled = await self._db.credit_by_hand(
            discord_id, amount, reason, admin_id, config.DEPOSIT_TIMEOUT_MINUTES,
            other_money=other_money, for_request=for_request,
            expected_revision=expected_revision,
        )
        log.info(
            "Manual credit: user=%s amount=%d admin=%s reason=%r settled=%s other_money=%s",
            discord_id, amount, admin_id, reason, settled.id if settled else None, other_money,
        )
        await self._events.record(
            "admin.credit",
            f"<@{admin_id}>님이 **{amount:,}원**을 수동 충전했어요 · 잔액 {new_balance:,}원\n"
            f"사유: {escape_markdown(reason)}"
            + (f"\n진행 중이던 충전 요청 #{settled.id}도 이 충전으로 마감했어요." if settled else "")
            + ("\n관리자가 입금과 상관없는 돈(보상·환불 등)으로 표시했어요." if other_money else ""),
            level="success",
            user_id=discord_id,
            ref=("deposit", settled.id) if settled else None,
        )
        return new_balance, settled

    async def manual_debit(
        self, discord_id: int, amount: int, reason: str, admin_id: int
    ) -> int:
        reason = " ".join(reason.split())
        if not reason:
            raise UserFacingError(
                "나중에 확인할 수 있게 차감 사유를 적어 주세요.",
                title="차감 사유가 비어 있어요",
                tone="warning",
            )
        new_balance = await self._db.debit_balance(discord_id, amount, reason, admin_id)
        log.info(
            "Manual debit: user=%s amount=%d admin=%s reason=%r",
            discord_id, amount, admin_id, reason,
        )
        await self._events.record(
            "admin.debit",
            f"<@{admin_id}>님이 잔액에서 **{amount:,}원**을 뺐어요 · 잔액 {new_balance:,}원\n"
            f"사유: {escape_markdown(reason)}",
            level="warning",
            user_id=discord_id,
        )
        return new_balance

    async def dismiss_notification(
        self, notification_id: int, admin_id: int
    ) -> BankNotification:
        notification = await self._db.dismiss_notification(notification_id, admin_id)
        log.info("Bank notification #%s dismissed by admin=%s", notification_id, admin_id)
        await self._events.record(
            "admin.dismiss",
            f"<@{admin_id}>님이 입금 알림 #{notification_id} "
            f"(**{escape_markdown(notification.depositor_name)}** {notification.amount:,}원)을 "
            "봇과 무관한 입금으로 제외했어요.",
            ref=("notification", notification_id),
        )
        return notification
