from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import ui
from discord.utils import escape_markdown

from .base import (
    BaseLayout, BaseModal, card, code, divider, error_notice, fee_percent, figure, get_bot,
    heading, loading, notice, reply_ephemeral, rows, subtext, truncate,
)
from .deposit import deposit_button
from .history import show_transaction
from ..database import Transaction, Wallet
from ..errors import ServicePausedError, UserFacingError
from ..services.binance_client import free_balance
from ..services.event_log import SKIP_EVENT, exception_details
from ..services.transfer_service import (
    SLIPPAGE_TOLERANCE, TransferQuote, TransferService, _fmt, accepts_memo,
)
from .. import config

log = logging.getLogger(__name__)

_STEPS = ("코인", "네트워크", "주소·금액", "확인")


@dataclass
class TransferSession:
    """송금 한 건의 진행 상태. 송금 버튼을 누를 때마다 새로 만들어서 창끼리 섞이지 않는다."""

    user_id: int
    coins: list[dict]
    selected_coin: str | None = None
    coin_data: dict | None = None
    selected_network: dict | None = None
    address: str | None = None
    memo: str | None = None


@dataclass(frozen=True)
class TransferOrder:
    # 확인 화면에 보여 준 내용 그대로. 이후에 바뀌면 안 돼서 frozen
    coin: str
    network_info: dict
    address: str
    memo: str | None
    krw_amount: int


def _progress(current: str) -> str:
    return "-# " + " › ".join(f"**{step}**" if step == current else step for step in _STEPS)


def _flow_card(title: str, step: str, *body: ui.Item, tone: str = "brand") -> ui.Container:
    return card(ui.TextDisplay(f"{_progress(step)}\n### {title}"), *body, tone=tone)


async def _gone(interaction: discord.Interaction, what: str) -> None:
    await reply_ephemeral(
        interaction,
        f"선택한 {what} 정보를 찾지 못했어요.",
        title="화면이 오래됐어요",
        hint="패널에서 송금을 다시 눌러 주세요",
    )


def _cancelled_view() -> BaseLayout:
    return notice("잔액은 그대로예요.", title="송금을 취소했어요")


async def start_transfer(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        view=loading("보낼 수 있는 코인을 불러오는 중이에요"), ephemeral=True
    )
    bot = get_bot(interaction)
    if await bot.db.get_service_pause() is not None:
        await interaction.edit_original_response(view=error_notice(ServicePausedError()))
        return

    user = await bot.db.get_user(interaction.user.id)
    if user.balance <= 0:
        await interaction.edit_original_response(view=notice(
            "코인을 보내려면 먼저 원화를 충전해 주세요.",
            title="잔액이 부족해요",
            tone="warning",
            facts=[("현재 잔액", f"**{user.balance:,}원**")],
            buttons=[deposit_button()],
        ))
        return

    try:
        coins = await bot.binance.get_withdrawable_coins()
    except Exception:
        log.warning("Failed to load withdrawable coins", exc_info=True)
        await interaction.edit_original_response(view=notice(
            "바이낸스에서 코인 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.",
            title="거래소에 연결하지 못했어요",
            tone="error",
            hint="계속되면 관리자에게 알려 주세요",
        ))
        return

    # 재고가 없어도 바이낸스에 USDT 마켓이 있으면 전송 때 사서 보낸다
    try:
        buyable = set(await bot.binance.get_usdt_markets())
        # USDT 자체는 USD로 산다
        if await bot.binance.get_market("USDTUSD"):
            buyable.add("USDT")
    except Exception:
        log.warning("Failed to load USDT markets; offering coins in stock only", exc_info=True)
        buyable = set()
    # config.TRANSFER_COINS에 있는 코인만, 그 순서대로
    offered = {c["coin"]: c for c in coins if free_balance(c) > 0 or c["coin"] in buyable}
    coins = [offered[symbol] for symbol in config.TRANSFER_COINS if symbol in offered]
    if not coins:
        await interaction.edit_original_response(view=notice(
            "지금은 재고도 없고 살 수 있는 코인도 없어요. 잠시 후 다시 확인해 주세요.",
            title="보낼 수 있는 코인이 없어요",
            tone="warning",
            hint="계속되면 관리자에게 알려 주세요",
        ))
        return

    session = TransferSession(user_id=interaction.user.id, coins=coins)
    await interaction.edit_original_response(
        view=CoinSelectLayout(session, balance=user.balance)
    )


class CoinSelectLayout(BaseLayout):
    def __init__(self, session: TransferSession, *, balance: int) -> None:
        super().__init__(owner_id=session.user_id)
        self.session = session
        self.select = ui.Select(
            placeholder="보낼 코인 선택",
            options=[
                discord.SelectOption(
                    label=c["coin"],
                    value=c["coin"],
                    description=(truncate(c["name"], 100) or None),
                )
                for c in session.coins[:config.MAX_TRANSFER_COINS]
            ],
        )
        self.select.callback = self._on_select
        self.add_item(_flow_card(
            "코인 선택",
            "코인",
            ui.TextDisplay(rows(("보유 잔액", f"**{balance:,}원**"))),
            ui.ActionRow(self.select),
        ))

    async def _on_select(self, interaction: discord.Interaction) -> None:
        session = self.session
        selected = self.select.values[0]
        coin_data = next((c for c in session.coins if c["coin"] == selected), None)
        if coin_data is None:
            await _gone(interaction, "코인")
            return

        session.selected_coin = selected
        session.coin_data = coin_data

        if len(coin_data["networks"]) == 1:
            session.selected_network = coin_data["networks"][0]
            await _show_address_step(interaction, session)
            return

        await interaction.response.edit_message(view=NetworkSelectLayout(session))


class NetworkSelectLayout(BaseLayout):
    def __init__(self, session: TransferSession) -> None:
        super().__init__(owner_id=session.user_id)
        self.session = session
        coin = session.selected_coin
        self.select = ui.Select(
            placeholder="네트워크 선택",
            options=[
                discord.SelectOption(
                    label=net["network"],
                    value=net["network"],
                    description=f"{net['name']} · 수수료 {net['withdraw_fee']} {coin}"[:100],
                )
                for net in session.coin_data["networks"][:25]
            ],
        )
        self.select.callback = self._on_select
        self.add_item(_flow_card(
            "네트워크 선택",
            "네트워크",
            ui.TextDisplay(
                f"**{coin}** 보낼 네트워크를 골라 주세요.\n"
                "받는 지갑과 **같은 네트워크**여야 해요."
            ),
            ui.ActionRow(self.select),
        ))

    async def _on_select(self, interaction: discord.Interaction) -> None:
        session = self.session
        selected = self.select.values[0]
        network = next(
            (n for n in session.coin_data["networks"] if n["network"] == selected),
            None,
        )
        if network is None:
            await _gone(interaction, "네트워크")
            return

        session.selected_network = network
        await _show_address_step(interaction, session)


def _default_amount(balance: int) -> int:
    # 금액 칸 기본값: 잔액 전부, 1회 한도가 있으면 거기까지
    if config.MAX_TRANSFER_KRW:
        return max(min(balance, config.MAX_TRANSFER_KRW), 0)
    return max(balance, 0)


async def _show_address_step(
    interaction: discord.Interaction, session: TransferSession
) -> None:
    bot = get_bot(interaction)
    wallets = await bot.db.get_wallets(
        session.user_id,
        coin=session.selected_coin,
        network=session.selected_network["network"],
    )
    user = await bot.db.get_user(session.user_id)
    await interaction.response.edit_message(
        view=AddressLayout(session, wallets, balance=user.balance)
    )


class AddressLayout(BaseLayout):
    def __init__(self, session: TransferSession, wallets: list[Wallet], *, balance: int) -> None:
        super().__init__(owner_id=session.user_id)
        self.session = session
        network = session.selected_network

        body: list[ui.Item] = [
            ui.TextDisplay(rows(
                ("코인", f"**{session.selected_coin}** · {network['network']} 네트워크"),
                ("보유 잔액", f"**{balance:,}원**"),
                ("수수료", fee_percent(config.FEE_RATE)),
            )),
        ]
        if network.get("memo_required"):
            body.append(ui.TextDisplay(
                "이 네트워크는 거래소로 보낼 때 **메모(태그)**가 필요해요."
            ))

        self._wallets = {w.id: w for w in wallets[:25]}
        if self._wallets:
            self.favorite_select = ui.Select(
                placeholder="주소록에서 선택",
                options=[
                    discord.SelectOption(
                        label=truncate(w.label, 100),
                        value=str(w.id),
                        description=truncate(w.address, 100),
                    )
                    for w in self._wallets.values()
                ],
            )
            self.favorite_select.callback = self._on_favorite
            body.append(ui.ActionRow(self.favorite_select))

        manual = ui.Button(label="주소·금액 입력", style=discord.ButtonStyle.primary)
        manual.callback = self._manual_input
        cancel = ui.Button(label="취소", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        body += [
            ui.ActionRow(manual, cancel),
            subtext("금액 칸에는 보유 잔액이 미리 채워져 있어요"),
        ]
        self.add_item(_flow_card("받는 주소와 금액", "주소·금액", *body))

    async def _current_default(self, interaction: discord.Interaction) -> int:
        user = await get_bot(interaction).db.get_user(self.session.user_id)
        return _default_amount(user.balance)

    async def _manual_input(self, interaction: discord.Interaction) -> None:
        default = await self._current_default(interaction)
        await interaction.response.send_modal(AddressModal(self.session, default_amount=default))

    async def _on_favorite(self, interaction: discord.Interaction) -> None:
        wallet = self._wallets.get(int(self.favorite_select.values[0]))
        if wallet is None:
            await _gone(interaction, "주소록")
            return

        TransferService.validate_destination(
            self.session.selected_network, wallet.address, wallet.memo
        )
        self.session.address = wallet.address
        self.session.memo = wallet.memo
        default = await self._current_default(interaction)
        await interaction.response.send_modal(
            AmountModal(self.session, default_amount=default, wallet_label=wallet.label)
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=_cancelled_view())


def _amount_input(default_amount: int) -> ui.TextInput:
    return ui.TextInput(
        label="보낼 금액 (원)",
        placeholder="50000",
        default=str(default_amount) if default_amount > 0 else None,
        style=discord.TextStyle.short,
        required=True,
        max_length=15,
    )


class AddressModal(BaseModal):
    def __init__(self, session: TransferSession, *, default_amount: int) -> None:
        super().__init__(title="받는 주소와 금액")
        self.session = session
        self.address_input = ui.TextInput(
            label="지갑 주소",
            placeholder="코인을 받을 지갑 주소",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
        )
        self.amount_input = _amount_input(default_amount)
        self.add_item(self.address_input)
        self.add_item(self.amount_input)
        # 메모를 받는 네트워크에서만 보여 준다. BSC·SOL·TRX 등에 메모를 넣으면 바이낸스가 -4106으로 거절
        self.memo_input: ui.TextInput | None = None
        if accepts_memo(session.selected_network):
            self.memo_input = ui.TextInput(
                label="메모/태그 (선택)",
                placeholder="받는 거래소가 알려 준 경우에만 입력",
                style=discord.TextStyle.short,
                required=False,
                max_length=100,
            )
            self.add_item(self.memo_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        address = self.address_input.value.strip()
        memo = (self.memo_input.value.strip() or None) if self.memo_input is not None else None
        TransferService.validate_destination(self.session.selected_network, address, memo)

        self.session.address = address
        self.session.memo = memo
        await _submit_amount(interaction, self.session, self.amount_input.value)


class AmountModal(BaseModal):
    # 주소록에서 주소를 골랐을 때는 금액만 받는다
    def __init__(
        self, session: TransferSession, *, default_amount: int, wallet_label: str | None = None
    ) -> None:
        title = f"보낼 금액 · {wallet_label}" if wallet_label else "보낼 금액"
        super().__init__(title=truncate(title, 45))
        self.session = session
        self.amount_input = _amount_input(default_amount)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _submit_amount(interaction, self.session, self.amount_input.value)


async def _submit_amount(
    interaction: discord.Interaction, session: TransferSession, raw_amount: str
) -> None:
    raw = raw_amount.strip().replace(",", "").removesuffix("원").strip()
    if not (raw.isascii() and raw.isdigit()) or int(raw) <= 0:
        await reply_ephemeral(
            interaction,
            "0보다 큰 금액을 숫자로만 입력해 주세요. 예: `50000`",
            title="금액을 다시 확인해 주세요",
        )
        return
    krw = int(raw)

    bot = get_bot(interaction)
    user = await bot.db.get_user(session.user_id)
    if krw > user.balance:
        await reply_ephemeral(
            interaction,
            "보낼 금액이 잔액보다 많아요. 금액을 줄이거나 먼저 충전해 주세요.",
            title="잔액이 부족해요",
            facts=[
                ("보낼 금액", f"{krw:,}원"),
                ("현재 잔액", f"{user.balance:,}원"),
                ("부족한 금액", f"**{krw - user.balance:,}원**"),
            ],
            buttons=[deposit_button()],
        )
        return
    await bot.transfers.check_limits(session.user_id, krw)

    # 시세 조회가 3초를 넘길 수 있다
    await interaction.response.defer()
    order = TransferOrder(
        coin=session.selected_coin,
        network_info=session.selected_network,
        address=session.address,
        memo=session.memo,
        krw_amount=krw,
    )
    quote = await bot.transfers.create_quote(
        coin=order.coin, network_info=order.network_info, krw_amount=krw
    )
    await interaction.edit_original_response(
        view=ConfirmLayout(session.user_id, order, quote)
    )


class ConfirmLayout(BaseLayout):
    def __init__(
        self,
        owner_id: int,
        order: TransferOrder,
        quote: TransferQuote,
        *,
        repriced: bool = False,
        fee_changed: bool = False,
    ) -> None:
        super().__init__(owner_id=owner_id)
        self.order = order
        self.quote = quote
        self._submitted = False

        coin = quote.coin
        body: list[ui.Item] = []
        if fee_changed:
            title, tone = "수수료가 바뀌었어요", "warning"
            body.append(ui.TextDisplay(
                "그사이 수수료가 바뀌어 새 견적을 준비했어요. 확인한 뒤 다시 **전송**을 눌러 주세요."
            ))
        elif repriced:
            title, tone = "시세가 바뀌었어요", "warning"
            body.append(ui.TextDisplay(
                "시세나 출금 조건이 바뀌어 새 견적을 준비했어요. 확인한 뒤 다시 **전송**을 눌러 주세요."
            ))
        else:
            title, tone = "송금 확인", "brand"

        destination = [("네트워크", quote.network), ("받는 주소", code(order.address))]
        if order.memo:
            destination.append(("메모/태그", code(order.memo)))

        body += [
            figure("예상 수령량", f"{_fmt(quote.receive_amount)} {coin}"),
            divider(),
            ui.TextDisplay(rows(
                ("결제 금액", f"{quote.krw_amount:,}원"),
                (f"수수료 {fee_percent(quote.fee_rate)}", f"{quote.fee_krw:,}원"),
                ("김프", f"+{quote.premium_pct:.2f}%"),
                ("전송 수량", f"`{_fmt(quote.coin_amount)}` {coin}"),
                ("네트워크 수수료", f"`{_fmt(quote.network_fee)}` {coin}"),
            )),
            divider(),
            ui.TextDisplay(rows(*destination)),
        ]
        if not order.memo and order.network_info.get("memo_required"):
            body.append(ui.TextDisplay(
                "**메모 없이** 거래소 지갑으로 보내면 자산을 잃을 수 있어요."
            ))

        confirm = ui.Button(label="전송", style=discord.ButtonStyle.success)
        confirm.callback = self._confirm
        cancel = ui.Button(label="취소", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        body += [
            ui.ActionRow(confirm, cancel),
            subtext(
                f"전송 직전에 시세를 다시 확인하고, 수령량이 {SLIPPAGE_TOLERANCE:.1%} 넘게 줄면 다시 물어봐요\n"
                "전송한 뒤에는 취소할 수 없어요"
            ),
        ]
        self.add_item(_flow_card(title, "확인", *body, tone=tone))

    async def _confirm(self, interaction: discord.Interaction) -> None:
        # await 전에 플래그부터 세운다. 더블클릭해도 한 번만 나가야 한다
        if self._submitted:
            await _already_sending(interaction)
            return
        self._submitted = True
        self.stop()

        await interaction.response.edit_message(view=notice(
            "시세를 다시 확인한 뒤 바이낸스에 출금을 요청하고 있어요.",
            title="송금을 처리하고 있어요",
            hint="보통 몇 초면 끝나요. 창을 닫아도 송금은 계속 진행돼요",
        ))

        bot = get_bot(interaction)
        order = self.order
        try:
            network = await bot.transfers.fresh_network(order.coin, order.network_info["network"])
            bot.transfers.validate_destination(network, order.address, order.memo)
            order = TransferOrder(order.coin, network, order.address, order.memo, order.krw_amount)
            quote = await bot.transfers.create_quote(
                coin=order.coin,
                network_info=order.network_info,
                krw_amount=order.krw_amount,
            )
            # 화면에 보여 준 것보다 불리해지면 보내지 않고 다시 확인받는다
            if quote.fee_rate > self.quote.fee_rate:
                await interaction.edit_original_response(
                    view=ConfirmLayout(self.owner_id, order, quote, fee_changed=True)
                )
                return
            if (quote.network_fee != self.quote.network_fee
                    or (quote.memo_required and not self.quote.memo_required and not order.memo)
                    or quote.receive_amount < self.quote.receive_amount * (1 - SLIPPAGE_TOLERANCE)):
                await interaction.edit_original_response(
                    view=ConfirmLayout(self.owner_id, order, quote, repriced=True)
                )
                return
            tx = await bot.transfers.execute(
                discord_id=interaction.user.id,
                quote=quote,
                address=order.address,
                memo=order.memo,
            )
        except UserFacingError as exc:
            await interaction.edit_original_response(view=error_notice(exc))
            return
        except Exception as exc:
            log.exception("Transfer failed", extra={SKIP_EVENT: True})
            await bot.events.record(
                "transfer.error",
                f"**{order.coin}** ({order.network_info['network']}) {order.krw_amount:,}원 송금 중 예상하지 못한 "
                "오류가 났어요. 잔액이 차감됐는지 '유저 보기'로 확인하고, 송금이 만들어졌다면 "
                "'송금 상세'에서 바이낸스와 대조하세요.",
                level="error",
                user_id=interaction.user.id,
                details={"주소": order.address, **exception_details(exc)},
            )
            await interaction.edit_original_response(view=notice(
                "송금하다 예상하지 못한 오류가 났어요.",
                title="송금하지 못했어요",
                tone="error",
                hint="잔액이 차감됐다면 관리자에게 문의해 주세요. 관리자에게도 자동으로 알렸어요",
            ))
            return

        user = await bot.db.get_user(interaction.user.id)
        await interaction.edit_original_response(
            view=ResultLayout(interaction.user.id, tx, balance=user.balance)
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await _already_sending(interaction)
            return
        self.stop()
        await interaction.response.edit_message(view=_cancelled_view())


async def _already_sending(interaction: discord.Interaction) -> None:
    await reply_ephemeral(
        interaction,
        "같은 송금이 두 번 나가지 않도록 한 번만 처리해요.",
        title="이미 처리 중인 송금이에요",
        tone="info",
    )


class ResultLayout(BaseLayout):
    def __init__(self, owner_id: int, tx: Transaction, *, balance: int) -> None:
        super().__init__(owner_id=owner_id)
        self.tx = tx

        details = [
            ("네트워크", tx.network),
            ("받는 주소", code(tx.address)),
            ("차감 금액", f"{tx.krw_amount:,}원"),
            ("남은 잔액", f"**{balance:,}원**"),
        ]
        if tx.binance_withdraw_id:
            details.append(("출금 ID", code(tx.binance_withdraw_id)))

        txid_button = ui.Button(label="TXID 조회", style=discord.ButtonStyle.primary)
        txid_button.callback = self._show_txid
        self.save_button = ui.Button(
            label="주소록에 저장", style=discord.ButtonStyle.secondary
        )
        self.save_button.callback = self._save
        self.add_item(card(
            heading(
                "출금 요청 완료",
                "바이낸스에 출금 요청이 접수됐어요. 블록체인 전송까지 조금 걸릴 수 있어요.",
            ),
            figure("보낸 수량", f"{tx.coin_amount} {tx.coin}"),
            divider(),
            ui.TextDisplay(rows(*details)),
            ui.ActionRow(txid_button, self.save_button),
            subtext("TXID는 블록체인 전송이 시작되면 조회돼요. 내 정보에서도 다시 볼 수 있어요"),
            tone="success",
        ))

    async def _show_txid(self, interaction: discord.Interaction) -> None:
        await show_transaction(interaction, self.tx.id)

    async def _save(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SaveWalletModal(self))


class SaveWalletModal(BaseModal):
    def __init__(self, result: ResultLayout) -> None:
        super().__init__(title="주소록에 저장")
        self._result = result

        self.label_input = ui.TextInput(
            label="이름",
            placeholder="내 메타마스크, 업비트 등",
            style=discord.TextStyle.short,
            required=True,
            max_length=30,
        )
        self.add_item(self.label_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        label = " ".join(self.label_input.value.split())
        if not label:
            await reply_ephemeral(
                interaction, "주소를 구분할 이름을 입력해 주세요.", title="이름이 비어 있어요"
            )
            return

        result = self._result
        tx = result.tx
        bot = get_bot(interaction)
        await bot.db.save_wallet(
            discord_id=interaction.user.id,
            label=label,
            coin=tx.coin,
            network=tx.network,
            address=tx.address,
            memo=tx.memo,
        )

        # 결과 화면은 그대로 살려 둔다. TXID 버튼이 계속 눌려야 한다
        result.save_button.disabled = True
        result.save_button.label = "저장됨"
        await interaction.response.edit_message(view=result)
        await interaction.followup.send(
            view=notice(
                f"**{escape_markdown(label)}** 이름으로 저장했어요.",
                title="주소록에 저장했어요",
                tone="success",
                facts=[("코인", f"{tx.coin} · {tx.network}"), ("주소", code(tx.address))],
                hint="다음 송금 때 주소록에서 바로 고를 수 있어요",
            ),
            ephemeral=True,
        )
