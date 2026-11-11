from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import ui

from .. import config
from ..errors import PriceUnavailableError
from ..services.price_service import FX_FALLBACK
from .base import PersistentLayout, divider, fee_percent, rows, subtext, timestamp
from .deposit import show_deposit_modal
from .info import show_info
from .transfer import start_transfer
from .wallet import show_wallets

if TYPE_CHECKING:
    from ..bot import CoinBot

log = logging.getLogger(__name__)


@dataclass
class PanelStatus:
    stock_krw: float | None = None
    usdt_krw: float | None = None
    fx_rate: float | None = None  # 김프 계산 기준 환율
    fx_source: str | None = None
    btc_premium: float | None = None  # 시장 김프 (업비트 BTC / 바이낸스×환율)
    btc_applied: float | None = None  # 실제 판매에 붙는 김프 = 시장 김프 + /김프
    usdt_premium: float | None = None
    updated_at: datetime | None = None


async def build_panel_status(bot: CoinBot) -> PanelStatus:
    status = PanelStatus(updated_at=datetime.now(timezone.utc))
    try:
        # 현물 지갑에 있는 건 USDT·USD까지 전부 재고로 친다
        account = await bot.binance.get_account()
        holdings = {
            b["asset"]: float(b["free"])
            for b in account.get("balances", [])
            if float(b.get("free") or 0) > 0
        }
    except Exception:
        log.warning("Panel status: failed to load spot balances", exc_info=True)
        holdings = None

    try:
        status.usdt_krw = await bot.prices.get_usdt_krw()
        prices = await bot.prices.get_prices(sorted({"BTC", "USDT"} | set(holdings or {})))
    except PriceUnavailableError:
        return status

    status.btc_premium = prices["BTC"].kimchi_premium
    status.btc_applied = prices["BTC"].applied_premium
    if bot.prices.fx_source != FX_FALLBACK:
        # 바이낸스 USDT 1개 = 1달러라 원화 환산값이 곧 환율
        status.fx_rate, status.fx_source = prices["USDT"].binance_krw, bot.prices.fx_source
        status.usdt_premium = prices["USDT"].kimchi_premium
    if holdings is not None:
        status.stock_krw = sum(
            amount * prices[asset].binance_krw for asset, amount in holdings.items()
        )
    return status


# custom_id는 이미 올라가 있는 패널과 맞아야 하니 바꾸지 말 것
_MENU = [
    ("main:deposit", "충전", discord.ButtonStyle.primary, show_deposit_modal),
    ("main:transfer", "송금", discord.ButtonStyle.primary, start_transfer),
    ("main:wallet", "주소록", discord.ButtonStyle.secondary, show_wallets),
    ("main:info", "내 정보", discord.ButtonStyle.secondary, show_info),
]

_UNKNOWN = "확인 중"


def _won(value: float | None) -> str:
    return f"₩{value:,.0f}" if value is not None else _UNKNOWN


def _figures(status: PanelStatus | None) -> tuple[str, str]:
    status = status or PanelStatus()
    # 환율을 못 받으면 김프도 의미가 없다
    known = status.fx_rate is not None and status.btc_applied is not None
    figures = (
        f"-# 보유 재고\n## {_won(status.stock_krw)}\n"
        + rows(
            ("김프", f"**+{status.btc_applied:.2f}%**" if known else _UNKNOWN),
            ("USDT", f"**{_won(status.usdt_krw)}**" if status.usdt_krw is not None else _UNKNOWN),
            ("환율", f"**₩{status.fx_rate:,.2f}**" if status.fx_rate is not None else _UNKNOWN),
        )
    )
    updated = f"{timestamp(status.updated_at)} 갱신" if status.updated_at else "시세를 불러오는 중"
    return figures, f"-# 수수료 {fee_percent(config.FEE_RATE)} · {updated}"


class PanelLayout(PersistentLayout):
    def __init__(
        self, status: PanelStatus | None = None, thumbnail_url: str | None = None
    ) -> None:
        super().__init__()
        figures, footer = _figures(status)
        texts = [ui.TextDisplay(figures), ui.TextDisplay(footer)]

        items: list[ui.Item] = []
        if config.PANEL_BANNER_URL:
            items.append(ui.MediaGallery(discord.MediaGalleryItem(config.PANEL_BANNER_URL)))
        if thumbnail_url:
            items.append(ui.Section(*texts, accessory=ui.Thumbnail(thumbnail_url)))
        else:
            items += texts

        buttons = []
        for custom_id, label, style, action in _MENU:
            button = ui.Button(label=label, style=style, custom_id=custom_id)
            button.callback = action
            buttons.append(button)
        items += [
            divider(),
            ui.ActionRow(*buttons),
            subtext("출금은 바이낸스를 통해 처리돼요 · 문의는 관리자 DM"),
        ]
        self.add_item(ui.Container(*items, accent_colour=config.PANEL_COLOR))
