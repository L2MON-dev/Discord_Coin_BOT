from __future__ import annotations

from datetime import datetime, timezone

from ..database import Transaction
from .base import BaseLayout, notice, timestamp


def purchase_card(tx: Transaction, *, show_buyer: bool = True) -> BaseLayout:
    """구매 알림 채널에 올리는 카드. 공개 채널일 수 있어 주소와 TXID는 넣지 않는다."""
    buyer = f"<@{tx.discord_id}>님이" if show_buyer else "한 고객님이"
    return notice(
        f"{buyer} **{tx.coin}**을 구매했어요.",
        title=f"{tx.coin} 구매 완료",
        tone="success",
        facts=[
            ("수량", f"`{tx.coin_amount}` {tx.coin}"),
            ("네트워크", tx.network),
            ("결제 금액", f"**{tx.krw_amount:,}원**"),
        ],
        hint=f"{timestamp(datetime.now(timezone.utc), 'f')} · 패널에서 바로 구매할 수 있어요",
    )
