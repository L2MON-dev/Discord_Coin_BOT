from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from ..database import Database, NewUserStatus, Transaction
from ..errors import (
    GlobalLimitError, RejectedLimitError, ServicePausedError, TransferPendingError, UserFacingError,
)
from .binance_client import ORDER_NOT_FOUND_CODE, BinanceClient, BinanceError, SpotMarket
from .event_log import SKIP_EVENT, EventLog, NullEventLog, exception_details
from .price_service import CoinPrice, PriceService
from .. import config

log = logging.getLogger(__name__)

Notifier = Callable[..., Awaitable[None]]
# on_purchase: 바이낸스가 출금을 접수했을 때 (구매 알림)
# on_complete: 출금이 완료됐을 때 (구매자 역할). 접수된 출금도 실패할 수 있다
PurchaseHook = Callable[[Transaction], Awaitable[None]]

# 전송 직전 수령량이 이 비율 넘게 줄면 유저에게 다시 확인받는다
SLIPPAGE_TOLERANCE = Decimal("0.005")
# 업비트 김프가 USDT 김프보다 이만큼(%) 넘게 높은 코인은 팔지 않는다
# 업비트 입출금이 막혔거나 티커만 같은 다른 코인일 때 그렇게 된다
MAX_UPBIT_PREMIUM_PCT = 5.0
DEFAULT_WITHDRAW_MULTIPLE = Decimal("0.00000001")
# pending인 채로 이 시간이 지났으면 요청 도중에 끊긴 것
PENDING_GRACE_SECONDS = 180
# 이 시간이 지나도 바이낸스에 기록이 없으면 환불한다
UNKNOWN_REFUND_AFTER_SECONDS = 30 * 60
# 바이낸스가 접수한 걸 본 적 없는 상태. 기록이 없다는 이유로는 이 상태에서만 환불한다
# (submitted는 바이낸스가 실패라고 답했을 때만 환불)
UNCONFIRMED_STATUSES = ("pending", "unknown")
# 이 기간이 지나도 결과가 안 나오면 경고한다. 추적은 결과가 나올 때까지 계속한다
SUBMITTED_TRACK_DAYS = 7
# 바이낸스 출금 내역의 status 값
WITHDRAW_COMPLETED = 6
WITHDRAW_FAILED = {1, 3, 5}  # 취소, 거절, 실패
# 자동 매수 가격이 견적보다 이만큼 비싸면 사지 않는다
MAX_BUY_SLIPPAGE = Decimal("0.01")
DEFAULT_TAKER_FEE = Decimal("0.001")
# USD로 USDT를 살 때 받아들이는 최고가
MAX_USDT_USD_PRICE = Decimal("1.02")
# TXID 조회 최소 간격(초). 한 번에 UID weight 18000을 쓴다
TXID_LOOKUP_INTERVAL = 10
# 방금 산 코인은 체결은 바로 되지만 출금 쪽 잔고에는 조금 늦게 잡힌다
# 그 전에 출금하면 -4024(보유하지 않은 코인)나 -4026(잔고 부족)으로 거절된다
SETTLE_TIMEOUT_SECONDS = 20
SETTLE_POLL_SECONDS = 1
UNSETTLED_CODES = {-4024, -4026}
# 유저가 넣은 값 때문에 거절된 경우. 이것만 MAX_REJECTED_TRANSFERS에 센다
# 그 밖의 거절(API 키·IP, 한도, 출금 중지 등)은 운영자 쪽 문제다
USER_FAULT_CODES = {
    -4007,  # 주소 검증 실패
    -4008,  # 태그 검증 실패
    -4013,  # 받는 주소가 지원하지 않음
    -4033,  # 잘못된 주소
    -4034,  # 가짜로 의심되는 주소
}
WITHDRAW_RETRIES = 3
WITHDRAW_RETRY_DELAY = 3


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _average_fill(asks: list, quantity: Decimal) -> Decimal | None:
    """호가에서 quantity만큼 살 때의 평균가. 호가가 모자라면 None."""
    remaining, cost = quantity, Decimal(0)
    for price, size in asks:
        take = min(remaining, Decimal(str(size)))
        cost += take * Decimal(str(price))
        remaining -= take
        if remaining <= 0:
            return cost / quantity
    return None


def _free(account: dict, asset: str) -> Decimal:
    for balance in account.get("balances", []):
        if balance.get("asset") == asset:
            return Decimal(str(balance.get("free", "0")))
    return Decimal(0)


def _price_room(quote: "TransferQuote") -> Decimal:
    """전송 시점의 코인 값이 견적보다 얼마나 비싸도 되는지.

    MAX_BUY_SLIPPAGE까지지만 그 판매에서 버는 몫(수수료와 /김프)을 넘지는 않는다.
    코인 값이 고객이 낸 돈보다 더 들면 손해 보고 보내지 않고 환불한다.
    """
    earned = (1 + Decimal(str(quote.margin_pct)) / 100) / (1 - Decimal(str(quote.fee_rate))) - 1
    return max(Decimal(0), min(MAX_BUY_SLIPPAGE, earned))


def _taker_rate(account: dict) -> Decimal:
    rate = _positive_decimal((account.get("commissionRates") or {}).get("taker"))
    return rate if rate is not None and rate < Decimal("0.01") else DEFAULT_TAKER_FEE


def _no_stock() -> UserFacingError:
    # 코인도 없고 살 돈도 없는 경우. 관리자에게는 funds_low 알림이 간다
    return UserFacingError(
        "지금은 이 금액을 보낼 재고가 없어요.",
        title="송금 재고가 부족해요",
        hint="관리자에게 문의해 주세요 · 관리자에게도 자동으로 알렸어요",
    )


def _find_record(tx: Transaction, history: list[dict]) -> dict | None:
    """출금 내역에서 이 송금의 기록을 찾는다. 우리 주문 id를 먼저 보고, 없으면 바이낸스 id."""
    for record in history:
        if tx.withdraw_order_id and record.get("withdrawOrderId") == tx.withdraw_order_id:
            return record
    for record in history:
        if tx.binance_withdraw_id and str(record.get("id")) == tx.binance_withdraw_id:
            return record
    return None


@dataclass
class _BuyPlan:
    market: SpotMarket
    quantity: Decimal
    shortage: Decimal
    taker: Decimal
    # USDT가 모자랄 때 먼저 USD로 살 (USDTUSD 마켓, 수량)
    conversion: tuple[SpotMarket, Decimal] | None = None


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _matches(pattern: str | None, value: str) -> bool:
    """바이낸스가 준 정규식으로 검사한다. 정규식이 없거나 못 쓰는 것이면 통과."""
    if not pattern:
        return True
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        log.warning("Ignoring invalid Binance regex %r", pattern)
        return True


def accepts_memo(network_info: dict) -> bool:
    """바이낸스가 이 네트워크에서 메모(태그)를 받는지.

    메모 형식이나 필수 표시는 메모를 받는 네트워크(XRP 등)에만 있다. 다른 네트워크(BSC, SOL, TRX…)에
    메모를 넣으면 -4106으로 거절되는데, 그때는 이미 코인을 산 뒤다.
    """
    return bool(network_info.get("memo_required") or network_info.get("memo_regex"))


def _parse_utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _new_user_limit(discord_id: int) -> tuple[int, int, int]:
    """(신규 24시간 한도, 기간, 누적 금액). 관리자는 한도 없음."""
    if discord_id in config.ADMIN_USERS:
        return 0, 0, 0
    return config.NEW_USER_DAILY_LIMIT_KRW, config.NEW_USER_DAYS, config.NEW_USER_MIN_TOTAL_KRW


def _rejected_limit(discord_id: int) -> tuple[int, int]:
    """(거절 횟수, 분). 관리자는 제한 없음."""
    if discord_id in config.ADMIN_USERS:
        return 0, 0
    return config.MAX_REJECTED_TRANSFERS, config.REJECTED_WINDOW_MINUTES


async def _ignore(*_args: Any, **_kwargs: Any) -> None:
    return None


@dataclass
class TransferQuote:
    coin: str
    network: str
    network_name: str
    krw_amount: int
    fee_krw: int
    net_krw: int
    exchange_rate: float
    coin_amount: Decimal
    network_fee: Decimal
    receive_amount: Decimal
    usdt_price: float = 0.0  # 견적 기준 바이낸스 USDT 가격
    fee_rate: float = 0.0  # 견적 시점의 수수료율 (0.02 = 2%)
    premium_pct: float = 0.0  # 가격에 들어간 김프(%) = 시장 김프 + /김프
    # 사서 보내는 코인 가격에 들어간 바이낸스 거래 수수료 (0.001 = 0.1%). 재고에서 보내는 USDT는 0
    buy_fee_rate: float = 0.0
    margin_pct: float = 0.0  # premium_pct 중 운영자 몫(/김프)
    # 코인의 시장 가치(원): 드는 USDT × 업비트 USDT 시세. 결제 금액에서 이걸 빼면 이익 (/이익)
    market_krw: int | None = None
    memo_required: bool = False


class TransferService:
    def __init__(
        self,
        db: Database,
        binance: BinanceClient,
        prices: PriceService,
        *,
        notify_user: Notifier = _ignore,
        on_purchase: PurchaseHook = _ignore,
        on_complete: PurchaseHook = _ignore,
        events: EventLog | NullEventLog | None = None,
    ) -> None:
        self._db = db
        self._binance = binance
        self._prices = prices
        self._notify_user = notify_user
        self._on_purchase = on_purchase
        self._on_complete = on_complete
        self._announcing: set[asyncio.Task] = set()
        self._events = events or NullEventLog()
        # 매수와 출금은 한 번에 한 건만. 두 송금이 같은 코인을 세지 않게
        self._send_lock = asyncio.Lock()
        # 잔액을 차감하고 보내는 중인 송금(락 대기 포함). reconcile()이 이걸 환불하면 안 된다
        self._in_flight: set[int] = set()
        self._lookup_lock = asyncio.Lock()
        self._last_lookup = float("-inf")
        # 계정에서 마지막으로 읽은 거래 수수료율 (_plan_buy)
        self._taker = DEFAULT_TAKER_FEE

    async def _event(self, tx: Transaction, kind: str, message: str, *, level: str = "info",
                     details: dict | None = None, dedup: str | None = None) -> None:
        await self._events.record(
            kind, message, level=level, user_id=tx.discord_id, ref=("tx", tx.id), details=details,
            dedup=dedup,
        )

    async def check_limits(self, discord_id: int, krw_amount: int) -> None:
        new_limit, new_days, new_total = _new_user_limit(discord_id)
        try:
            await self._db.check_transfer_limits(
                discord_id,
                krw_amount,
                config.MAX_TRANSFER_KRW,
                config.DAILY_TRANSFER_LIMIT_KRW,
                new_limit, new_days,
                config.GLOBAL_DAILY_LIMIT_KRW,
                *_rejected_limit(discord_id),
                new_user_min_total=new_total,
            )
        except GlobalLimitError as exc:
            await self._global_limit_hit(discord_id, krw_amount, exc)
            raise
        except RejectedLimitError as exc:
            await self._reject_limited(discord_id, exc)
            raise

    async def new_user_status(self, discord_id: int) -> NewUserStatus:
        new_limit, new_days, new_total = _new_user_limit(discord_id)
        return await self._db.new_user_status(
            discord_id, new_limit, new_days, config.DAILY_TRANSFER_LIMIT_KRW, new_total
        )

    async def new_user_limit_to_change(self, discord_id: int) -> NewUserStatus:
        """전용 한도를 줄 수 있는 유저면 현재 상태를 돌려준다. 신규 한도가 걸리지 않는 유저(관리자, 신규 기간 끝, 한도 꺼짐)는 거절한다."""
        new_limit, new_days, _ = _new_user_limit(discord_id)
        status = await self.new_user_status(discord_id)
        if discord_id in config.ADMIN_USERS:
            reason, hint = "관리자에게는 신규 이용자 한도가 걸리지 않아요.", None
        elif not (new_limit and new_days):
            reason = "신규 이용자 한도를 쓰지 않도록 설정돼 있어서 모든 유저에게 일반 한도만 적용돼요."
            hint = "서버 설정(.env)의 NEW_USER_DAILY_LIMIT_KRW, NEW_USER_DAYS"
        elif not status.is_new:
            reason, hint = "이 유저는 신규 기간이 끝나서 이미 일반 한도로 보내고 있어요.", None
        else:
            return status
        raise UserFacingError(reason, title="바꿀 신규 한도가 없어요", tone="info", hint=hint)

    async def set_new_user_limit(self, discord_id: int, limit: int, admin_id: int) -> int | None:
        """확인을 마친 신규 유저에게 전용 24시간 한도를 준다. 1회·일·전체 한도는 그대로다. 이전 전용 한도를 돌려준다."""
        await self.new_user_limit_to_change(discord_id)
        new_limit = config.NEW_USER_DAILY_LIMIT_KRW
        old = await self._db.set_new_user_limit(discord_id, limit, admin_id)
        log.info("New-user limit set: user=%s limit=%s admin=%s", discord_id, limit, admin_id)
        daily = config.DAILY_TRANSFER_LIMIT_KRW
        await self._events.record(
            "admin.new_user_limit",
            f"<@{admin_id}>님이 이 유저의 신규 이용자 24시간 한도를 "
            f"**{old or new_limit:,}원 → {limit:,}원**으로 바꿨어요."
            + (f"\n일반 24시간 한도({daily:,}원) 이상이라 신규 한도 없이 일반 한도만 적용돼요."
               if daily and limit >= daily else ""),
            level="warning",
            user_id=discord_id,
        )
        return old

    async def clear_new_user_limit(self, discord_id: int, admin_id: int) -> int | None:
        old = await self._db.clear_new_user_limit(discord_id)
        if old is not None:
            log.info("New-user limit cleared: user=%s admin=%s", discord_id, admin_id)
            await self._events.record(
                "admin.new_user_limit",
                f"<@{admin_id}>님이 이 유저에게 따로 정했던 신규 이용자 24시간 한도"
                f"(**{old:,}원**)를 지웠어요. 기본 신규 한도가 다시 적용돼요.",
                level="success",
                user_id=discord_id,
            )
        return old

    async def _reject_limited(self, discord_id: int, exc: RejectedLimitError) -> None:
        await self._events.record(
            "transfer.reject_limited",
            f"최근 {exc.window_minutes}분 동안 바이낸스가 이 유저의 출금을 {exc.count}번 거절해서 "
            "송금을 잠시 막았어요.\n거절된 출금은 전액 환불되지만, 그때마다 봇이 코인을 사고 바이낸스를 "
            "호출하고 경고를 남겨요. 주소 입력 실수인지 일부러 반복하는 건지 '유저 보기'로 확인하세요.",
            level="warning",
            user_id=discord_id,
            dedup=f"transfer.reject_limited:{discord_id}",
        )

    async def _global_limit_hit(self, discord_id: int, krw_amount: int, exc: GlobalLimitError) -> None:
        await self._events.record(
            "transfer.global_limit",
            f"서비스 전체의 24시간 송금이 상한에 닿아 **{krw_amount:,}원** 송금 요청을 막았어요 · "
            f"최근 24시간 {exc.spent:,}원 / 상한 {exc.limit:,}원\n"
            "평소보다 송금이 많다면 관리자 계정이나 웹훅 키가 털리지 않았는지 먼저 확인하세요. "
            "정상적으로 늘어난 거라면 서버 설정의 GLOBAL_DAILY_LIMIT_KRW를 올리고 재시작하세요.",
            level="error",
            user_id=discord_id,
            dedup="transfer.global_limit",
        )

    async def fresh_network(self, coin: str, network: str) -> dict:
        """출금 조건을 캐시 없이 새로 읽는다. 전송 직전에는 메뉴에 쓰던 캐시를 믿지 않는다."""
        try:
            coins = await self._binance.get_withdrawable_coins(force_refresh=True)
        except Exception as exc:
            raise UserFacingError(
                "현재 출금 조건을 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                title="거래소에 연결하지 못했어요",
            ) from exc
        for item in coins:
            if item["coin"] == coin:
                for candidate in item["networks"]:
                    if candidate["network"] == network:
                        return dict(candidate)
        raise UserFacingError(
            "선택한 네트워크의 출금이 현재 중지되어 있어요. 다른 네트워크를 확인해 주세요.",
            title="출금할 수 없는 네트워크예요", tone="warning",
        )

    async def _buying_price(
        self, coin: str, price: CoinPrice, net_krw: int,
    ) -> tuple[Decimal, Decimal]:
        """USDT가 아닌 코인의 (USDT 가격, 거래 수수료율).

        이런 코인은 전송할 때 바이낸스에서 USDT로 사므로 사는 데 드는 값을 고객이 낸다:
        그 수량의 호가(최근 체결가보다 낮게는 치지 않음)와 거래 수수료. USDT 마켓이 없는 코인은
        재고로만 보내고 최근 체결가로 판다.
        """
        last = Decimal(str(price.binance_usdt))
        try:
            market = (await self._binance.get_usdt_markets()).get(coin)
            if market is None:
                return last, Decimal(0)
            asks = (await self._binance.get_order_book(market.symbol)).get("asks", [])
        except Exception as exc:
            log.warning("Order book for a quote failed: %s", coin, exc_info=True,
                        extra={SKIP_EVENT: True})
            raise UserFacingError(
                "바이낸스 시세를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                title="거래소에 연결하지 못했어요",
            ) from exc
        asked = _average_fill(asks, Decimal(net_krw) / Decimal(str(price.sale_krw)))
        if asked is None:
            raise UserFacingError(
                "거래소 호가가 부족해요. 잠시 후 다시 시도하거나 금액을 줄여 주세요.",
                title="지금은 시세를 확인할 수 없어요",
            )
        if asked > last * (1 + MAX_BUY_SLIPPAGE):
            # 이 수량에 호가가 얇거나 시세가 급변하는 중. 시세와 크게 다른 값은 받지도 내지도 않는다
            raise UserFacingError(
                "지금은 이 코인을 시세에 가깝게 살 수 없어요. 잠시 후 다시 시도하거나 금액을 줄여 주세요.",
                title="시세가 급하게 움직였어요",
            )
        return max(last, asked), self._taker

    async def _validate_quote_network(
        self, quote: TransferQuote, address: str, memo: str | None,
    ) -> None:
        network = await self.fresh_network(quote.coin, quote.network)
        self.validate_destination(network, address, memo)
        try:
            fee = Decimal(str(network["withdraw_fee"]))
            minimum = Decimal(str(network["withdraw_min"]))
            maximum = _positive_decimal(network.get("withdraw_max"))
            step = _positive_decimal(network.get("withdraw_multiple")) or DEFAULT_WITHDRAW_MULTIPLE
            valid = (
                fee.is_finite() and minimum.is_finite() and fee >= 0
                and fee == quote.network_fee and quote.coin_amount >= minimum
                and (maximum is None or quote.coin_amount <= maximum)
                and quote.coin_amount % step == 0 and quote.coin_amount > fee
                and (not network.get("memo_required") or quote.memo_required or bool(memo))
            )
        except (InvalidOperation, KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise UserFacingError(
                "네트워크 수수료나 출금 조건이 바뀌었어요. 새 견적을 확인한 뒤 다시 전송해 주세요.",
                title="출금 조건을 다시 확인해 주세요", tone="warning",
            )

    async def create_quote(
        self,
        coin: str,
        network_info: dict,
        krw_amount: int,
    ) -> TransferQuote:
        # 견적을 만드는 도중에 /수수료로 바뀔 수 있어 한 번만 읽는다
        fee_rate = Decimal(str(config.FEE_RATE))
        fee_krw = int(
            (Decimal(krw_amount) * fee_rate).to_integral_value(rounding=ROUND_DOWN)
        )
        net_krw = krw_amount - fee_krw

        price = await self._prices.get_coin_price(coin)
        upbit_premium = price.kimchi_premium
        above_market = price.premium_over_usdt
        if (upbit_premium is not None and upbit_premium > 0
                and above_market is not None and above_market > MAX_UPBIT_PREMIUM_PCT):
            raise UserFacingError(
                "이 코인만 업비트 시세가 비정상적으로 높아서 지금은 보낼 수 없어요.",
                title="시세가 평소와 달라요",
                tone="warning",
                facts=[
                    ("이 코인 김프", f"+{upbit_premium:.2f}%"),
                    ("USDT보다", f"+{above_market:.2f}%"),
                    ("허용 한도", f"{MAX_UPBIT_PREMIUM_PCT:g}%"),
                ],
                hint="업비트 입출금이 막혔거나 이름만 같은 다른 코인일 수 있어요 · 다른 코인을 골라 주세요",
            )
        price_krw = price.sale_krw
        premium = price.applied_premium
        usdt_price, buy_fee = Decimal(str(price.binance_usdt)), Decimal(0)
        if coin != "USDT":
            usdt_price, buy_fee = await self._buying_price(coin, price, net_krw)
            price_krw *= float(usdt_price / Decimal(str(price.binance_usdt)) / (1 - buy_fee))

        # withdrawIntegerMultiple의 배수가 아니면 바이낸스가 거절한다
        step = _positive_decimal(network_info.get("withdraw_multiple")) or DEFAULT_WITHDRAW_MULTIPLE
        raw_amount = Decimal(net_krw) / Decimal(str(price_krw))
        coin_amount = (raw_amount / step).to_integral_value(rounding=ROUND_DOWN) * step
        network_fee = Decimal(str(network_info["withdraw_fee"]))
        receive_amount = coin_amount - network_fee

        min_amount = Decimal(str(network_info["withdraw_min"]))
        if coin_amount < min_amount:
            raise UserFacingError(
                "금액을 늘려서 다시 입력해 주세요.",
                title="최소 전송 수량보다 적어요",
                tone="warning",
                facts=[
                    ("최소 수량", f"{_fmt(min_amount)} {coin}"),
                    ("계산된 수량", f"{_fmt(coin_amount)} {coin}"),
                ],
            )
        max_amount = _positive_decimal(network_info.get("withdraw_max"))
        if max_amount is not None and coin_amount > max_amount:
            raise UserFacingError(
                "금액을 줄여서 다시 입력해 주세요.",
                title="최대 전송 수량을 넘어요",
                tone="warning",
                facts=[
                    ("최대 수량", f"{_fmt(max_amount)} {coin}"),
                    ("계산된 수량", f"{_fmt(coin_amount)} {coin}"),
                ],
            )

        # 나간 USDT는 업비트에서 다시 채우므로 업비트 USDT 시세로 가치를 매긴다
        usdt_krw = price.upbit_usdt_krw or price.binance_krw / price.binance_usdt

        if receive_amount <= 0:
            raise UserFacingError(
                "네트워크 수수료를 빼면 받을 코인이 남지 않아요. 금액을 늘려 주세요.",
                title="받을 수 있는 수량이 없어요",
                tone="warning",
                facts=[("네트워크 수수료", f"{_fmt(network_fee)} {coin}")],
            )

        return TransferQuote(
            coin=coin,
            network=network_info["network"],
            network_name=network_info.get("name", ""),
            krw_amount=krw_amount,
            fee_krw=fee_krw,
            net_krw=net_krw,
            exchange_rate=price_krw,
            coin_amount=coin_amount,
            network_fee=network_fee,
            receive_amount=receive_amount,
            usdt_price=float(usdt_price),
            fee_rate=float(fee_rate),
            premium_pct=premium,
            buy_fee_rate=float(buy_fee),
            margin_pct=config.MIN_KIMCHI_PREMIUM_PCT,
            market_krw=int(
                (coin_amount * usdt_price / (1 - buy_fee) * Decimal(str(usdt_krw)))
                .to_integral_value(rounding=ROUND_CEILING)
            ),
            memo_required=bool(network_info.get("memo_required")),
        )

    @staticmethod
    def validate_destination(network_info: dict, address: str, memo: str | None) -> None:
        """주소와 메모를 바이낸스가 준 네트워크 형식으로 검사한다."""
        if not address or any(ch.isspace() for ch in address):
            raise UserFacingError(
                "공백 없이 받을 지갑 주소를 입력해 주세요.",
                title="지갑 주소를 확인해 주세요",
                tone="warning",
            )
        if not _matches(network_info.get("address_regex"), address):
            raise UserFacingError(
                f"{network_info['network']} 네트워크 주소가 아니에요. "
                "받는 지갑의 네트워크와 주소를 다시 확인해 주세요.",
                title="주소 형식이 맞지 않아요",
                tone="warning",
                hint="네트워크가 다르면 보낸 코인을 잃을 수 있어요",
            )
        if memo and not accepts_memo(network_info):
            raise UserFacingError(
                f"{network_info['network']} 네트워크는 메모(태그)를 받지 않아요. "
                "메모 없이 주소만 입력해 주세요.",
                title="메모(태그)를 비워 주세요",
                tone="warning",
                hint="받는 곳에서 메모를 꼭 넣으라고 했다면 네트워크가 맞는지 다시 확인해 주세요",
            )
        if memo and not _matches(network_info.get("memo_regex"), memo):
            raise UserFacingError(
                "받는 거래소가 알려 준 메모(태그)를 그대로 넣어 주세요.",
                title="메모(태그) 형식이 맞지 않아요",
                tone="warning",
            )

    async def execute(
        self,
        discord_id: int,
        quote: TransferQuote,
        address: str,
        memo: str | None = None,
    ) -> Transaction:
        if discord_id in config.BLOCKED_USERS:
            # 화면에서도 막지만, 진행 도중에 정지된 경우는 여기서 잡는다
            raise UserFacingError(
                "관리자가 이 계정의 봇 이용을 멈췄어요.",
                title="이용이 정지된 계정이에요",
                hint="잔액은 그대로예요 · 관리자에게 문의해 주세요",
            )
        order_id = f"cb{uuid.uuid4().hex}"
        new_limit, new_days, new_total = _new_user_limit(discord_id)
        max_rejected, rejected_window = _rejected_limit(discord_id)
        try:
            tx = await self._db.create_pending_transaction(
                discord_id=discord_id,
                coin=quote.coin,
                network=quote.network,
                address=address,
                memo=memo,
                coin_amount=_fmt(quote.coin_amount),
                krw_amount=quote.krw_amount,
                fee_krw=quote.fee_krw,
                exchange_rate=quote.exchange_rate,
                withdraw_order_id=order_id,
                max_per_transfer=config.MAX_TRANSFER_KRW,
                daily_limit=config.DAILY_TRANSFER_LIMIT_KRW,
                new_user_limit=new_limit,
                new_user_days=new_days,
                global_limit=config.GLOBAL_DAILY_LIMIT_KRW,
                max_rejected=max_rejected,
                rejected_window=rejected_window,
                new_user_min_total=new_total,
                market_krw=quote.market_krw,
            )
        except GlobalLimitError as exc:
            await self._global_limit_hit(discord_id, quote.krw_amount, exc)
            raise
        except RejectedLimitError as exc:
            await self._reject_limited(discord_id, exc)
            raise
        self._in_flight.add(tx.id)
        try:
            return await self._send(tx, quote, address, memo)
        finally:
            self._in_flight.discard(tx.id)

    async def _send(
        self, tx: Transaction, quote: TransferQuote, address: str, memo: str | None
    ) -> Transaction:
        """모자란 코인을 사고 출금한다. tx의 금액은 이미 잔액에서 차감돼 있다."""
        details = {"주소": address, "주문 ID": tx.withdraw_order_id}
        if memo:
            details["메모/태그"] = memo

        async with self._send_lock:
            # 락을 오래 기다렸을 수 있다. 그동안 reconcile()은 건드리지 않지만(_in_flight)
            # 다른 경로로 정리됐다면 지금 보내면 이중 지급이 된다
            current = await self._db.get_transaction(tx.id)
            if current is None or current.status != "pending":
                state = current.status if current else "없음"
                await self._state_conflict(
                    tx,
                    f"**{tx.coin_amount} {tx.coin}** ({tx.krw_amount:,}원) 송금이 차례를 기다리는 사이 "
                    f"상태가 바뀌어 있어서(`{state}`) 보내지 않았어요.\n"
                    "환불됐는지 '유저 보기'에서 잔액을 확인하세요.",
                )
                raise UserFacingError(
                    "송금을 기다리는 사이 처리 상태가 바뀌어서 보내지 않았어요.",
                    title="송금을 멈췄어요",
                    hint="내 정보에서 잔액을 확인해 주세요 · 관리자에게도 자동으로 알렸어요",
                )
            await self._refuse_if_paused(tx)
            await self._refuse_if_blocked(tx)
            await self._refuse_if_rejected_too_often(tx)
            # 실제로 보낼 송금만 요청 카드를 올린다
            await self._event(
                tx, "transfer.request",
                f"**{tx.coin_amount} {quote.coin}** ({quote.network}) 송금 요청 · "
                f"{quote.krw_amount:,}원 (수수료 {quote.fee_krw:,}원)",
                details=details,
            )
            try:
                await self._validate_quote_network(quote, address, memo)
                bought = await self._ensure_inventory(tx, quote)
            except UserFacingError as exc:
                # 산 게 없으니 바로 환불한다
                await self._db.fail_transaction(tx.id, f"buy: {exc}")
                await self._event(
                    tx, "transfer.reject",
                    f"코인을 준비하지 못해 송금을 멈추고 **{tx.krw_amount:,}원**을 환불했어요.\n"
                    f"이유: {exc}",
                    level="warning",
                    details=exception_details(exc.__cause__),
                )
                raise UserFacingError(
                    str(exc), title=exc.title, tone=exc.tone, facts=exc.facts,
                    hint="차감했던 잔액은 모두 환불했어요" + (f"\n{exc.hint}" if exc.hint else ""),
                ) from exc
            except asyncio.CancelledError:
                await asyncio.shield(self._mark_unknown(tx, "cancelled during buy"))
                raise
            except Exception as exc:
                await self._mark_unknown(tx, f"buy: {exc!r}")
                raise TransferPendingError() from exc
            if bought:
                await self._wait_until_withdrawable(tx, quote.coin, quote.coin_amount)
            result = await self._withdraw(tx, quote, address, memo, just_bought=bought)

        withdraw_id = str(result.get("id") or "") or None
        if not await self._db.set_transaction_status(
            tx.id, "submitted", binance_withdraw_id=withdraw_id
        ):
            current = await self._db.get_transaction(tx.id)
            await self._state_conflict(
                tx,
                f"바이낸스는 **{tx.coin_amount} {tx.coin}** ({tx.krw_amount:,}원) 출금을 접수했는데, 송금 기록이 "
                f"이미 `{current.status if current else '없음'}` 상태라 '출금 접수'로 바꾸지 못했어요.\n"
                "환불까지 됐다면 이중 지급이에요. '송금 상세'와 '유저 보기'로 확인하고, 필요하면 "
                "`/관리자 잔액차감`으로 바로잡으세요.",
            )
        tx.status = "submitted"
        tx.binance_withdraw_id = withdraw_id
        log.info(
            "Transfer submitted: tx=%s user=%s coin=%s amount=%s withdraw_id=%s",
            tx.id, tx.discord_id, quote.coin, tx.coin_amount, withdraw_id,
        )
        await self._event(
            tx, "transfer.submit",
            f"바이낸스가 **{tx.coin_amount} {quote.coin}** 출금을 접수했어요.",
            details={"출금 ID": withdraw_id or "-"},
        )
        self._in_background(self._on_purchase, tx, "Purchase announcement")
        return tx

    async def _refuse_if_paused(self, tx: Transaction, *, paused: bool | None = None) -> None:
        """차례를 기다리는 사이 서비스가 멈췄으면 보내지 않고 환불한다."""
        if paused is None:
            paused = await self._db.get_service_pause() is not None
        if not paused:
            return
        await self._db.fail_transaction(tx.id, "paused: the service was paused before sending",
                                        from_statuses=("pending",))
        await self._event(
            tx, "transfer.reject",
            f"충전·송금을 멈춘 뒤라서 **{tx.coin_amount} {tx.coin}** 송금을 보내지 않고 "
            f"**{tx.krw_amount:,}원**을 환불했어요.",
            level="warning",
        )
        exc = ServicePausedError()
        exc.hint = f"차감했던 잔액은 모두 환불했어요\n{exc.hint}"
        raise exc

    async def _refuse_if_blocked(self, tx: Transaction, *, blocked: bool | None = None) -> None:
        """차례를 기다리는 사이 유저가 정지됐으면 보내지 않고 환불한다.

        입금 취소 문자가 오면 그 유저를 바로 정지하는데(DepositService._stop_for_reversal),
        빠져나간 돈이 코인으로 나가지 않게 하려는 것이다.
        """
        if blocked is None:
            blocked = tx.discord_id in config.BLOCKED_USERS or await self._db.get_block(tx.discord_id) is not None
        if not blocked:
            return
        await self._db.fail_transaction(tx.id, "blocked: the user was stopped before sending",
                                        from_statuses=("pending",))
        await self._event(
            tx, "transfer.reject",
            f"이용정지된 유저라서 **{tx.coin_amount} {tx.coin}** 송금을 보내지 않고 "
            f"**{tx.krw_amount:,}원**을 환불했어요.",
            level="warning",
        )
        raise UserFacingError(
            "관리자가 이 계정의 봇 이용을 멈췄어요.",
            title="이용이 정지된 계정이에요",
            hint="차감했던 잔액은 모두 환불했어요 · 관리자에게 문의해 주세요",
        )

    async def _refuse_if_rejected_too_often(self, tx: Transaction) -> None:
        """거절 횟수가 MAX_REJECTED_TRANSFERS에 닿았으면 보내지 않고 환불한다.

        동시에 연 송금들은 만들 때는 모두 한도를 통과했다. 여기는 _send_lock 안이라
        앞선 송금이 어떻게 끝났는지를 보고 판단한다.
        """
        limit, window = _rejected_limit(tx.discord_id)
        if not (limit and window):
            return
        rejected = await self._db.count_rejected_transfers(tx.discord_id, window)
        if rejected < limit:
            return
        await self._db.fail_transaction(tx.id, f"refused: {rejected} rejected withdrawals in {window} min")
        exc = RejectedLimitError(rejected, limit, window)
        await self._reject_limited(tx.discord_id, exc)
        exc.hint = f"차감했던 잔액은 모두 환불했어요\n{exc.hint}"
        raise exc

    async def _state_conflict(self, tx: Transaction, message: str) -> None:
        log.error("Transfer changed while being sent: tx=%s", tx.id, extra={SKIP_EVENT: True})
        await self._event(tx, "transfer.state_conflict", message, level="error")

    def _in_background(self, hook: PurchaseHook, tx: Transaction, what: str) -> None:
        """on_purchase·on_complete를 백그라운드로 돌린다. 유저 응답이 이걸 기다리지 않는다."""
        async def run() -> None:
            try:
                await hook(tx)
            except Exception:
                log.warning("%s failed: tx=%s", what, tx.id, exc_info=True, extra={SKIP_EVENT: True})

        task = asyncio.create_task(run())
        self._announcing.add(task)
        task.add_done_callback(self._announcing.discard)

    async def _ensure_inventory(self, tx: Transaction, quote: TransferQuote) -> bool:
        """출금에 모자란 만큼을 USDT로 산다. 샀으면 True.

        UserFacingError면 쓸 만큼 사지 못한 것이라 환불해도 안전하다.
        다른 예외는 매수 결과를 알 수 없다는 뜻이다.
        """
        try:
            plan = await self._plan_buy(tx, quote)
        except UserFacingError:
            raise
        except Exception as exc:
            # 주문을 넣기 전에 실패한 것
            log.warning("Buy planning failed: tx=%s", tx.id, exc_info=True, extra={SKIP_EVENT: True})
            raise UserFacingError(
                "바이낸스 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                title="거래소에 연결하지 못했어요",
            ) from exc
        if plan is None:
            return False

        if plan.conversion is not None:
            usd_market, usdt_quantity = plan.conversion
            converted = await self._market_buy(
                tx, usd_market, usdt_quantity, f"{tx.withdraw_order_id}u", "USD→USDT 전환"
            )
            log.info(
                "Converted for transfer: tx=%s %s USDT for %s USD",
                tx.id, converted.get("executedQty"), converted.get("cummulativeQuoteQty"),
            )
            await self._event(
                tx, "transfer.convert",
                f"USD **{converted.get('cummulativeQuoteQty')}**로 USDT "
                f"**{converted.get('executedQty')}**를 샀어요.",
            )

        order_id = f"{tx.withdraw_order_id}b"
        order = await self._market_buy(
            tx, plan.market, plan.quantity, order_id, f"{quote.coin} 자동 매수"
        )

        bought = Decimal(str(order.get("executedQty", "0")))
        if "fills" in order:
            fee = sum(
                (Decimal(str(f["commission"])) for f in order["fills"]
                 if f.get("commissionAsset") == quote.coin),
                Decimal(0),
            )
        else:
            fee = bought * plan.taker
        if bought > 0:
            await self._db.set_transaction_buy(
                tx.id, order_id, str(order.get("cummulativeQuoteQty", "0"))
            )
        log.info(
            "Bought for transfer: tx=%s %s %s for %s USDT",
            tx.id, _fmt(bought), quote.coin, order.get("cummulativeQuoteQty"),
        )
        if bought - fee < plan.shortage:
            await self._event(
                tx, "transfer.partial_buy",
                f"{quote.coin} 자동 매수가 일부만 체결됐어요 "
                f"({_fmt(bought)} / {_fmt(plan.quantity)}). 산 코인은 계정에 남아 있어요.",
                level="error",
                details={"매수 주문": order_id},
            )
            raise UserFacingError("자동 매수가 일부만 체결돼서 송금을 멈췄어요.", title="코인을 다 사지 못했어요")
        # USDT는 USDTUSD 마켓에서 USD로 산다
        paid_in = plan.market.symbol.removeprefix(quote.coin) or "USDT"
        await self._event(
            tx, "transfer.buy",
            f"**{_fmt(bought)} {quote.coin}**를 **{order.get('cummulativeQuoteQty')} {paid_in}**에 샀어요.",
            details={"매수 주문": order_id},
        )
        return True

    async def _wait_until_withdrawable(self, tx: Transaction, coin: str, amount: Decimal) -> None:
        """방금 산 코인이 출금 쪽 잔고에 잡힐 때까지 기다린다 (SETTLE_TIMEOUT_SECONDS)."""
        started = time.monotonic()
        while True:
            try:
                available = await self._binance.get_withdrawable_amount(coin)
            except Exception:
                log.info("Withdrawable %s check failed: tx=%s", coin, tx.id, exc_info=True)
                available = None
            if available is not None and available >= amount:
                return
            if time.monotonic() - started >= SETTLE_TIMEOUT_SECONDS:
                log.warning(
                    "Bought %s not withdrawable after %ss: tx=%s have=%s need=%s",
                    coin, SETTLE_TIMEOUT_SECONDS, tx.id, available, amount,
                    extra={SKIP_EVENT: True},
                )
                return  # 그다음은 출금 재시도에 맡긴다
            await asyncio.sleep(SETTLE_POLL_SECONDS)

    async def _plan_buy(self, tx: Transaction, quote: TransferQuote) -> _BuyPlan | None:
        """얼마나 살지 정한다. 이미 충분하면 None."""
        account = await self._binance.get_account()
        self._taker = _taker_rate(account)  # 다음 견적부터 이 수수료율을 쓴다
        shortage = quote.coin_amount - _free(account, quote.coin)
        if shortage <= 0:
            await self._check_stock_price(quote)
            return None

        if quote.coin == "USDT":
            # USDT를 보내는 경우: USD로 바로 산다
            conversion = await self._plan_usd_conversion(account, shortage)
            if conversion is None:
                await self._event(
                    tx, "transfer.funds_low",
                    f"USDT 송금에 **{_fmt(shortage)} USDT**가 더 필요한데 "
                    f"USD가 {_free(account, 'USD'):.2f}뿐이라 살 수 없어요. 바이낸스에 자금을 넣어주세요.",
                    level="error",
                    # 자금이 모자란 동안 시도할 때마다 멘션이 가지 않게
                    dedup="transfer.funds_low",
                )
                raise _no_stock()
            usd_market, quantity = conversion
            return _BuyPlan(usd_market, quantity, shortage, _taker_rate(account))

        market = (await self._binance.get_usdt_markets()).get(quote.coin)
        if market is None:
            raise UserFacingError(
                f"{quote.coin} 재고가 부족하고, 바이낸스에서 USDT로 살 수도 없어요.",
                title="지금은 보낼 수 없는 코인이에요",
                hint="다른 코인을 고르거나 관리자에게 문의해 주세요",
            )

        # 거래 수수료가 산 코인에서 빠지므로 그만큼 더 산다
        taker = _taker_rate(account)
        quantity = max(
            _ceil_step(shortage / (1 - taker), market.step),
            _ceil_step(market.min_qty, market.step),
        )
        asks = (await self._binance.get_order_book(market.symbol)).get("asks", [])
        price = _average_fill(asks, quantity)
        if price is not None and market.min_notional and quantity * price < market.min_notional:
            # 최소 주문 금액에 못 미치면 조금 더 사고 남는 건 재고로 둔다
            quantity = _ceil_step(market.min_notional * Decimal("1.05") / price, market.step)
            price = _average_fill(asks, quantity)
        if price is None:
            raise UserFacingError(
                "거래소 호가가 부족해요. 잠시 후 다시 시도하거나 금액을 줄여 주세요.",
                title="지금은 코인을 살 수 없어요",
            )
        if market.max_qty and quantity > market.max_qty:
            raise UserFacingError(
                "금액을 줄여서 다시 시도해 주세요.",
                title="한 번에 살 수 있는 수량을 넘어요",
                tone="warning",
            )
        if price > Decimal(str(quote.usdt_price)) * (1 + _price_room(quote)):
            raise UserFacingError(
                "안전을 위해 송금을 멈췄어요. 잠시 후 다시 시도해 주세요.",
                title="시세가 급하게 움직였어요",
            )

        needed_usdt = quantity * price * (1 + MAX_BUY_SLIPPAGE)
        usdt = _free(account, "USDT")
        conversion = None
        if usdt < needed_usdt:
            conversion = await self._plan_usd_conversion(account, needed_usdt - usdt)
            if conversion is None:
                await self._event(
                    tx, "transfer.funds_low",
                    f"{quote.coin} 자동 매수에 약 **{needed_usdt:.2f} USDT**가 필요한데 "
                    f"USDT {usdt:.2f} · USD {_free(account, 'USD'):.2f}뿐이에요. "
                    "바이낸스에 자금을 넣어주세요.",
                    level="error",
                    dedup="transfer.funds_low",
                )
                raise _no_stock()
        return _BuyPlan(market, quantity, shortage, taker, conversion)

    async def _check_stock_price(self, quote: TransferQuote) -> None:
        """재고로 보내는 코인의 가격을 확인한다. 사지 않으니 매수 가격 검사를 거치지 않는다.

        견적은 최대 1분, 앞 송금을 기다렸다면 더 오래된 값일 수 있다. 지금 다시 채우는 값이
        견적보다 _price_room 넘게 비싸면 거절한다. 아직 보낸 게 없어 환불해도 안전하다.
        """
        if quote.coin == "USDT":
            return  # USDT는 원화로 값을 매긴다 (바이낸스 마켓 기준이 아님)
        market = (await self._binance.get_usdt_markets()).get(quote.coin)
        if market is None:
            raise UserFacingError(
                f"{quote.coin}의 바이낸스 시세를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                title="시세를 확인하지 못했어요",
            )
        asks = (await self._binance.get_order_book(market.symbol)).get("asks", [])
        price = _average_fill(asks, quote.coin_amount)
        if price is None:
            raise UserFacingError(
                "거래소 호가가 부족해요. 잠시 후 다시 시도하거나 금액을 줄여 주세요.",
                title="지금은 시세를 확인할 수 없어요",
            )
        if price > Decimal(str(quote.usdt_price)) * (1 + _price_room(quote)):
            raise UserFacingError(
                "안전을 위해 송금을 멈췄어요. 잠시 후 다시 시도해 주세요.",
                title="시세가 급하게 움직였어요",
            )

    async def _plan_usd_conversion(
        self, account: dict, missing_usdt: Decimal
    ) -> tuple[SpotMarket, Decimal] | None:
        """계정의 USD로 살 (USDTUSD 마켓, USDT 수량). USD로 감당이 안 되면 None."""
        usd = _free(account, "USD")
        market = await self._binance.get_market("USDTUSD") if usd > 0 else None
        if market is None:
            return None

        quantity = max(
            _ceil_step(missing_usdt / (1 - _taker_rate(account)), market.step),
            _ceil_step(market.min_qty, market.step),
        )
        asks = (await self._binance.get_order_book(market.symbol)).get("asks", [])
        price = _average_fill(asks, quantity)
        if price is not None and market.min_notional and quantity * price < market.min_notional:
            quantity = _ceil_step(market.min_notional * Decimal("1.05") / price, market.step)
            price = _average_fill(asks, quantity)
        # USDT는 1달러 근처여야 한다. 아니면 마켓이 이상한 것
        if price is None or not Decimal("0.98") <= price <= MAX_USDT_USD_PRICE:
            return None
        if market.max_qty and quantity > market.max_qty:
            return None
        if usd < quantity * price * (1 + MAX_BUY_SLIPPAGE):
            return None
        return market, quantity

    async def _market_buy(
        self, tx: Transaction, market: SpotMarket, quantity: Decimal, order_id: str, label: str
    ) -> dict:
        """시장가 매수. UserFacingError면 아무것도 사지 않은 것."""
        try:
            return await self._binance.market_buy(market.symbol, _fmt(quantity), order_id)
        except BinanceError as exc:
            if not exc.is_rejection:
                return await self._find_order(market.symbol, order_id)
            log.warning("%s rejected: tx=%s code=%s msg=%s", label, tx.id, exc.code, exc)
            await self._event(
                tx, "transfer.buy_rejected",
                f"바이낸스가 {label} 주문을 거절했어요 (코드 {exc.code}).\n{exc}",
                level="error",
                details={"주문 ID": order_id, "종목": market.symbol, "수량": _fmt(quantity)},
            )
            raise UserFacingError(
                "바이낸스가 매수 주문을 거절했어요.",
                title="코인을 사지 못했어요",
                facts=[("거래소 코드", str(exc.code))],
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self._find_order(market.symbol, order_id)

    async def _find_order(self, symbol: str, client_order_id: str) -> dict:
        """응답을 못 받은 매수 주문을 조회한다. 주문이 없으면 산 게 없는 것."""
        try:
            return await self._binance.get_order(symbol, client_order_id)
        except BinanceError as exc:
            if exc.code == ORDER_NOT_FOUND_CODE:
                raise UserFacingError("매수 주문이 처리되지 않았어요.", title="코인을 사지 못했어요") from exc
            raise

    async def _withdraw(
        self,
        tx: Transaction,
        quote: TransferQuote,
        address: str,
        memo: str | None,
        *,
        just_bought: bool = False,
    ) -> dict:
        retries = WITHDRAW_RETRIES if just_bought else 0
        attempt = 0
        while True:
            # 아직 시도 전이거나 직전 시도가 확실히 거절된 상태라 여기서는 환불해도 안전하다
            # 매수와 정산을 기다린 뒤이므로 출금 조건을 다시 확인한다
            try:
                await self._validate_quote_network(quote, address, memo)
            except UserFacingError as exc:
                await self._refund(tx, str(exc))
                exc.hint = "차감했던 잔액은 모두 환불했어요"
                raise

            async def before_send() -> None:
                state, paused, blocked = await self._db.withdrawal_gate(tx.id, tx.discord_id)
                if state != "pending":
                    await self._state_conflict(tx, "출금 직전 송금 상태가 바뀌어서 보내지 않았어요.")
                    raise UserFacingError(
                        "송금 기록이 바뀌어 출금을 멈췄어요. 내 정보에서 잔액을 확인해 주세요.",
                        title="송금을 멈췄어요",
                    )
                if paused:
                    await self._refuse_if_paused(tx, paused=True)
                if blocked or tx.discord_id in config.BLOCKED_USERS:
                    await self._refuse_if_blocked(tx, blocked=True)

            await before_send()
            try:
                result = await self._binance.withdraw(
                    coin=quote.coin,
                    network=quote.network,
                    address=address,
                    amount=_fmt(quote.coin_amount),
                    address_tag=memo,
                    withdraw_order_id=tx.withdraw_order_id,
                    before_send=before_send,
                )
                if not isinstance(result, dict) or not result.get("id"):
                    raise ValueError("Withdrawal accepted without a usable response id")
                return result
            except UserFacingError:
                raise  # HTTP 요청 직전의 마지막 확인에서 멈춘 경우
            except BinanceError as exc:
                if exc.is_rejection and exc.code in UNSETTLED_CODES and attempt < retries:
                    # 거절된 요청은 출금을 만들지 않으므로 같은 주문 id로 다시 시도한다
                    attempt += 1
                    log.info("Withdrawal not settled yet (code %s), retry %d: tx=%s",
                             exc.code, attempt, tx.id)
                    await asyncio.sleep(WITHDRAW_RETRY_DELAY)
                    continue
                if not exc.is_rejection:
                    await self._mark_unknown(tx, f"HTTP {exc.status}: {exc}")
                    raise TransferPendingError() from exc
                await self._reject(tx, quote, address, exc, attempts=attempt + 1,
                                   just_bought=just_bought)
                raise UserFacingError(
                    "차감했던 잔액은 모두 환불했어요.",
                    title="거래소가 출금을 거절했어요",
                    facts=[("거래소 코드", str(exc.code))] if exc.code is not None else (),
                    hint="주소와 네트워크를 확인한 뒤 다시 시도해 주세요",
                ) from exc
            except asyncio.CancelledError:
                # 요청이 바이낸스에 닿았는지 알 수 없다
                await asyncio.shield(self._mark_unknown(tx, "cancelled"))
                raise
            except Exception as exc:
                # 타임아웃이나 끊긴 연결은 결과를 모른다. 여기서 환불하면 이중 지급이 될 수 있다
                await self._mark_unknown(tx, repr(exc))
                raise TransferPendingError() from exc

    async def _reject(
        self, tx: Transaction, quote: TransferQuote, address: str, exc: BinanceError,
        *, attempts: int, just_bought: bool,
    ) -> None:
        log.warning("Withdrawal rejected: tx=%s code=%s msg=%s", tx.id, exc.code, exc)
        # 'rejected (…'로 시작하는 것만 거절 횟수에 센다 (database._count_rejected)
        # 거래소 쪽 문제로 유저가 막히거나 악용자처럼 보이면 안 된다
        if exc.code in USER_FAULT_CODES:
            kind = "rejected"
        elif exc.code in UNSETTLED_CODES:
            kind = "unsettled"
        else:
            kind = "exchange"
        await self._db.fail_transaction(tx.id, f"{kind} ({exc.code}): {exc}")
        hint = ""
        if just_bought and exc.code in UNSETTLED_CODES:
            hint = (
                f"\n자동으로 산 {quote.coin}가 출금 가능 잔고에 잡히지 않았어요 ({attempts}번 시도). "
                "산 코인은 계정에 남아 있어요. 바이낸스 앱에서 입금·구매 후 출금 대기 기간 같은 "
                "출금 제한이 걸려 있는지 확인하세요."
            )
        await self._event(
            tx, "transfer.reject",
            f"바이낸스가 출금을 거절해 **{tx.krw_amount:,}원**을 환불했어요 (코드 {exc.code}).\n{exc}{hint}",
            level="warning",
            details={"주소": address, "네트워크": quote.network, "시도 횟수": str(attempts)},
        )

    async def _mark_unknown(self, tx: Transaction, reason: str) -> None:
        log.error(
            "Withdrawal outcome unknown: tx=%s order=%s reason=%s",
            tx.id, tx.withdraw_order_id, reason, extra={SKIP_EVENT: True},
        )
        await self._db.set_transaction_status(tx.id, "unknown", error=reason[:500])
        await self._event(
            tx, "transfer.unknown",
            f"**{tx.coin_amount} {tx.coin}** ({tx.krw_amount:,}원) 송금 결과를 확인하지 못했어요. "
            "1분마다 바이낸스 기록을 확인하고, 30분 이후 미접수가 확인되면 자동 환불해요. "
            "조회가 불완전하거나 오래 지연된 송금은 관리자 확인이 필요해요.",
            level="error",
            details={"원인": reason[:500], "주문 ID": tx.withdraw_order_id},
        )

    async def reconcile(self) -> None:
        """결과가 확정되지 않은 출금을 바이낸스 기록과 대조해 정리한다.

        이 프로세스가 보내는 중인 송금은 execute()가 처리하므로 건드리지 않는다.
        조회 기간은 실제 신청 시각에 맞춘다 (90일이 넘은 것도 포함).
        """
        open_txs = [
            tx for tx in await self._db.get_open_transactions(
                PENDING_GRACE_SECONDS, SUBMITTED_TRACK_DAYS
            )
            if tx.id not in self._in_flight
        ]
        if not open_txs:
            return

        now = datetime.now(timezone.utc)
        remaining = sorted(open_txs, key=lambda tx: tx.created_at)
        while remaining:
            start = _parse_utc(remaining[0].created_at) - timedelta(minutes=10)
            end = max(start, min(now, start + timedelta(days=89)))
            group = [remaining[0]] + [tx for tx in remaining[1:] if _parse_utc(tx.created_at) <= end]
            remaining = remaining[len(group):]
            try:
                history = await self._binance.get_withdraw_history(_ms(start), _ms(end))
            except Exception:
                # 오래된 구간 조회가 실패해도 최근 출금 확인은 계속한다
                log.exception("Withdrawal reconciliation failed for %s through %s", start, end)
                continue
            for tx in group:
                try:
                    await self._settle(tx, _find_record(tx, history), now)
                except Exception:
                    log.exception("Withdrawal reconciliation failed: tx=%s", tx.id)

    async def lookup_txid(self, tx: Transaction) -> Transaction | None:
        """출금 한 건의 TXID를 바이낸스에서 받아 저장한다.

        내역 조회가 UID weight 18000을 쓰므로 TXID_LOOKUP_INTERVAL초 간격을 둔다.
        None은 아직 이르니 잠시 뒤에 다시 하라는 뜻이다.
        """
        if (tx.status == "failed" or (tx.status == "completed" and tx.txid)
                or not (tx.withdraw_order_id or tx.binance_withdraw_id)):
            return tx
        if tx.id in self._in_flight:
            return tx  # 보내는 중. 결과는 execute()가 기록한다
        async with self._lookup_lock:
            if time.monotonic() - self._last_lookup < TXID_LOOKUP_INTERVAL:
                return None
            self._last_lookup = time.monotonic()
            now = datetime.now(timezone.utc)
            start = _parse_utc(tx.created_at) - timedelta(minutes=10)
            end = max(start, min(now, start + timedelta(days=89)))
            history = await self._binance.get_withdraw_history(_ms(start), _ms(end))
        record = _find_record(tx, history)
        if record is not None:
            await self._settle(tx, record, now)
        return await self._db.get_transaction(tx.id) or tx

    async def _settle(self, tx: Transaction, record: dict | None, now: datetime) -> None:
        age = (now - _parse_utc(tx.created_at)).total_seconds()
        if record is None:
            if age < UNKNOWN_REFUND_AFTER_SECONDS:
                return
            # 실제로 나간 출금을 환불하면 이중 지급이다. 내역 조회는 페이지나 시각 차이로 빠뜨릴 수 있으니
            # 포기하기 전에 주문 id로 한 번 더 찾는다
            if tx.withdraw_order_id:
                start = _parse_utc(tx.created_at) - timedelta(minutes=10)
                record = await self._binance.find_withdrawal(
                    tx.withdraw_order_id, start_time=_ms(start),
                    end_time=_ms(min(now, start + timedelta(days=6))),
                )
            if record is None:
                if (tx.status not in UNCONFIRMED_STATUSES or tx.binance_withdraw_id or tx.txid
                        or not tx.withdraw_order_id or age >= SUBMITTED_TRACK_DAYS * 86400):
                    await self._alert_unresolved(tx)
                    return
                # 아직 미확정일 때만 환불한다. 그사이 TXID 조회가 출금을 찾아 submitted로 바꿨을 수 있다
                await self._refund(tx, "바이낸스에 출금 기록 없음", unconfirmed_only=True)
                return

        txid = str(record.get("txId") or "").strip()
        if txid:
            await self._db.set_transaction_txid(tx.id, txid)
        try:
            status = int(record.get("status"))
        except (TypeError, ValueError):
            return
        withdraw_id = str(record.get("id") or "") or None
        if status in WITHDRAW_FAILED:
            await self._refund(tx, f"바이낸스 출금 상태 {status}")
            return
        if status != WITHDRAW_COMPLETED and age >= SUBMITTED_TRACK_DAYS * 86400:
            await self._alert_unresolved(tx)
        # 뒤늦게 접수가 확인된 경우. reconcile과 TXID 조회가 같은 송금을 동시에 정리할 수 있어
        # pending/unknown에서 빼낸 쪽만 알린다
        accepted = tx.status in ("pending", "unknown") and await self._db.set_transaction_status(
            tx.id, "submitted", binance_withdraw_id=withdraw_id
        )
        if status == WITHDRAW_COMPLETED and await self._db.set_transaction_status(
            tx.id, "completed", binance_withdraw_id=withdraw_id
        ):
            await self._event(
                tx, "transfer.complete",
                f"**{tx.coin_amount} {tx.coin}** 출금이 블록체인에서 완료됐어요.",
                level="success",
                details={"TXID": txid or "-"},
            )
            # 완료돼야 구매로 친다 (구매자 역할)
            self._in_background(self._on_complete, tx, "Completion hook")
        if accepted:
            # 접수가 확인된 지금이 구매 시점
            self._in_background(self._on_purchase, tx, "Purchase announcement")
            await self._notify_user(
                tx.discord_id,
                message="확인이 늦어졌던 송금이 바이낸스에 정상 접수됐어요.",
                title="출금이 접수됐어요",
                tone="success",
                facts=[("송금 번호", f"#{tx.id}"), ("보낸 수량", f"`{tx.coin_amount}` {tx.coin}")],
                hint="TXID는 패널의 내 정보에서 볼 수 있어요",
            )

    async def _alert_unresolved(self, tx: Transaction) -> None:
        if await self._db.find_event("transfer.overdue", "tx", tx.id) is not None:
            return
        await self._event(
            tx, "transfer.overdue",
            f"송금 #{tx.id}의 최종 결과를 확인하지 못했어요. 바이낸스 출금 내역을 확인해 주세요.\n"
            "완료·실패가 확인될 때까지 계속 추적하며, 접수된 출금은 기록이 없다는 이유로 환불하지 않아요.",
            level="error", details={"주문 ID": tx.withdraw_order_id, "출금 ID": tx.binance_withdraw_id},
        )

    async def _refund(self, tx: Transaction, reason: str, *, unconfirmed_only: bool = False) -> None:
        if unconfirmed_only:
            refunded = await self._db.fail_transaction(tx.id, reason, from_statuses=UNCONFIRMED_STATUSES)
        else:
            refunded = await self._db.fail_transaction(tx.id, reason)
        if refunded is None:
            return
        log.warning("Withdrawal refunded: tx=%s reason=%s", tx.id, reason)
        await self._event(
            tx, "transfer.refund",
            f"**{tx.coin_amount} {tx.coin}** 송금이 처리되지 않아 **{tx.krw_amount:,}원**을 환불했어요.\n"
            f"이유: {reason}",
            level="warning",
        )
        await self._notify_user(
            tx.discord_id,
            message="송금이 처리되지 않아 차감했던 금액을 잔액으로 돌려드렸어요.",
            title="송금 금액을 환불했어요",
            tone="warning",
            facts=[
                ("송금 번호", f"#{tx.id}"),
                ("코인", f"`{tx.coin_amount}` {tx.coin}"),
                ("환불 금액", f"**{tx.krw_amount:,}원**"),
            ],
            hint="다시 보내려면 패널에서 송금하기를 눌러 주세요",
        )
