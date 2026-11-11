from __future__ import annotations

import logging
import re

import discord
from discord import ui

from .base import (
    BaseLayout, card, code, divider, format_kst, get_bot, heading, is_admin, loading,
    notice, rows, subtext,
)
from ..database import Transaction

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "pending": "처리 중",
    "unknown": "확인 중",
    "submitted": "출금 접수",
    "completed": "완료",
    "failed": "실패·환불",
}

# 키는 바이낸스 네트워크 코드
EXPLORERS = {
    "BTC": "https://mempool.space/tx/{}",
    "ETH": "https://etherscan.io/tx/{}",
    "BSC": "https://bscscan.com/tx/{}",
    "TRX": "https://tronscan.org/#/transaction/{}",
    "SOL": "https://solscan.io/tx/{}",
    "XRP": "https://xrpscan.com/tx/{}",
    "MATIC": "https://polygonscan.com/tx/{}",
    "ARBITRUM": "https://arbiscan.io/tx/{}",
    "OPTIMISM": "https://optimistic.etherscan.io/tx/{}",
    "BASE": "https://basescan.org/tx/{}",
    "AVAXC": "https://snowtrace.io/tx/{}",
    "LTC": "https://blockchair.com/litecoin/transaction/{}",
    "DOGE": "https://blockchair.com/dogecoin/transaction/{}",
}
_TXID_PATTERN = re.compile(r"[0-9A-Za-z]{20,128}")


def explorer_url(network: str, txid: str | None) -> str | None:
    # 바이낸스 내부 이체는 TXID가 해시 모양이 아니라 링크를 만들지 않는다
    template = EXPLORERS.get(network)
    if not template or not txid or not _TXID_PATTERN.fullmatch(txid):
        return None
    return template.format(txid)


def short_txid(txid: str) -> str:
    return txid if len(txid) <= 16 else f"{txid[:8]}…{txid[-6:]}"


def needs_lookup(tx: Transaction) -> bool:
    """바이낸스에 다시 물어볼 게 남았는지: 실패했거나 TXID까지 받은 완료 건은 끝난 것."""
    return tx.status != "failed" and (tx.status != "completed" or not tx.txid)


class TransactionLayout(BaseLayout):
    def __init__(self, tx: Transaction, owner_id: int, *, note: str | None = None) -> None:
        super().__init__(owner_id=owner_id)
        self.tx_id = tx.id

        details = [
            ("상태", f"**{STATUS_LABELS.get(tx.status, tx.status)}**"),
            ("일시", format_kst(tx.created_at)),
            ("코인", f"{tx.coin} · {tx.network}"),
            ("보낸 수량", f"`{tx.coin_amount}` {tx.coin}"),
            ("차감 금액", f"{tx.krw_amount:,}원"),
            ("받는 주소", code(tx.address)),
        ]
        if tx.memo:
            details.append(("메모/태그", code(tx.memo)))
        if tx.binance_withdraw_id:
            details.append(("출금 ID", code(tx.binance_withdraw_id)))

        if tx.txid:
            txid_text = code(tx.txid)
        elif tx.status == "failed":
            txid_text = "출금되지 않은 송금이라 TXID가 없어요."
        else:
            txid_text = "아직 블록체인에 전송되지 않았어요. 잠시 후 다시 조회해 주세요."

        buttons: list[ui.Item] = []
        if needs_lookup(tx):
            refresh = ui.Button(
                label="상태 다시 조회" if tx.txid else "TXID 다시 조회",
                style=discord.ButtonStyle.primary,
            )
            refresh.callback = self._refresh
            buttons.append(refresh)
        url = explorer_url(tx.network, tx.txid)
        if url:
            buttons.append(ui.Button(label="블록 탐색기", style=discord.ButtonStyle.link, url=url))

        items: list[ui.Item] = [
            heading(f"송금 #{tx.id}"),
            ui.TextDisplay(rows(*details)),
            divider(),
            ui.TextDisplay(f"-# TXID\n{txid_text}"),
        ]
        if buttons:
            items.append(ui.ActionRow(*buttons))
        if note:
            items.append(subtext(note))
        self.add_item(card(*items, tone="brand"))

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = await _transaction_view(interaction, self.tx_id)
        self.stop()
        await interaction.edit_original_response(view=view)


async def _transaction_view(interaction: discord.Interaction, tx_id: int) -> BaseLayout:
    bot = get_bot(interaction)
    tx = await bot.db.get_transaction(tx_id)
    if tx is None or (tx.discord_id != interaction.user.id and not is_admin(interaction.user.id)):
        return notice(
            "내 송금만 볼 수 있어요. 번호를 다시 확인해 주세요.",
            title="송금 내역을 찾을 수 없어요",
            tone="warning",
        )

    note = None
    if needs_lookup(tx):
        try:
            looked_up = await bot.transfers.lookup_txid(tx)
        except Exception:
            log.warning("TXID lookup failed: tx=%s", tx.id, exc_info=True)
            looked_up = tx
            note = "바이낸스 조회에 실패했어요. 잠시 후 다시 시도해 주세요."
        if looked_up is None:
            note = "조회가 몰려 있어요. 10초쯤 뒤에 다시 눌러 주세요."
        else:
            tx = looked_up
    return TransactionLayout(tx, interaction.user.id, note=note)


async def show_transaction(interaction: discord.Interaction, tx_id: int) -> None:
    await interaction.response.send_message(view=loading("송금 내역을 확인하는 중이에요"), ephemeral=True)
    try:
        view = await _transaction_view(interaction, tx_id)
    except Exception:
        log.exception("Failed to show transaction %s", tx_id)
        await interaction.edit_original_response(view=notice(
            "잠시 후 다시 시도해 주세요.",
            title="송금 내역을 불러오지 못했어요",
            tone="error",
            hint="계속되면 관리자에게 알려 주세요",
        ))
        return
    await interaction.edit_original_response(view=view)
