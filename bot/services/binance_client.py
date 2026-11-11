from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable
from urllib.parse import urlencode

import aiohttp

from .. import config

log = logging.getLogger(__name__)

TIMESTAMP_ERROR_CODE = -1021
ORDER_NOT_FOUND_CODE = -2013
# 바이낸스가 실제로 처리했는지 알 수 없는 응답
# -1000 처리 중 알 수 없는 오류, -1006/-1007 실행 상태 불명
UNKNOWN_OUTCOME_CODES = frozenset({-1000, -1006, -1007})
COIN_CONFIG_TTL_SECONDS = 60
MARKETS_TTL_SECONDS = 60 * 60
HISTORY_PAGE_SIZE = 1000
HISTORY_MAX_PAGES = 5


def free_balance(coin: dict) -> float:
    try:
        return float(coin.get("free", 0))
    except (TypeError, ValueError):
        return 0.0


def _decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


@dataclass(frozen=True)
class SpotMarket:
    symbol: str
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal | None
    min_notional: Decimal | None


def _parse_market(symbol: dict) -> SpotMarket:
    filters = {f.get("filterType"): f for f in symbol.get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", {})
    # MARKET_LOT_SIZE가 0이면 LOT_SIZE와 같다는 뜻
    step = _decimal(market_lot.get("stepSize")) or _decimal(lot.get("stepSize")) or Decimal("0.00000001")
    min_qty = max(
        _decimal(lot.get("minQty")) or Decimal(0),
        _decimal(market_lot.get("minQty")) or Decimal(0),
    )
    max_qty = _decimal(market_lot.get("maxQty")) or _decimal(lot.get("maxQty"))

    min_notional = None
    if "NOTIONAL" in filters and filters["NOTIONAL"].get("applyMinToMarket", True):
        min_notional = _decimal(filters["NOTIONAL"].get("minNotional"))
    elif "MIN_NOTIONAL" in filters and filters["MIN_NOTIONAL"].get("applyToMarket", True):
        min_notional = _decimal(filters["MIN_NOTIONAL"].get("minNotional"))
    return SpotMarket(symbol["symbol"], step, min_qty, max_qty, min_notional)


class BinanceError(Exception):
    def __init__(self, message: str, *, status: int, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def is_rejection(self) -> bool:
        """바이낸스가 요청을 실행하지 않은 게 확실한지.

        5xx, 타임아웃(408), UNKNOWN_OUTCOME_CODES는 실행 여부를 알 수 없는 응답이다.
        """
        return (
            self.status < 500
            and self.status != 408
            and self.code not in UNKNOWN_OUTCOME_CODES
        )


class BinanceClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._time_offset_ms: int | None = None
        self._coins: list[dict] | None = None
        self._coins_time = 0.0
        self._coins_lock = asyncio.Lock()
        self._markets: dict[str, SpotMarket] | None = None
        self._symbols: dict[str, SpotMarket] = {}
        self._markets_time = 0.0
        self._markets_lock = asyncio.Lock()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            config.BINANCE_API_SECRET.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def sync_time(self) -> None:
        before = time.time() * 1000
        async with self._get_session().get(
            f"{config.BINANCE_BASE_URL}/api/v3/time"
        ) as res:
            server_time = (await res.json(content_type=None))["serverTime"]
        after = time.time() * 1000
        self._time_offset_ms = int(server_time - (before + after) / 2)

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + (self._time_offset_ms or 0)

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        *,
        signed: bool = True,
        retry: bool = True,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> dict | list:
        session = self._get_session()

        if signed:
            if self._time_offset_ms is None:
                await self.sync_time()
            all_params = {**(params or {}), "timestamp": self._timestamp()}
            query_string = urlencode(all_params)
            url = (
                f"{config.BINANCE_BASE_URL}{endpoint}"
                f"?{query_string}&signature={self._sign(query_string)}"
            )
            headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
        else:
            url = f"{config.BINANCE_BASE_URL}{endpoint}"
            if params:
                url += f"?{urlencode(params)}"
            headers = {}

        if before_send is not None:
            await before_send()
        async with session.request(method, url, headers=headers) as res:
            status = res.status
            try:
                data = await res.json(content_type=None)
            except ValueError:
                data = {}

        if status >= 400:
            if not isinstance(data, dict):
                data = {}
            code = data.get("code")
            if retry and code == TIMESTAMP_ERROR_CODE:
                await self.sync_time()
                return await self._request(
                    method, endpoint, params, signed=signed, retry=False, before_send=before_send,
                )
            raise BinanceError(
                data.get("msg") or f"Binance API error ({status})",
                status=status,
                code=code,
            )

        return data

    async def get_all_ticker_prices(self) -> dict[str, float]:
        data = await self._request(
            "GET", "/api/v3/ticker/price", signed=False
        )
        return {item["symbol"]: float(item["price"]) for item in data}

    async def _load_markets(self) -> None:
        async with self._markets_lock:
            now = time.monotonic()
            if self._markets is not None and now - self._markets_time < MARKETS_TTL_SECONDS:
                return
            data = await self._request("GET", "/api/v3/exchangeInfo", signed=False)
            tradable = [
                s for s in data.get("symbols", [])
                if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)
            ]
            self._symbols = {s["symbol"]: _parse_market(s) for s in tradable}
            self._markets = {
                s["baseAsset"]: self._symbols[s["symbol"]]
                for s in tradable
                if s.get("quoteAsset") == "USDT"
            }
            self._markets_time = now

    async def get_usdt_markets(self) -> dict[str, SpotMarket]:
        """USDT로 살 수 있는 코인들. 캐시됨 (weight 20)."""
        await self._load_markets()
        return self._markets

    async def get_market(self, symbol: str) -> SpotMarket | None:
        """현물 심볼의 거래 규칙 (예: USDTUSD). 거래할 수 없으면 None."""
        await self._load_markets()
        return self._symbols.get(symbol)

    async def get_order_book(self, symbol: str, limit: int = 100) -> dict:
        return await self._request(
            "GET", "/api/v3/depth", {"symbol": symbol, "limit": limit}, signed=False
        )

    async def get_coin_config(self) -> list[dict]:
        return await self._request("GET", "/sapi/v1/capital/config/getall")

    async def get_withdrawable_amount(self, coin: str) -> Decimal:
        """출금 쪽에서 보이는 잔고. 캐시하지 않는다 (weight 10)."""
        for item in await self.get_coin_config():
            if item.get("coin") == coin:
                return _decimal(item.get("free")) or Decimal(0)
        return Decimal(0)

    async def get_account(self) -> dict:
        """현물 잔고와 수수료율 (weight 20)."""
        return await self._request(
            "GET", "/api/v3/account", {"omitZeroBalances": "true"}
        )

    async def market_buy(self, symbol: str, quantity: str, client_order_id: str) -> dict:
        # API 키에 현물 거래 권한이 있어야 한다
        return await self._request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quantity": quantity,
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
        )

    async def get_order(self, symbol: str, client_order_id: str) -> dict:
        return await self._request(
            "GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}
        )

    async def get_withdrawable_coins(self, *, force_refresh: bool = False) -> list[dict]:
        """출금이 열린 네트워크가 있는 코인들. 잠깐 캐시한다 (weight 10)."""
        async with self._coins_lock:
            now = time.monotonic()
            if force_refresh or self._coins is None or now - self._coins_time >= COIN_CONFIG_TTL_SECONDS:
                self._coins = self._parse_withdrawable(await self.get_coin_config())
                self._coins_time = now
            return list(self._coins)

    @staticmethod
    def _parse_withdrawable(coin_config: list[dict]) -> list[dict]:
        coins = []
        for coin in coin_config:
            networks = [
                {
                    "network": net["network"],
                    "name": net.get("name", ""),
                    "withdraw_fee": net["withdrawFee"],
                    "withdraw_min": net["withdrawMin"],
                    "withdraw_max": net.get("withdrawMax"),
                    "withdraw_multiple": net.get("withdrawIntegerMultiple"),
                    "address_regex": net.get("addressRegex"),
                    "memo_regex": net.get("memoRegex"),
                    # sameAddress는 폐기 예정이라 withdrawTag를 먼저 본다
                    "memo_required": bool(
                        net.get("withdrawTag", net.get("sameAddress", False))
                    ),
                }
                for net in coin.get("networkList", [])
                if net.get("withdrawEnable")
            ]
            if networks:
                coins.append({
                    "coin": coin["coin"],
                    "name": coin.get("name", ""),
                    "free": coin.get("free", "0"),
                    "networks": networks,
                })
        return coins

    async def withdraw(
        self,
        coin: str,
        network: str,
        address: str,
        amount: str,
        address_tag: str | None = None,
        withdraw_order_id: str | None = None,
        *, before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> dict:
        params: dict = {
            "coin": coin,
            "network": network,
            "address": address,
            "amount": amount,
            "walletType": 0,  # 재고 확인과 자동 매수가 현물 지갑 기준이다
        }
        if address_tag:
            params["addressTag"] = address_tag
        if withdraw_order_id:
            # 우리가 정한 id. 결과를 모를 때 이걸로 다시 찾는다
            params["withdrawOrderId"] = withdraw_order_id
        return await self._request(
            "POST", "/sapi/v1/capital/withdraw/apply", params, before_send=before_send,
        )

    async def find_withdrawal(
        self, withdraw_order_id: str, *, start_time: int | None = None, end_time: int | None = None,
    ) -> dict | None:
        """주문 id로 출금을 찾는다. 기간을 주면 7일 미만이어야 하고, 안 주면 바이낸스가 최근 7일만 찾는다."""
        params: dict = {"withdrawOrderId": withdraw_order_id}
        if start_time is not None and end_time is not None:
            if not 0 <= end_time - start_time < 7 * 86400 * 1000:
                raise ValueError("Withdrawal order lookup window must be shorter than 7 days")
            params.update(startTime=start_time, endTime=end_time)
        records = await self._request(
            "GET", "/sapi/v1/capital/withdraw/history", params,
        )
        if not isinstance(records, list):
            raise ValueError("Invalid withdrawal history response")
        for record in records or []:
            if record.get("withdrawOrderId") == withdraw_order_id:
                return record
        return None

    async def get_withdraw_history(self, start_time: int, end_time: int) -> list[dict]:
        """기간(ms, 90일 미만) 안에 신청된 출금들. 페이지당 UID weight 18000이라 자주 부르면 안 된다."""
        records: list[dict] = []
        if not 0 <= end_time - start_time < 90 * 86400 * 1000:
            raise ValueError("Withdrawal history window must be shorter than 90 days")
        for page in range(HISTORY_MAX_PAGES):
            batch = await self._request(
                "GET",
                "/sapi/v1/capital/withdraw/history",
                {
                    "startTime": start_time,
                    "endTime": end_time,
                    "offset": page * HISTORY_PAGE_SIZE,
                    "limit": HISTORY_PAGE_SIZE,
                },
            )
            if not isinstance(batch, list):
                raise ValueError("Invalid withdrawal history response")
            records.extend(batch)
            if len(batch) < HISTORY_PAGE_SIZE:
                return records
        # 다 읽지 못한 내역을 환불 근거로 쓰면 안 된다
        raise RuntimeError("Withdrawal history pagination limit reached; result is incomplete")
