from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import discord
from discord import ui
from discord.utils import escape_markdown

from .base import (
    BaseLayout, BaseModal, card, code, divider, error_notice, figure, get_bot, heading, notice,
    reply_ephemeral, rows, send_ephemeral, subtext, timestamp, to_kst,
)
from .. import config
from ..database import Deposit, DepositSlotBusyError
from ..errors import ServicePausedError


async def show_deposit_modal(interaction: discord.Interaction) -> None:
    if not (config.BANK_NAME and config.BANK_ACCOUNT and config.BANK_HOLDER):
        await reply_ephemeral(
            interaction,
            "관리자가 입금 계좌를 아직 설정하지 않았어요.",
            title="지금은 충전할 수 없어요",
            tone="error",
            hint="관리자에게 알려 주세요",
        )
        return

    bot = get_bot(interaction)
    # 멈춘 동안(지급정지 등)에는 계좌 정보도 보여 주지 않는다
    if await bot.db.get_service_pause() is not None:
        await send_ephemeral(interaction, error_notice(ServicePausedError()))
        return
    if await bot.deposits.get_depositor_name(interaction.user.id) is None:
        await reply_ephemeral(
            interaction,
            "충전하려면 관리자가 입금자명을 먼저 지정해야 해요.",
            title="입금자명이 필요해요",
            hint="은행 입금 때 표시될 이름을 관리자에게 알려 주세요",
        )
        return

    pending = await bot.deposits.get_pending_deposit(interaction.user.id)
    if pending is not None:
        await interaction.response.send_message(
            view=DepositLayout(pending, title="진행 중인 충전 요청"), ephemeral=True
        )
        bot.deposit_prompts.add(pending.id, interaction)
        return

    opened_at = await bot.deposits.other_pending_since(interaction.user.id)
    if opened_at is not None:
        await interaction.response.send_message(view=deposit_wait_view(opened_at), ephemeral=True)
        return

    await interaction.response.send_modal(DepositModal())


def deposit_button(label: str = "충전하기") -> ui.Button:
    button = ui.Button(label=label, style=discord.ButtonStyle.primary)
    button.callback = show_deposit_modal
    return button


def deposit_complete_view(amount: int, new_balance: int) -> BaseLayout:
    """충전 영수증. DM으로도 보내고, 열려 있던 입금 안내 화면도 이걸로 바꾼다."""
    return notice(
        "입금이 확인되어 잔액에 반영했어요.",
        title="충전이 완료됐어요",
        tone="success",
        amount=("충전 금액", f"{amount:,}원"),
        facts=[("현재 잔액", f"{new_balance:,}원")],
        hint=timestamp(datetime.now(timezone.utc), "f"),
    )


def deposit_wait_view(opened_at: str) -> BaseLayout:
    """다른 유저의 요청이 열려 있을 때. 누구 것인지, 얼마인지는 보여 주지 않는다."""
    facts = []
    opened = to_kst(opened_at)
    if opened is not None:
        free_by = opened + timedelta(minutes=config.DEPOSIT_TIMEOUT_MINUTES)
        facts.append(("신청 가능", f"늦어도 {timestamp(free_by, 't')} ({timestamp(free_by)})"))
    return notice(
        "입금을 정확히 확인하려고 충전은 한 번에 한 분씩 받아요.\n"
        "앞선 충전이 끝나면 다시 눌러 주세요.",
        title="다른 분이 충전 중이에요",
        tone="warning",
        facts=facts,
        buttons=[deposit_button("다시 시도")],
        hint="아직 입금하지 마세요. 신청 없이 보낸 돈은 자동으로 충전되지 않아요",
    )


class DepositPrompts:
    """유저에게 보여 준 입금 안내(ephemeral)를 기억해 뒀다가 영수증으로 바꾼다.

    ephemeral 메시지는 보낸 interaction으로만 고칠 수 있고 토큰은 15분이면 끝난다.
    """

    LIFETIME = 14 * 60
    # 안내를 계속 다시 열어도 요청당 최근 3개만 고친다 (웹훅 응답이 느려지지 않게)
    MAX_PER_DEPOSIT = 3

    def __init__(self) -> None:
        self._prompts: dict[int, list[tuple[float, discord.Interaction]]] = {}

    def add(self, deposit_id: int, interaction: discord.Interaction) -> None:
        now = time.monotonic()
        for key in list(self._prompts):
            alive = [p for p in self._prompts[key] if now - p[0] < self.LIFETIME]
            if alive:
                self._prompts[key] = alive
            else:
                del self._prompts[key]
        prompts = self._prompts.setdefault(deposit_id, [])
        prompts.append((now, interaction))
        del prompts[:-self.MAX_PER_DEPOSIT]

    def discard(self, deposit_id: int) -> None:
        self._prompts.pop(deposit_id, None)

    async def complete(self, deposit_id: int, amount: int, new_balance: int) -> None:
        await self._replace(deposit_id, lambda: deposit_complete_view(amount, new_balance))

    async def checking(self, deposit_id: int) -> None:
        # 돈은 들어왔는데 수동 충전과 겹칠 수 있어 관리자가 먼저 본다 (process_incoming)
        await self._replace(deposit_id, lambda: notice(
            "입금 문자는 받았어요. 관리자가 직접 처리한 충전과 같은 돈인지 확인한 뒤 정리해요.",
            title="입금을 확인하고 있어요",
            hint="잔액은 내 정보에서 볼 수 있어요",
        ))

    async def expire(self, deposit_id: int, amount: int | None = None) -> None:
        await self._replace(deposit_id, lambda: notice(
            f"{config.DEPOSIT_TIMEOUT_MINUTES}분 안에 입금이 확인되지 않았어요.",
            title="충전 요청이 마감됐어요",
            tone="warning",
            facts=[("요청 금액", f"{amount:,}원")] if amount else (),
            buttons=[deposit_button("다시 신청")],
            hint="이미 입금했다면 관리자에게 알려 주세요",
        ))

    async def _replace(self, deposit_id: int, make_view) -> None:
        now = time.monotonic()
        for sent_at, interaction in self._prompts.pop(deposit_id, []):
            if now - sent_at >= self.LIFETIME:
                continue
            try:
                await interaction.edit_original_response(view=make_view())
            except discord.HTTPException:
                pass  # 유저가 닫았거나 토큰 만료


class DepositLayout(BaseLayout):
    def __init__(self, deposit: Deposit, *, title: str = "입금 안내") -> None:
        super().__init__(owner_id=deposit.discord_id)
        self._deposit_id = deposit.id

        created = to_kst(deposit.created_at)
        deadline = "-"
        if created is not None:
            expires = created + timedelta(minutes=config.DEPOSIT_TIMEOUT_MINUTES)
            deadline = f"{timestamp(expires, 't')} ({timestamp(expires)})"

        cancel = ui.Button(label="요청 취소", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        # 금액과 입금자명이 한 글자라도 다르면 자동 충전이 안 돼서 이 둘을 가장 크게 보여 준다
        self.add_item(card(
            heading(title, "아래 계좌로 금액과 입금자명을 똑같이 맞춰 보내 주세요."),
            divider(),
            ui.TextDisplay(rows(
                ("은행", config.BANK_NAME),
                ("계좌번호", code(config.BANK_ACCOUNT)),
                ("예금주", config.BANK_HOLDER),
            )),
            divider(),
            figure("보낼 금액", f"{deposit.amount:,}원"),
            figure("입금자명 · 받는 분에게 표시", escape_markdown(deposit.depositor_name)),
            ui.ActionRow(cancel),
            subtext(f"입금 마감 {deadline}\n입금자명에 이름만 적으면 자동 충전되지 않아요"),
            tone="brand",
        ))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        bot = get_bot(interaction)
        cancelled = await bot.deposits.cancel_deposit(interaction.user.id, self._deposit_id)
        self.stop()
        if cancelled:
            bot.deposit_prompts.discard(self._deposit_id)
            view = notice("필요할 때 패널에서 다시 신청할 수 있어요.", title="충전 요청을 취소했어요")
        else:
            view = notice(
                "이미 충전이 끝났거나 마감된 요청이에요.",
                title="취소할 수 없는 요청이에요", tone="warning",
            )
        await interaction.response.edit_message(view=view)


class DepositModal(BaseModal):
    def __init__(self) -> None:
        super().__init__(title="원화 충전")

        self.amount_input = ui.TextInput(
            label=f"충전 금액 (원, 최소 {config.MIN_DEPOSIT_KRW:,})",
            placeholder="50000",
            style=discord.TextStyle.short,
            required=True,
            max_length=15,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_amount = self.amount_input.value.strip().replace(",", "")
        if not (raw_amount.isascii() and raw_amount.isdigit()):
            await reply_ephemeral(
                interaction,
                "숫자로만 입력해 주세요. 예: `50000`",
                title="금액을 다시 확인해 주세요",
            )
            return

        amount = int(raw_amount)
        if amount < config.MIN_DEPOSIT_KRW:
            await reply_ephemeral(
                interaction,
                "최소 금액 이상으로 다시 신청해 주세요.",
                title="최소 충전 금액보다 적어요",
                facts=[
                    ("입력한 금액", f"{amount:,}원"),
                    ("최소 충전 금액", f"{config.MIN_DEPOSIT_KRW:,}원"),
                ],
            )
            return

        bot = get_bot(interaction)
        try:
            deposit = await bot.deposits.request_deposit(
                discord_id=interaction.user.id,
                amount=amount,
                channel_id=interaction.channel_id,
            )
        except DepositSlotBusyError as exc:
            # 입력창을 열어 둔 사이 다른 사람이 먼저 신청한 경우
            await interaction.response.send_message(view=deposit_wait_view(exc.opened_at), ephemeral=True)
            return
        try:
            await interaction.response.send_message(view=DepositLayout(deposit), ephemeral=True)
        except Exception:
            # 계좌 안내를 못 보여 줬으면 요청을 열어 두지 않는다
            await bot.deposits.cancel_deposit(interaction.user.id, deposit.id)
            raise
        bot.deposit_prompts.add(deposit.id, interaction)
