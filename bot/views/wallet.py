from __future__ import annotations

import discord
from discord import ui
from discord.utils import escape_markdown

from .base import (
    BaseLayout, card, code, divider, get_bot, heading, join_limited, loading,
    reply_ephemeral, subtext, truncate,
)
from ..database import Wallet

MAX_LISTED_WALLETS = 20
# 셀렉트 메뉴 하나에 옵션 25개까지
MAX_SELECT_OPTIONS = 25


async def show_wallets(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        view=loading("주소록을 불러오는 중이에요"), ephemeral=True
    )
    bot = get_bot(interaction)
    wallets = await bot.db.get_wallets(interaction.user.id)
    await interaction.edit_original_response(view=WalletLayout(wallets, interaction.user.id))


class WalletLayout(BaseLayout):
    def __init__(self, wallets: list[Wallet], owner_id: int) -> None:
        super().__init__(owner_id=owner_id)

        if not wallets:
            self.add_item(card(
                heading("주소록", "저장된 지갑 주소가 없어요."),
                subtext("송금을 마치면 받는 주소를 주소록에 저장할 수 있어요"),
                tone="brand",
            ))
            return

        lines: list[str] = []
        for w in wallets[:MAX_LISTED_WALLETS]:
            memo = f" · 메모 {code(w.memo)}" if w.memo else ""
            lines.append(
                f"**{escape_markdown(w.label)}**　{w.coin} · {w.network}\n"
                f"{code(w.address)}{memo}"
            )

        self.select = ui.Select(
            placeholder="삭제할 주소 선택",
            options=[
                discord.SelectOption(
                    label=truncate(f"{w.label} ({w.coin})", 100),
                    value=str(w.id),
                    description=truncate(w.address, 100),
                )
                for w in wallets[:MAX_SELECT_OPTIONS]
            ],
        )
        self.select.callback = self._on_delete_select
        self.add_item(card(
            heading("주소록"),
            ui.TextDisplay(join_limited(lines, sep="\n\n")),
            divider(),
            ui.ActionRow(self.select),
            subtext("고르면 바로 삭제돼요"),
            tone="brand",
        ))

    async def _on_delete_select(self, interaction: discord.Interaction) -> None:
        bot = get_bot(interaction)
        deleted = await bot.db.delete_wallet(int(self.select.values[0]), interaction.user.id)
        if not deleted:
            await reply_ephemeral(
                interaction,
                "이미 지웠거나 다른 화면에서 삭제한 주소예요.",
                title="주소를 삭제하지 못했어요",
            )
            return

        self.stop()
        wallets = await bot.db.get_wallets(interaction.user.id)
        await interaction.response.edit_message(view=WalletLayout(wallets, interaction.user.id))
