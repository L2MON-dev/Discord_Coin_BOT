from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from .. import config
from ..errors import PriceUnavailableError, UserFacingError
from .binance_client import BinanceClient
from .event_log import SKIP_EVENT

log = logging.getLogger(__name__)

UPBIT_TICKER_ALL_URL = "https://api.upbit.com/v1/ticker/all"
# 환율은 네이버가 주는 하나은행 고시환율(김프 사이트들이 쓰는 값), 안 되면 야후
NAVER_USD_KRW_URL = "https://api.stock.naver.com/marketindex/exchange/FX_USDKRW"
YAHOO_USD_KRW_URL = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
# 시세는 이 간격으로 새로 받고
REFRESH_SECONDS = 15
# 이보다 오래된 건 쓰지 않는다 (API 장애 때)
MAX_PRICE_AGE_SECONDS = 60
FX_REFRESH_SECONDS = 60
# 환율이 이보다 오래됐거나 업비트 USDT 가격과 이 비율 넘게 차이 나면 못 믿는 값으로 보고
# 업비트 USDT 가격으로 환산한다
FX_MAX_AGE_SECONDS = 10 * 60
FX_MAX_GAP = 0.10
FX_FALLBACK = "업비트 USDT"
# 접속 자체가 안 될 때(업비트 점검 등) 빨리 포기한다. 그동안 락을 잡고 있어 다른 조회가 다 밀린다
CONNECT_TIMEOUT_SECONDS = 5

POPULAR_COINS = [
    "USDT", "BTC", "ETH", "XRP", "SOL", "DOGE",
    "ADA", "TRX", "POL", "LTC", "LINK",
    "BNB", "AVAX", "DOT", "ATOM", "NEAR",
]


@dataclass
class CoinPrice:
    coin: str
    binance_usdt: float
    binance_krw: float
    upbit_krw: float | None
    kimchi_premium: float | None  # 업비트 가격이 바이낸스×환율보다 얼마나 비싼지(%)
    upbit_usdt_krw: float | None = None

    @property
    def premium_over_usdt(self) -> float | None:
        """이 코인의 김프가 USDT 김프(국내 시장 전체)보다 얼마나 높은지(%). 업비트 USDT 가격이 없으면 김프 그대로."""
        if self.upbit_krw is None or not self.upbit_usdt_krw or self.binance_usdt <= 0:
            return self.kimchi_premium
        return (self.upbit_krw / (self.binance_usdt * self.upbit_usdt_krw) - 1) * 100

    @property
    def usdt_premium(self) -> float | None:
        """업비트 USDT의 김프(%). 국내 시장 전체의 프리미엄으로 본다."""
        if not self.upbit_usdt_krw or self.binance_usdt <= 0 or self.binance_krw <= 0:
            return None
        rate = self.binance_krw / self.binance_usdt
        return (self.upbit_usdt_krw / rate - 1) * 100

    @property
    def market_premium(self) -> float:
        """이 코인의 시장 김프(%). 0 밑으로 내려가지 않고 USDT 김프보다 낮지도 않다.

        코인은 USDT로 사는데 USDT에 이미 그만큼의 김프가 붙어 있다. 코인 자체 김프가 더 낮다고
        그대로 팔면 원가보다 싸게 파는 셈이다.
        """
        measured = [p for p in (self.kimchi_premium, self.usdt_premium) if p is not None]
        return max([0.0, *measured])

    @property
    def applied_premium(self) -> float:
        """판매에 붙는 김프 = 시장 김프 + /김프"""
        return self.market_premium + config.MIN_KIMCHI_PREMIUM_PCT

    @property
    def sale_krw(self) -> float:
        return self.binance_krw * (1 + self.applied_premium / 100)


class PriceService:
    """업비트와 바이낸스 시세를 받아 캐시한다.

    거래소별로 전체 시세를 한 번에 받으므로 받은 시각이 모든 코인에 그대로 맞는다.
    시세가 오래되면 값을 짐작하지 않고 PriceUnavailableError를 낸다.
    """

    def __init__(self, binance: BinanceClient) -> None:
        self._binance = binance
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._binance_usdt: dict[str, float] = {}
        self._binance_time: float | None = None
        self._upbit_krw: dict[str, float] = {}
        self._upbit_time: float | None = None
        self._fx: float | None = None
        self._fx_time: float | None = None
        self._fx_tried: float | None = None
        self._fx_from = FX_FALLBACK
        # 마지막으로 쓴 환율의 출처. FX_FALLBACK이면 쓸 만한 환율이 없었다는 뜻
        self.fx_source = FX_FALLBACK

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=CONNECT_TIMEOUT_SECONDS)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _age(fetched_at: float | None) -> float:
        return float("inf") if fetched_at is None else time.monotonic() - fetched_at

    async def _fetch_upbit_krw(self) -> dict[str, float]:
        async with self._get_session().get(
            UPBIT_TICKER_ALL_URL, params={"quote_currencies": "KRW"}
        ) as res:
            res.raise_for_status()
            data = await res.json(content_type=None)
        return {
            item["market"].removeprefix("KRW-"): float(item["trade_price"])
            for item in data
            if str(item.get("market", "")).startswith("KRW-")
        }

    async def _fetch_json(self, url: str):
        async with self._get_session().get(url, headers={"User-Agent": "Mozilla/5.0"}) as res:
            res.raise_for_status()
            return await res.json(content_type=None)

    async def _fetch_usd_krw(self) -> tuple[float, str]:
        try:
            data = await self._fetch_json(NAVER_USD_KRW_URL)
            return float(str(data["exchangeInfo"]["calcPrice"]).replace(",", "")), "하나은행"
        except Exception:
            log.info("Naver exchange rate unavailable; trying Yahoo", exc_info=True)
        data = await self._fetch_json(YAHOO_USD_KRW_URL)
        return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"]), "야후 파이낸스"

    async def _refresh(self) -> None:
        async with self._lock:
            if self._age(self._binance_time) >= REFRESH_SECONDS:
                try:
                    self._binance_usdt = await self._binance.get_all_ticker_prices()
                    self._binance_time = time.monotonic()
                except Exception:
                    log.warning("Failed to fetch Binance prices", exc_info=True)
            if self._age(self._upbit_time) >= REFRESH_SECONDS:
                try:
                    self._upbit_krw = await self._fetch_upbit_krw()
                    self._upbit_time = time.monotonic()
                except Exception:
                    # 여기서는 알리지 않는다. 장애와 회복은 HealthMonitor가 한 번씩 알린다 (업비트 점검 때 알림이 쏟아졌었다)
                    log.warning("Failed to fetch Upbit prices", exc_info=True,
                                extra={SKIP_EVENT: True})
            if self._age(self._fx_tried) >= FX_REFRESH_SECONDS:
                self._fx_tried = time.monotonic()
                try:
                    self._fx, self._fx_from = await self._fetch_usd_krw()
                    self._fx_time = time.monotonic()
                except Exception:
                    log.warning("Failed to fetch the USD/KRW rate", exc_info=True)

    def _rate(self, usdt_krw: float) -> float:
        """코인 값을 매길 원화 환율. 은행 환율이 최신이고 말이 되는 값이면 그걸, 아니면 업비트 USDT 가격."""
        fx = self._fx
        if fx and self._age(self._fx_time) < FX_MAX_AGE_SECONDS:
            if abs(fx / usdt_krw - 1) <= FX_MAX_GAP:
                self.fx_source = self._fx_from
                return fx
            # 범위를 벗어난 경우: 업비트 USDT보다 낮게는 치지 않고(싸게 팔게 됨) 위로는 허용 폭까지만 친다
            # 경계에서 범위 안의 값과 이어지므로 가격이 튀지 않는다
            clamped = max(usdt_krw, min(fx, usdt_krw * (1 + FX_MAX_GAP)))
            if self.fx_source != FX_FALLBACK:
                log.warning("USD/KRW %s from %s is %.1f%% off Upbit USDT %s; pricing with %.2f",
                            fx, self._fx_from, (fx / usdt_krw - 1) * 100, usdt_krw, clamped)
            self.fx_source = FX_FALLBACK
            return clamped
        if fx and self.fx_source != FX_FALLBACK:
            log.warning("USD/KRW %s from %s is stale; pricing with Upbit USDT %s",
                        fx, self._fx_from, usdt_krw)
        self.fx_source = FX_FALLBACK
        return usdt_krw

    def _usdt_value(self, asset: str) -> float:
        if asset == "USDT":
            return 1.0
        price = self._binance_usdt.get(f"{asset}USDT", 0.0)
        if not price:
            # USD 같은 법정화폐는 심볼이 반대다 (USDTUSD)
            inverse = self._binance_usdt.get(f"USDT{asset}", 0.0)
            price = 1 / inverse if inverse else 0.0
        return price

    async def get_usdt_krw(self) -> float:
        await self._refresh()
        rate = self._upbit_krw.get("USDT")
        if not rate or self._age(self._upbit_time) >= MAX_PRICE_AGE_SECONDS:
            raise PriceUnavailableError()
        return rate

    async def get_prices(self, coins: list[str]) -> dict[str, CoinPrice]:
        usdt_krw = await self.get_usdt_krw()
        if self._age(self._binance_time) >= MAX_PRICE_AGE_SECONDS:
            raise PriceUnavailableError()
        rate = self._rate(usdt_krw)

        result: dict[str, CoinPrice] = {}
        for coin in coins:
            binance_usdt = self._usdt_value(coin)
            binance_krw = binance_usdt * rate

            upbit_krw = self._upbit_krw.get(coin)
            premium = None
            if upbit_krw and binance_krw > 0:
                premium = ((upbit_krw - binance_krw) / binance_krw) * 100

            result[coin] = CoinPrice(
                coin=coin,
                binance_usdt=binance_usdt,
                binance_krw=binance_krw,
                upbit_krw=upbit_krw,
                kimchi_premium=premium,
                upbit_usdt_krw=usdt_krw,
            )
        return result

    async def get_coin_price(self, coin: str) -> CoinPrice:
        price = (await self.get_prices([coin]))[coin]
        if price.binance_krw <= 0:
            raise UserFacingError(
                f"{coin} 가격을 받아오지 못했어요. 잠시 후 다시 시도해 주세요.",
                title="시세를 불러오지 못했어요",
            )
        return price

    def sort_coins(self, coins: list[dict]) -> list[dict]:
        def key(c: dict):
            sym = c["coin"]
            if sym in POPULAR_COINS:
                return (0, POPULAR_COINS.index(sym), "")
            return (1, 0, sym)
        return sorted(coins, key=key)
