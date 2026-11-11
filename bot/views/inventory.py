from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import ui

from .base import (
    BaseLayout, card, divider, get_bot, heading, join_limited, loading, notice,
    send_ephemeral, subtext,
)
from ..errors import PriceUnavailableError
from ..services.binance_client import free_balance

if TYPE_CHECKING:
    from ..bot import CoinBot

log = logging.getLogger(__name__)

MAX_LISTED_COINS = 15


def _fmt_krw(value: float) -> str:
    if value >= 1_000_000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.4f}"


def _api_error() -> BaseLayout:
    return notice(
        "바이낸스에서 재고와 시세를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.",
        title="거래소에 연결하지 못했어요",
        tone="error",
        hint="계속되면 관리자에게 알려 주세요",
    )


async def _build_inventory(bot: CoinBot) -> InventoryLayout:
    coins = await bot.binance.get_withdrawable_coins()
    in_stock = bot.prices.sort_coins([c for c in coins if free_balance(c) > 0])
    if not in_stock:
        return InventoryLayout(["지금 전송 가능한 코인이 없어요."], header="")

    shown = in_stock[:MAX_LISTED_COINS]
    try:
        prices = await bot.prices.get_prices([c["coin"] for c in shown])
        header = f"USDT 시세 **₩{await bot.prices.get_usdt_krw():,.0f}**"
    except PriceUnavailableError:
        prices, header = {}, "시세를 불러오지 못했어요"

    lines: list[str] = []
    for c in shown:
        symbol = c["coin"]
        price = prices.get(symbol)
        line = f"**{symbol}**　재고 `{free_balance(c):g}`"
        if price and price.binance_krw > 0:
            line += f" · ₩{_fmt_krw(price.sale_krw)} · 김프 {price.applied_premium:+.2f}%"
        lines.append(line)
    return InventoryLayout(lines, header=header)


async def show_inventory(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        view=loading("재고와 시세를 불러오는 중이에요"), ephemeral=True
    )
    try:
        view: BaseLayout = await _build_inventory(get_bot(interaction))
    except Exception:
        log.warning("Failed to load inventory", exc_info=True)
        view = _api_error()
    await interaction.edit_original_response(view=view)


class InventoryLayout(BaseLayout):
    def __init__(self, lines: list[str], *, header: str) -> None:
        super().__init__()
        refresh = ui.Button(label="새로고침", style=discord.ButtonStyle.secondary)
        refresh.callback = self._refresh
        self.add_item(card(
            heading("재고 · 시세", header or None),
            divider(),
            ui.TextDisplay(join_limited(lines)),
            ui.ActionRow(refresh),
            subtext("가격은 바이낸스 시세에 김프를 더한 값이에요. 재고가 없는 코인도 USDT로 사서 보낼 수 있어요"),
            tone="brand",
        ))

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            view = await _build_inventory(get_bot(interaction))
        except Exception:
            log.warning("Failed to refresh inventory", exc_info=True)
            await send_ephemeral(interaction, _api_error())
            return
        self.stop()
        await interaction.edit_original_response(view=view)
