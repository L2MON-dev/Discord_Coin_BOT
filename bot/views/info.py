from __future__ import annotations

import discord
from discord import ui
from discord.utils import escape_markdown

from .base import (
    BaseLayout, card, divider, figure, format_kst, get_bot, heading, join_limited, loading,
    rows, truncate,
)
from .history import STATUS_LABELS, short_txid, show_transaction
from ..database import Transaction


async def show_info(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        view=loading("내 정보를 불러오는 중이에요"), ephemeral=True
    )
    bot = get_bot(interaction)
    user_id = interaction.user.id

    stats = await bot.db.get_user_stats(user_id)
    txs = await bot.db.get_user_transactions(user_id, limit=5)
    depositor_name = await bot.deposits.get_depositor_name(user_id)
    await interaction.edit_original_response(
        view=InfoLayout(user_id, stats, txs, depositor_name)
    )


def _status(tx: Transaction) -> str:
    return STATUS_LABELS.get(tx.status, tx.status)


def _when(tx: Transaction) -> str:
    return format_kst(tx.created_at, "%m-%d %H:%M")


class InfoLayout(BaseLayout):
    def __init__(
        self,
        user_id: int,
        stats: dict,
        txs: list[Transaction],
        depositor_name: str | None,
    ) -> None:
        super().__init__(owner_id=user_id)

        if txs:
            history = join_limited([
                f"`{_when(tx)}`　{tx.krw_amount:,}원 → `{tx.coin_amount}` {tx.coin} · {_status(tx)}\n"
                f"-# {tx.network}" + (f" · TXID {short_txid(tx.txid)}" if tx.txid else "")
                for tx in txs
            ])
        else:
            history = "아직 거래 내역이 없어요."

        items: list[ui.Item] = [
            heading("내 정보"),
            figure("잔액", f"{stats['balance']:,}원"),
            divider(),
            ui.TextDisplay(rows(
                ("총 충전", f"{stats['total_deposited']:,}원"),
                ("총 사용", f"{stats['total_spent']:,}원"),
                ("거래", f"{stats['transaction_count']}건"),
                ("입금자명", escape_markdown(depositor_name) if depositor_name else "미지정 (관리자에게 요청)"),
            )),
            divider(),
            ui.TextDisplay(f"-# 최근 거래 · 한국 시간\n{history}"),
        ]
        if txs:
            self.select = ui.Select(
                placeholder="거래 상세·TXID 보기",
                options=[
                    discord.SelectOption(
                        label=truncate(f"#{tx.id} {tx.coin_amount} {tx.coin}", 100),
                        value=str(tx.id),
                        description=truncate(f"{_when(tx)} · {_status(tx)}", 100),
                    )
                    for tx in txs
                ],
            )
            self.select.callback = self._on_select
            items.append(ui.ActionRow(self.select))
        self.add_item(card(*items, tone="brand"))

    async def _on_select(self, interaction: discord.Interaction) -> None:
        await show_transaction(interaction, int(self.select.values[0]))
