from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from .. import config
from ..database import Database
from ..kst import KST
from .binance_client import BinanceClient
from .event_log import EventLog, exception_details
from .price_service import PriceService

log = logging.getLogger(__name__)

# 연속으로 이만큼 실패하면(1분에 한 번 확인) 장애로 알린다
FAIL_THRESHOLD = 3
# 기준을 넉넉히 넘어야 회복으로 본다. 기준 근처를 오갈 때 매분 알림이 오지 않게
FUNDS_RECOVERY_FACTOR = Decimal("1.2")
SUMMARY_HOUR_KST = 9
EVENT_KEEP_DAYS = 90
# 백업은 하루 한 번 돈다. 이보다 오래됐으면 멈춘 것
BACKUP_MAX_AGE = timedelta(hours=26)


def latest_backup(folder: str) -> tuple[datetime, int] | None:
    """(가장 최근 복사본 시각, 보관 개수). 없으면 None."""
    try:
        names = [n for n in os.listdir(folder) if n.startswith("bot-") and n.endswith(".db")]
        newest = max((os.path.getmtime(os.path.join(folder, n)) for n in names), default=None)
    except OSError:
        return None
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, timezone.utc), len(names)


def backup_status(now: datetime | None = None) -> tuple[bool, str]:
    """(백업이 최신인지, 상태 설명)"""
    found = latest_backup(config.BACKUP_DIR)
    if found is None:
        return False, "아직 없어요. 서버의 cron에 `scripts/backup_db.py`가 등록됐는지 확인하세요."
    made, count = found
    when = f"{made.astimezone(KST):%m-%d %H:%M}"
    if (now or datetime.now(timezone.utc)) - made > BACKUP_MAX_AGE:
        return False, f"**마지막 백업이 {when}이에요.** 하루 넘게 새 백업이 없어요."
    return True, f"마지막 {when} · {count}개 보관"


@dataclass
class Probe:
    ok: bool | None = None
    streak: int = 0
    alerted: bool = False
    checked_at: datetime | None = None
    error: str | None = None


class HealthMonitor:
    """바이낸스, 시세, 매수 자금을 지켜보고 장애와 회복을 한 번씩 알린다."""

    def __init__(
        self, db: Database, binance: BinanceClient, prices: PriceService, events: EventLog
    ) -> None:
        self._db = db
        self._binance = binance
        self._prices = prices
        self._events = events
        self.binance = Probe()
        self.prices = Probe()
        self.balances: dict[str, Decimal] = {}
        self.started_monotonic = time.monotonic()

    async def check(self) -> None:
        account = await self._probe(self.binance, "health.binance", "바이낸스 API", self._binance.get_account)
        if account is not None:
            self.balances = _free_balances(account)
            await self._check_funds()
        await self._probe(self.prices, "health.prices", "시세(업비트·바이낸스)", self._prices.get_usdt_krw)
        await self.daily_summary()

    async def _probe(self, probe: Probe, kind: str, label: str, call: Callable[[], Awaitable[Any]]) -> Any:
        probe.checked_at = datetime.now(timezone.utc)
        try:
            result = await call()
        except Exception as exc:
            probe.ok = False
            probe.streak += 1
            probe.error = f"{type(exc).__name__}: {exc}"[:300]
            if probe.streak >= FAIL_THRESHOLD and not probe.alerted:
                probe.alerted = True
                await self._events.record(
                    kind,
                    f"{label}에 {probe.streak}분째 연결하지 못하고 있어요. "
                    "그동안 시세 표시와 송금이 실패할 수 있어요.\n"
                    f"오류: {probe.error}",
                    level="error",
                    details=exception_details(exc),
                )
            return None
        if probe.alerted:
            await self._events.record(
                kind, f"{label} 연결이 회복됐어요 (약 {probe.streak}분 장애).", level="success"
            )
        probe.ok, probe.streak, probe.alerted, probe.error = True, 0, False, None
        return result

    async def _check_funds(self) -> None:
        threshold = Decimal(str(config.LOW_FUNDS_USDT))
        if threshold <= 0:
            return
        cash = self.balances.get("USDT", Decimal(0)) + self.balances.get("USD", Decimal(0))
        settings = await self._db.get_settings()
        low = settings.get("funds_low") == "1"
        if not low and cash < threshold:
            await self._db.set_setting("funds_low", "1")
            await self._events.record(
                "health.funds",
                f"매수 자금(USDT+USD)이 **{cash:,.2f}**로 기준 {threshold:g} 아래예요. "
                "계정에 없는 코인을 보내는 송금은 자동 매수를 못 해 실패할 수 있어요. "
                "바이낸스에 USDT나 USD를 넣어주세요.",
                level="warning",
            )
        elif low and cash >= threshold * FUNDS_RECOVERY_FACTOR:
            await self._db.set_setting("funds_low", None)
            await self._events.record(
                "health.funds", f"매수 자금이 **{cash:,.2f}**로 회복됐어요.", level="success"
            )

    async def daily_summary(self, now: datetime | None = None) -> bool:
        """하루 한 번, 09시(KST) 이후에 지난 24시간 요약을 올린다. 올렸으면 True."""
        now = (now or datetime.now(timezone.utc)).astimezone(KST)
        if now.hour < SUMMARY_HOUR_KST:
            return False
        today = now.date().isoformat()
        if (await self._db.get_settings()).get("summary_date") == today:
            return False
        await self._db.set_setting("summary_date", today)

        s = await self._db.activity_summary(24)
        counts = await self._db.ops_counts(config.DEPOSIT_TIMEOUT_MINUTES)
        cash = self.balances.get("USDT", Decimal(0)) + self.balances.get("USD", Decimal(0))
        backup_ok, backup_line = backup_status(now)
        manual = f"수동 충전 {s['manual_krw']:,}원" + (
            f" · 수동 차감 {s['debit_krw']:,}원" if s["debit_krw"] else ""
        )
        text = (
            f"**지난 24시간**\n"
            f"충전 {s['deposits']}건 {s['deposit_krw']:,}원 · 미확인 입금 처리 {s['resolved']}건 "
            f"{s['resolved_krw']:,}원 · {manual}\n"
            f"송금 {s['transfers']}건 {s['transfer_krw']:,}원 · 수수료 {s['fee_krw']:,}원 · "
            f"실패·환불 {s['failed_transfers']}건\n"
            f"경고 {s['warnings']}건 · 오류 {s['errors']}건\n\n"
            f"**지금**\n"
            f"유저 잔액 합계 {s['user_balances']:,}원 · 매수 자금 {cash:,.2f} USDT\n"
            f"처리 대기: 미확인 입금 {counts['unmatched']}건 · 확인 불가 송금 {counts['unknown_transfers']}건\n"
            f"DB 백업: {backup_line}"
        )
        attention = counts["unmatched"] or counts["unknown_transfers"] or s["errors"] or not backup_ok
        await self._events.record(
            "summary.daily", text, level="warning" if attention else "info"
        )
        pruned = await self._db.prune_events(EVENT_KEEP_DAYS)
        if pruned:
            log.info("Pruned %d routine events older than %d days", pruned, EVENT_KEEP_DAYS)
        return True


def _free_balances(account: dict) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = {}
    for item in account.get("balances", []):
        try:
            amount = Decimal(str(item.get("free", "0")))
        except InvalidOperation:
            continue
        if amount > 0:
            balances[item.get("asset", "?")] = amount
    return balances
