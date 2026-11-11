from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Optional

import discord
from discord import app_commands, ui
from discord.utils import escape_markdown

from . import config
from .errors import UserFacingError
from .services.status_channels import (
    DEFAULT_PREMIUM_FORMAT, DEFAULT_STOCK_FORMAT, PLACEHOLDERS as STATUS_PLACEHOLDERS,
    PREMIUM_CHANNEL as STATUS_PREMIUM_CHANNEL, PREMIUM_FORMAT as STATUS_PREMIUM_FORMAT,
    RENAME_INTERVAL_SECONDS as STATUS_RENAME_INTERVAL, STOCK_CHANNEL as STATUS_STOCK_CHANNEL,
    STOCK_FORMAT as STATUS_STOCK_FORMAT, render as render_status_name,
)
from .services.transfer_service import SLIPPAGE_TOLERANCE
from .database import Deposit
from .views.base import (
    DENY_NOTICE, BaseLayout, BaseModal, answer, card, check_failed, deny_non_admin, deny_notice_fields,
    deny_notice_view, deny_user, divider, error_notice, fee_percent, figure, format_kst, get_bot,
    heading, is_admin, is_allowed, join_limited, loading, notice, premium_percent, quietly,
    refuse_bot, reply_ephemeral, rows, send_ephemeral, show_result, subtext, unclear_result, working,
)
from .views.deposit import deposit_button, deposit_complete_view
from .views.ops import (
    CATEGORIES, LogFilter, build_profit_view, build_status_view, build_tx_view, build_user_view,
    dismissed_view, limit_facts, resolve_checked, send_log_list,
)

log = logging.getLogger(__name__)

# 퍼센트 단위 상한. 2.5 대신 25를 치는 식의 오타를 걸러 낸다
MAX_FEE_PERCENT = 20.0
MAX_PREMIUM_PERCENT = 10.0
# 이보다 낮은 수수료는 한 번 더 묻는다 (2.5%를 0.025로 넣으면 0.02%가 됨)
FEE_CONFIRM_BELOW = Decimal("0.005")
# 이 금액부터는 수동 충전도 한 번 더 묻는다. 0 하나 더 붙으면 그대로 출금 가능한 돈이 된다
CREDIT_CONFIRM_FROM = 500_000
# /관리자 신규한도 입력 상한. 일반 한도 이상이면 어차피 "신규 한도 없음"이라 말도 안 되는 값만 막는다
MAX_OWN_LIMIT_KRW = 1_000_000_000
# 이 권한이 하나라도 있는 역할은 자동으로 주지 않는다
RISKY_ROLE_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
    "manage_messages", "ban_members", "kick_members", "moderate_members", "mention_everyone",
)
# 운영자 명령이 / 목록에 보이는 대상(서버 관리자). 표시용일 뿐이고 실제 권한은 명령마다 ADMIN_USERS로 확인한다
OPERATOR_PERMISSIONS = discord.Permissions(administrator=True)


class _Once:
    """확인 카드의 버튼들은 합쳐서 한 번만 동작한다."""

    def __init__(self, message: str) -> None:
        self.done = False
        self._message = message

    async def taken(self, interaction: discord.Interaction) -> bool:
        if self.done:
            await reply_ephemeral(interaction, self._message, title="이미 처리했어요", tone="info")
            return True
        self.done = True
        return False


def _button(label: str, callback, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> ui.Button:
    button = ui.Button(label=label, style=style)
    button.callback = callback
    return button


async def _refresh_panels_quietly(bot) -> None:
    # 패널에 수수료·김프가 보이므로 바로 갱신한다. 실패해도 다음 주기에 반영된다
    try:
        await bot.refresh_panels()
    except Exception:
        log.warning("Refreshing the panels after a settings change failed", exc_info=True)


async def _apply_fee(interaction: discord.Interaction, rate: Decimal, *, replace: bool = False) -> None:
    """수수료를 바꾸고 결과를 알린다. replace면 확인 카드를 결과로 바꿔 보여 준다."""
    bot = get_bot(interaction)
    old = await bot.set_fee_rate(rate)
    change = f"{fee_percent(old)} → {fee_percent(rate)}"
    notes = ["봇을 재시작해도 유지돼요"]
    if rate > Decimal(str(old)):
        notes.append("이미 확인 화면을 띄운 유저는 전송 전에 새 수수료로 한 번 더 확인해요")
    view = notice(
        "새 견적부터 바로 적용돼요.",
        title="송금 수수료를 바꿨어요",
        tone="success",
        facts=[("변경", f"**{change}**")],
        hint="\n".join(notes),
    )
    if replace:
        await interaction.response.edit_message(view=view)
    else:
        await send_ephemeral(interaction, view)
    await bot.events.record(
        "admin.fee",
        f"<@{interaction.user.id}>님이 송금 수수료를 **{change}**로 바꿨어요.",
        level="success",
    )
    await _refresh_panels_quietly(bot)


def _fee_confirm(admin_id: int, rate: Decimal, typed: float) -> BaseLayout:
    new = fee_percent(rate)
    intended = (Decimal(str(typed)) * 100).normalize()
    if not rate:
        message = "송금 수수료를 받지 않게 돼요."
    elif intended <= Decimal(str(MAX_FEE_PERCENT)):
        shown = format(intended, "f")
        message = (
            f"`퍼센트:{typed:g}`는 **{new}**예요. {shown}%로 하려던 거라면 "
            f"`퍼센트:{shown}`를 입력해 주세요."
        )
    else:
        message = "평소보다 아주 낮은 수수료예요. 퍼센트 단위가 맞는지 확인해 주세요."
    once = _Once("같은 변경은 한 번만 처리해요.")

    async def apply(interaction: discord.Interaction) -> None:
        if await once.taken(interaction):
            return
        await _apply_fee(interaction, rate, replace=True)

    async def keep(interaction: discord.Interaction) -> None:
        if await once.taken(interaction):
            return
        await interaction.response.edit_message(view=notice(
            f"송금 수수료는 **{fee_percent(config.FEE_RATE)}** 그대로예요.",
            title="수수료를 바꾸지 않았어요",
        ))

    return notice(
        message,
        title=f"수수료를 {new}로 바꿀까요?",
        tone="warning",
        facts=[("지금 수수료", fee_percent(config.FEE_RATE)), ("바꿀 수수료", f"**{new}**")],
        buttons=[_button(f"{new}로 바꾸기", apply, discord.ButtonStyle.danger), _button("취소", keep)],
        hint="실수로 넣은 값이면 취소를 눌러 주세요",
        owner_id=admin_id,
    )


async def _apply_credit(
    interaction: discord.Interaction, user: discord.abc.User, amount: int, reason: str,
    *, how: str = "send", other_money: bool = False, for_request: int | None = None,
    expected_revision: int | None = None,
) -> None:
    """수동 충전을 하고 관리자와 유저에게 알린다.

    잔액을 바꾸기 전에 디스코드에 먼저 답하고(how), 결과는 show_result로만 보여 준다.
    충전 뒤에 답장이 실패하면 안 된 것처럼 보여서 한 번 더 충전하게 되기 때문이다.
    other_money: 입금과 무관한 돈(보상·환불)이라고 관리자가 표시한 경우
    for_request: 이 충전이 어느 충전 요청의 돈인지 (그사이 이미 충전됐으면 거절된다)
    """
    bot = get_bot(interaction)
    await answer(interaction, working(f"{user.mention}님에게 {amount:,}원을 충전하는 중이에요"), how=how)
    try:
        new_balance, settled = await bot.deposits.manual_credit(
            user.id, amount, reason, interaction.user.id, other_money=other_money, for_request=for_request,
            expected_revision=expected_revision,
        )
    except UserFacingError as exc:
        await show_result(interaction, error_notice(exc))
        return
    except Exception:
        log.exception("Manual credit failed: user=%s amount=%s", user.id, amount)
        await show_result(interaction, unclear_result("수동 충전"))
        return
    facts = [
        ("받은 유저", user.mention),
        ("충전 금액", f"**{amount:,}원**"),
        ("사유", escape_markdown(reason)),
        ("현재 잔액", f"{new_balance:,}원"),
    ]
    if settled is not None:
        facts.append(("충전 요청", f"진행 중이던 #{settled.id}도 이 충전으로 마감했어요"))
    view = notice(
        f"{user.mention}님에게 **{amount:,}원**을 충전했어요.",
        title="수동 충전을 마쳤어요",
        tone="success",
        facts=facts,
        hint="유저에게 충전 완료 DM을 보내요. 사유는 운영 기록에 남아요",
    )
    await show_result(interaction, view)
    if settled is not None:
        # 유저가 열어 둔 입금 안내도 영수증으로 바꾼다
        await quietly(
            bot.deposit_prompts.complete(settled.id, settled.amount, new_balance), "Updating the deposit prompt"
        )
    await bot.notify_user(user.id, view=deposit_complete_view(amount, new_balance))


def _credit_confirm(
    admin_id: int, user: discord.abc.User, amount: int, reason: str, balance: int,
    warnings: list[str] | None = None, pending: Deposit | None = None,
    *, expected_revision: int | None = None,
) -> BaseLayout:
    """큰 금액이거나 이미 충전된 돈일 수 있을 때(DepositService.credit_warnings) 한 번 더 묻는 카드.

    같은 금액의 충전 요청이 열려 있으면(pending) 그 요청의 돈인지도 고르게 한다.
    """
    once = _Once("같은 충전은 한 번만 처리해요.")

    def apply_with(*, other_money: bool = False, for_request: int | None = None):
        async def apply(interaction: discord.Interaction) -> None:
            if await once.taken(interaction):
                return
            try:
                await _apply_credit(
                    interaction, user, amount, reason, how="edit",
                    other_money=other_money, for_request=for_request, expected_revision=expected_revision,
                )
            except Exception:
                # 디스코드가 클릭을 못 받았으면 충전 전이니 버튼을 다시 누를 수 있게 둔다
                if not interaction.response.is_done():
                    once.done = False
                raise
        return apply

    async def keep(interaction: discord.Interaction) -> None:
        if await once.taken(interaction):
            return
        await interaction.response.edit_message(view=notice(
            f"{user.mention}님의 잔액은 **{balance:,}원** 그대로예요.",
            title="충전하지 않았어요",
        ))

    # 무슨 돈인지에 따라 나중에 오는 같은 금액의 입금 문자 처리가 달라진다.
    # 입금 대신 넣어 준 돈이면 그 문자를 보류하고, 보상·환불 같은 다른 돈이면 보류하지 않는다
    if pending is not None:
        paid = _button(
            f"요청 #{pending.id}의 돈 · {amount:,}원 충전", apply_with(for_request=pending.id),
            discord.ButtonStyle.danger,
        )
    else:
        paid = _button(f"{amount:,}원 충전", apply_with(), discord.ButtonStyle.danger)
    other = _button("다른 돈(보상·환불 등) · 충전만", apply_with(other_money=True))
    if warnings:
        title = "같은 돈을 두 번 충전할 수 있어요"
        message = (
            f"{user.mention}님에게 **{amount:,}원**을 충전하기 전에 확인해 주세요. "
            "아래와 같은 돈이면 두 번 충전돼요.\n" + join_limited([f"- {line}" for line in warnings])
        )
        hint = ("미확인 입금이면 `/관리자 미확인입금충전`으로 처리하세요. "
                "은행 앱에서 다른 돈인 걸 확인했을 때만 충전을 눌러 주세요")
        if pending is not None:
            hint += ("\n'다른 돈'으로 충전하면 요청은 그대로 열려 있고, 그 입금 문자가 오면 평소처럼 "
                     "자동 충전돼요")
        else:
            hint += "\n보상·환불처럼 입금과 상관없는 돈이면 '다른 돈'으로 충전하세요"
    else:
        title = f"{amount:,}원을 충전할까요?"
        message = f"{user.mention}님에게 **{amount:,}원**을 충전해요. 금액의 0 개수가 맞는지 확인해 주세요."
        hint = (f"{CREDIT_CONFIRM_FROM:,}원 이상은 한 번 더 확인해요\n"
                "보상·환불처럼 입금과 상관없는 돈이면 '다른 돈'으로 충전하세요")
    return notice(
        message,
        title=title,
        tone="warning",
        facts=[
            ("받을 유저", user.mention),
            ("충전 금액", f"**{amount:,}원**"),
            ("사유", escape_markdown(reason)),
            ("충전 후 잔액", f"{balance + amount:,}원"),
        ],
        buttons=[paid, other, _button("취소", keep)],
        hint=hint,
        owner_id=admin_id,
    )


async def _apply_new_user_limit(
    interaction: discord.Interaction, user: discord.abc.User, limit: int, *, replace: bool = False,
) -> None:
    bot = get_bot(interaction)
    old = await bot.transfers.set_new_user_limit(user.id, limit, interaction.user.id)
    status = await bot.transfers.new_user_status(user.id)
    daily = config.DAILY_TRANSFER_LIMIT_KRW
    view = notice(
        "이 유저는 이제 신규 한도 없이 일반 24시간 한도까지 보낼 수 있어요."
        if daily and limit >= daily
        else "이 유저는 신규 이용자인 동안 24시간에 이만큼까지 보낼 수 있어요.",
        title="신규 한도를 바꿨어요",
        tone="success",
        facts=[
            ("유저", user.mention),
            ("변경", f"**{old or config.NEW_USER_DAILY_LIMIT_KRW:,}원 → {limit:,}원**"),
            *limit_facts(user.id, status),
        ],
        hint="1회 한도와 전체 송금 상한은 그대로예요. 봇을 재시작해도 유지돼요\n"
             "기본 신규 한도로 되돌리려면 `/관리자 신규한도 기본으로:참`",
    )
    if replace:
        await interaction.response.edit_message(view=view)
    else:
        await send_ephemeral(interaction, view)


def _limit_confirm(
    admin_id: int, user: discord.abc.User, limit: int, current: int, facts: list,
) -> BaseLayout:
    # 한도를 올릴 때만 묻는다. 0 하나 잘못 치면 아직 잘 모르는 계정이 10배를 보낼 수 있다
    daily = config.DAILY_TRANSFER_LIMIT_KRW
    once = _Once("같은 변경은 한 번만 처리해요.")

    async def apply(interaction: discord.Interaction) -> None:
        if await once.taken(interaction):
            return
        await _apply_new_user_limit(interaction, user, limit, replace=True)

    async def keep(interaction: discord.Interaction) -> None:
        if await once.taken(interaction):
            return
        await interaction.response.edit_message(view=notice(
            f"{user.mention}님의 신규 한도는 **{current:,}원** 그대로예요.",
            title="한도를 바꾸지 않았어요",
        ))

    return notice(
        f"일반 24시간 한도({daily:,}원) 이상이라, {user.mention}님은 신규 한도 없이 일반 한도까지 "
        "보낼 수 있게 돼요."
        if daily and limit >= daily
        else f"{user.mention}님이 신규 이용자인 동안 24시간에 **{limit:,}원**까지 보낼 수 있게 돼요.",
        title=f"신규 한도를 {limit:,}원으로 올릴까요?",
        tone="warning",
        facts=[*facts, ("바꿀 한도", f"**{current:,}원 → {limit:,}원**")],
        buttons=[_button(f"{limit:,}원으로 올리기", apply, discord.ButtonStyle.danger), _button("취소", keep)],
        hint="본인 확인을 마친 유저에게만 올려 주세요. 금액의 0 개수가 맞는지 확인해 주세요",
        owner_id=admin_id,
    )


# 관리자가 쓴 글 속의 "#채널이름". "<#id>", "a#b", "## 제목"은 해당 없음
_CHANNEL_REFERENCE = re.compile(r"(?<![<\w#])#([^\s#<>`*_~|]+)")
_CHANNEL_LINK = re.compile(r"<#\d+>")


def link_channels(text: str, guild: discord.Guild | None) -> tuple[str, list[str]]:
    """"#채널이름"을 눌리는 채널 링크로 바꾼다. (바꾼 글, 못 찾은 이름들)을 돌려준다.

    이름 뒤에 조사가 붙어도("#문의에 남겨 주세요") 되도록 가장 길게 맞는 채널 이름을 찾는다.
    """
    if guild is None:
        return text, []
    channels = {c.name: c for c in guild.channels if not isinstance(c, discord.CategoryChannel)}
    missing: list[str] = []

    def link(match: re.Match) -> str:
        word = match.group(1)
        for end in range(len(word), 0, -1):
            channel = channels.get(word[:end])
            if channel is not None:
                return channel.mention + word[end:]
        missing.append(f"#{word}")
        return match.group(0)

    return _CHANNEL_REFERENCE.sub(link, text), missing


def unlink_channels(text: str, guild: discord.Guild | None) -> str:
    """link_channels의 반대. 수정 창에 관리자가 쓴 그대로(#이름) 보여 주려고 쓴다.

    되돌린 글을 다시 링크로 바꿨을 때 원래와 달라지면(채널 이름이 바뀐 경우 등) 손대지 않는다.
    """
    if guild is None:
        return text
    names = {c.mention: c.name for c in guild.channels if not isinstance(c, discord.CategoryChannel)}
    plain = _CHANNEL_LINK.sub(lambda m: f"#{names[m.group(0)]}" if m.group(0) in names else m.group(0), text)
    return plain if link_channels(plain, guild)[0] == text else text


# 안내 문구 항목별 (이름, 입력 한도, 저장 한도).
# #이름이 20자쯤 되는 링크로 바뀌어 저장되므로 저장 한도가 더 크고, 셋을 합쳐도 메시지 한도(4000자) 안이다
DENY_NOTICE_PARTS = {
    "title": ("제목", 100, 200),
    "message": ("내용", 1500, 3000),
    "hint": ("작은 글씨", 300, 600),
}
# 디스코드 텍스트 입력 한도. 기본값이 max_length보다 길어도 거절된다
MAX_TEXT_INPUT = 4000


class DenyNoticeModal(BaseModal):
    def __init__(self, current: dict[str, str], guild: discord.Guild | None = None) -> None:
        super().__init__(title="입금자명 없는 유저에게 보일 안내")

        def prefill(key: str) -> dict:
            # 저장된 링크를 #이름으로 되돌리면 입력했을 때 길이와 같다. 지워진 채널처럼 못 되돌리는
            # 링크가 있으면 더 길어지는데, 그때는 입력 한도를 그 길이에 맞춰 늘린다
            text = unlink_channels(current[key], guild)[:MAX_TEXT_INPUT]
            return {"default": text, "max_length": max(DENY_NOTICE_PARTS[key][1], len(text))}

        self.title_input = ui.TextInput(label="제목", required=True, **prefill("title"))
        self.message_input = ui.TextInput(
            label="내용 (마크다운 가능 · #채널이름은 채널 링크로)",
            style=discord.TextStyle.paragraph,
            required=True,
            **prefill("message"),
        )
        self.hint_input = ui.TextInput(
            label="맨 아래 작은 글씨 (비우면 없음)",
            style=discord.TextStyle.paragraph,
            required=False,
            **prefill("hint"),
        )
        for item in (self.title_input, self.message_input, self.hint_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user.id):
            await deny_non_admin(interaction)
            return
        bot = get_bot(interaction)
        typed = {
            "title": self.title_input.value.strip(),
            "message": self.message_input.value.strip(),
            "hint": self.hint_input.value.strip(),
        }
        fields: dict[str, str] = {}
        missing: list[str] = []
        for key, text in typed.items():
            fields[key], unknown = link_channels(text, interaction.guild)
            missing += unknown
        if not fields["title"] or not fields["message"]:
            await reply_ephemeral(
                interaction, "제목과 내용은 비울 수 없어요.", title="안내 문구를 확인해 주세요",
            )
            return
        too_long = [
            (label, len(fields[key]), stored_max)
            for key, (label, _, stored_max) in DENY_NOTICE_PARTS.items() if len(fields[key]) > stored_max
        ]
        if too_long:
            await self._refuse_too_long(interaction, typed, too_long)
            return
        await bot.save_settings({DENY_NOTICE: json.dumps(fields, ensure_ascii=False)})
        await bot.events.record(
            "admin.deny_notice",
            f"<@{interaction.user.id}>님이 입금자명이 없는 유저에게 보이는 안내를 바꿨어요.",
            level="success",
        )
        # 유저가 보게 될 카드를 먼저 그대로 보여 주고, 그 아래에 결과를 알린다
        await interaction.response.send_message(view=deny_notice_view(fields), ephemeral=True)
        facts = [("찾지 못한 채널", ", ".join(dict.fromkeys(missing)))] if missing else ()
        await interaction.followup.send(view=notice(
            "위가 입금자명이 없는 유저가 봇 버튼을 누르면 보는 안내예요.",
            title="안내 문구를 바꿨어요",
            tone="warning" if missing else "success",
            facts=facts,
            hint=("찾지 못한 채널은 글자 그대로 보여요. 채널 이름을 정확히 적어 주세요\n" if missing else "")
            + "다시 고치려면 같은 명령을 실행하고, 처음 문구로 되돌리려면 `기본으로:True`",
        ), ephemeral=True)

    @staticmethod
    async def _refuse_too_long(
        interaction: discord.Interaction, typed: dict[str, str], too_long: list[tuple[str, int, int]]
    ) -> None:
        # 저장하지 않고, 입력한 내용 그대로 다시 열 수 있는 버튼을 준다
        async def reopen(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.send_modal(DenyNoticeModal(typed, button_interaction.guild))

        await reply_ephemeral(
            interaction,
            "#채널이름이 채널 링크(약 20자)로 바뀌면서 글이 길어져 저장하지 않았어요. "
            "채널 언급이나 글을 조금 줄여 주세요.",
            title="안내 문구가 너무 길어요",
            tone="warning",
            facts=[(label, f"링크로 바꾸면 {length:,}자 · 최대 {limit:,}자") for label, length, limit in too_long],
            buttons=[_button("다시 고치기", reopen, discord.ButtonStyle.primary)],
            hint="'다시 고치기'를 누르면 입력한 내용이 그대로 열려요. 지금 안내 문구는 바뀌지 않았어요",
            owner_id=interaction.user.id,
        )


def register_commands(tree: app_commands.CommandTree) -> None:

    @tree.command(name="패널", description="[관리자] 이 채널에 메인 패널(충전·송금 메뉴)을 올려요")
    @app_commands.default_permissions(OPERATOR_PERMISSIONS)
    @app_commands.guild_only()
    async def panel(interaction: discord.Interaction) -> None:
        # 올린 패널은 계속 남아 1분마다 갱신되므로 관리자만 올릴 수 있다
        if not is_admin(interaction.user.id):
            await deny_non_admin(interaction)
            return
        bot = get_bot(interaction)
        await interaction.response.send_message(view=bot.panel_view(interaction.guild))
        message = await interaction.original_response()
        await bot.db.add_panel(interaction.channel_id, message.id)
        if bot.panel_status is None:
            await bot.refresh_panels()

    @tree.command(name="잔액", description="내 잔액과 지금까지 충전·사용한 금액을 확인해요")
    async def balance(interaction: discord.Interaction) -> None:
        if not is_allowed(interaction.user.id):
            await deny_user(interaction)
            return
        bot = get_bot(interaction)
        user = await bot.db.get_user(interaction.user.id)
        view = BaseLayout(owner_id=interaction.user.id)
        view.add_item(card(
            figure("내 잔액", f"{user.balance:,}원"),
            divider(),
            ui.TextDisplay(rows(
                ("총 충전", f"{user.total_deposited:,}원"),
                ("총 사용", f"{user.total_spent:,}원"),
            )),
            ui.ActionRow(deposit_button()),
            subtext("코인 송금은 메인 패널에서 할 수 있어요"),
            tone="brand",
        ))
        await interaction.response.send_message(view=view, ephemeral=True)

    @tree.command(name="수수료", description="[관리자] 송금 수수료를 확인하거나 바꿔요")
    @app_commands.default_permissions(OPERATOR_PERMISSIONS)
    @app_commands.guild_only()
    @app_commands.rename(percent="퍼센트")
    @app_commands.describe(percent="새 수수료(%). 2.5를 넣으면 2.5%예요. 비워 두면 지금 수수료만 보여줘요")
    async def fee(
        interaction: discord.Interaction,
        percent: Optional[app_commands.Range[float, 0.0, MAX_FEE_PERCENT]] = None,
    ) -> None:
        if not is_admin(interaction.user.id):
            await deny_non_admin(interaction)
            return
        current = fee_percent(config.FEE_RATE)
        if percent is None:
            await reply_ephemeral(
                interaction,
                f"지금 송금 수수료는 **{current}**예요.",
                title="송금 수수료",
                tone="info",
                facts=[
                    ("바꾸는 방법", "`/수수료 퍼센트:2.5`"),
                    ("허용 범위", f"0% ~ {MAX_FEE_PERCENT:g}%"),
                ],
                hint="바꾼 값은 새 견적부터 적용되고, 봇을 재시작해도 유지돼요",
            )
            return
        rate = (Decimal(str(percent)) / 100).quantize(Decimal("0.0001"))
        if rate == Decimal(str(config.FEE_RATE)).quantize(Decimal("0.0001")):
            await reply_ephemeral(
                interaction, f"송금 수수료가 이미 **{current}**예요.", title="바뀐 게 없어요", tone="info",
            )
            return

        if rate < FEE_CONFIRM_BELOW:
            await send_ephemeral(interaction, _fee_confirm(interaction.user.id, rate, percent))
            return
        await _apply_fee(interaction, rate)

    @tree.command(name="김프", description="[관리자] 시장 김프에 더 붙이는 김프를 확인하거나 바꿔요")
    @app_commands.default_permissions(OPERATOR_PERMISSIONS)
    @app_commands.guild_only()
    @app_commands.rename(percent="퍼센트")
    @app_commands.describe(percent="시장 김프에 더할 김프(%). 0.4를 넣으면 0.4%예요. 비워 두면 지금 값만 보여줘요")
    async def premium(
        interaction: discord.Interaction,
        percent: Optional[app_commands.Range[float, 0.0, MAX_PREMIUM_PERCENT]] = None,
    ) -> None:
        if not is_admin(interaction.user.id):
            await deny_non_admin(interaction)
            return
        bot = get_bot(interaction)
        current = premium_percent(config.MIN_KIMCHI_PREMIUM_PCT)
        if percent is None:
            facts = []
            status = getattr(bot, "panel_status", None)
            if status is not None and status.btc_applied is not None and status.fx_rate is not None:
                facts.append(("지금 붙는 김프", f"+{status.btc_applied:.2f}% · 시장 {status.btc_premium:+.2f}%"))
                facts.append(("환율", f"₩{status.fx_rate:,.2f} ({status.fx_source})"))
            facts += [
                ("바꾸는 방법", "`/김프 퍼센트:0.4`"),
                ("허용 범위", f"0% ~ {MAX_PREMIUM_PERCENT:g}%"),
            ]
            await reply_ephemeral(
                interaction,
                f"시장 김프에 **{current}**를 더해서 팔아요.\n"
                "판매가 = 바이낸스 시세 × 환율 × (1 + 시장 김프 + 이 값) · 시장 김프가 마이너스면 0으로 봐요.",
                title="추가 김프",
                tone="info",
                facts=facts,
                hint="바꾼 값은 새 견적부터 적용되고, 봇을 재시작해도 유지돼요",
            )
            return
        pct = Decimal(str(percent)).quantize(Decimal("0.01"))
        if pct == Decimal(str(config.MIN_KIMCHI_PREMIUM_PCT)).quantize(Decimal("0.01")):
            await reply_ephemeral(
                interaction, f"추가 김프가 이미 **{current}**예요.", title="바뀐 게 없어요", tone="info",
            )
            return

        old = await bot.set_min_premium(pct)
        change = f"{premium_percent(old)} → {premium_percent(pct)}"
        notes = ["봇을 재시작해도 유지돼요"]
        if pct > Decimal(str(old)):
            notes.append(
                f"확인 화면을 띄워 둔 유저는 수령량이 {SLIPPAGE_TOLERANCE:.1%} 넘게 줄면 전송 전에 다시 확인해요"
            )
        await reply_ephemeral(
            interaction,
            "새 견적부터 바로 적용돼요. 시장 김프에 이 값을 더해서 팔아요.",
            title="추가 김프를 바꿨어요",
            tone="success",
            facts=[("변경", f"**{change}**")],
            hint="\n".join(notes),
        )
        await bot.events.record(
            "admin.premium",
            f"<@{interaction.user.id}>님이 추가 김프를 **{change}**로 바꿨어요.",
            level="success",
        )
        await _refresh_panels_quietly(bot)

    @tree.command(name="이익", description="[관리자] 고객 송금으로 번 이익을 봐요 (관리자 계정이 쓴 것은 제외)")
    @app_commands.default_permissions(OPERATOR_PERMISSIONS)
    @app_commands.guild_only()
    async def profit(interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user.id):
            await deny_non_admin(interaction)
            return
        await send_ephemeral(interaction, await build_profit_view(get_bot(interaction)))

    tree.add_command(AdminCommands())


def role_problem(role: discord.Role) -> tuple[str, str, str] | None:
    """봇이 이 역할을 줄 수 없거나 주면 안 되는 이유 (제목, 설명, 해결 방법). 문제없으면 None."""
    me = role.guild.me
    if role.is_default() or role.managed:
        return ("줄 수 없는 역할이에요",
                f"{role.mention}은 디스코드나 다른 봇이 관리하는 역할이에요.",
                "서버 설정 › 역할에서 만든 일반 역할을 골라 주세요")
    risky = [name for name in RISKY_ROLE_PERMISSIONS if getattr(role.permissions, name)]
    if risky:
        return ("관리 권한이 있는 역할이에요",
                f"{role.mention}에는 서버를 관리하는 권한이 있어 입금자명만으로 줄 수 없어요.",
                "관리 권한이 없는 역할을 만들어 골라 주세요")
    if not me.guild_permissions.manage_roles:
        return ("역할 관리 권한이 필요해요",
                "봇에게 역할 관리 권한이 없어 역할을 줄 수 없어요.",
                "서버 설정 › 역할에서 봇 역할에 '역할 관리'를 허용해 주세요")
    if role >= me.top_role:
        return ("봇 역할보다 높은 역할이에요",
                f"봇은 자기 역할({me.top_role.mention})보다 아래 있는 역할만 줄 수 있어요.",
                "서버 설정 › 역할에서 봇 역할을 이 역할보다 위로 끌어 올려 주세요")
    return None


def _list_card(title: str, lines: list[str], empty: str, footer: str) -> BaseLayout:
    if not lines:
        return notice(empty, title=title)
    view = BaseLayout()
    view.add_item(card(
        heading(title),
        divider(),
        ui.TextDisplay(join_limited(lines)),
        subtext(footer),
    ))
    return view


def _sendable(channel: discord.abc.GuildChannel) -> bool:
    perms = channel.permissions_for(channel.guild.me)
    return perms.view_channel and perms.send_messages


async def _refuse_unsendable(interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
    await reply_ephemeral(
        interaction,
        f"{channel.mention} 채널에 봇이 메시지를 보낼 수 없어요.",
        title="채널 권한이 필요해요",
        tone="error",
        hint="채널 설정 › 권한에서 봇에게 '채널 보기'와 '메시지 보내기'를 허용해 주세요",
    )


class AdminCommands(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(
            name="관리자",
            description="운영자 전용 · 입금자명, 잔액, 조회, 설정",
            default_permissions=OPERATOR_PERMISSIONS,
            guild_only=True,
            allowed_contexts=app_commands.AppCommandContext(guild=True),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user.id):
            return True
        await deny_non_admin(interaction)
        return False

    @app_commands.command(name="입금자명지정", description="유저에게 입금자명을 정해요 · 이 이름으로 입금하면 자동 충전돼요")
    @app_commands.rename(user="유저", name="입금자명")
    @app_commands.describe(
        user="입금자명을 정할 유저",
        name="은행 입금 문자에 찍히는 이름 그대로",
    )
    async def depositor_set(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        name: app_commands.Range[str, 1, 20],
    ) -> None:
        refuse_bot(user)
        # 역할 변경이 디스코드를 거쳐 3초를 넘길 수 있어 먼저 defer
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = get_bot(interaction)
        was_allowed = is_allowed(user.id)
        saved = await bot.deposits.assign_depositor_name(user.id, name, interaction.user.id)
        _, role_line = await bot.sync_depositor_role(user.id, grant=True)
        if user.id in config.BLOCKED_USERS:
            hint = "이용 정지 중인 유저예요. 쓰게 하려면 /관리자 이용정지해제 로 정지를 풀어 주세요"
        elif was_allowed:
            hint = None
        else:
            hint = "봇 사용 권한도 함께 줬어요. 이제 이 유저도 패널을 쓸 수 있어요"
        await reply_ephemeral(
            interaction,
            "이 이름으로 들어온 입금은 이 유저에게 자동으로 충전돼요.",
            title="입금자명을 지정했어요",
            tone="success",
            facts=[("유저", user.mention), ("입금자명", f"**{escape_markdown(saved)}**")]
            + ([("역할", role_line)] if role_line else []),
            hint=hint,
        )

    @app_commands.command(name="입금자명해제", description="유저의 입금자명을 지워요 · 이후 입금은 자동 충전되지 않아요")
    @app_commands.rename(user="유저")
    @app_commands.describe(user="입금자명을 지울 유저")
    async def depositor_remove(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = get_bot(interaction)
        if await bot.deposits.remove_depositor_name(user.id, interaction.user.id):
            _, role_line = await bot.sync_depositor_role(user.id, grant=False)
            await reply_ephemeral(
                interaction,
                f"{user.mention}님의 입금자명 지정을 해제했어요.",
                title="입금자명을 해제했어요",
                tone="success",
                facts=[("역할", role_line)] if role_line else (),
                hint="이 이름으로 들어온 입금은 이제 자동 충전되지 않아요\n"
                     "봇 사용 권한은 그대로예요. 이용을 막으려면 /관리자 이용정지",
            )
        else:
            await reply_ephemeral(
                interaction,
                f"{user.mention}님에게 지정된 입금자명이 없어요.",
                title="해제할 입금자명이 없어요",
                tone="info",
            )

    @app_commands.command(name="입금자명목록", description="입금자명을 정해 둔 유저 목록을 봐요")
    async def depositor_list(self, interaction: discord.Interaction) -> None:
        records = await get_bot(interaction).deposits.list_depositor_names()
        lines = [f"<@{r.discord_id}>　**{escape_markdown(r.name)}**" for r in records]
        await interaction.response.send_message(
            view=_list_card(
                "입금자명 목록",
                lines,
                empty="지정된 입금자명이 없어요.",
                footer=f"총 {len(lines)}명 · /관리자 입금자명지정 으로 추가해요",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="입금자역할설정", description="입금자명을 정한 유저에게 자동으로 줄 역할을 정해요")
    @app_commands.rename(role="역할")
    @app_commands.describe(role="줄 역할 · 비워 두면 자동 부여를 꺼요")
    async def depositor_role(
        self, interaction: discord.Interaction, role: Optional[discord.Role] = None
    ) -> None:
        bot = get_bot(interaction)
        if role is None:
            await bot.set_depositor_role(None)
            await bot.events.record(
                "admin.depositor_role",
                f"<@{interaction.user.id}>님이 입금자 역할 자동 부여를 껐어요.",
            )
            await reply_ephemeral(
                interaction,
                "이제 입금자명을 지정해도 역할을 주지 않아요.",
                title="역할 자동 부여를 껐어요",
                tone="success",
                hint="이미 받은 역할은 그대로 남아요. 다시 켜려면 역할을 골라 실행해 주세요",
            )
            return
        problem = role_problem(role)
        if problem:
            title, message, hint = problem
            await reply_ephemeral(interaction, message, title=title, tone="error", hint=hint)
            return

        await interaction.response.send_message(
            view=loading("이미 입금자명이 있는 유저에게 역할을 주는 중이에요"), ephemeral=True
        )
        await bot.set_depositor_role(role)
        given = skipped = 0
        failure = ""
        for record in await bot.deposits.list_depositor_names():
            ok, line = await bot.sync_depositor_role(record.discord_id, grant=True)
            if ok:
                given += 1
            elif ok is None:
                skipped += 1
            else:
                failure = line  # 같은 이유로 나머지도 실패할 테니 여기서 멈춘다
                break
        await bot.events.record(
            "admin.depositor_role",
            f"<@{interaction.user.id}>님이 입금자명을 지정하면 {role.mention} 역할을 주도록 설정했어요. "
            f"기존 유저 {given}명에게 역할을 줬어요.",
            level="warning" if failure else "success",
        )
        facts = [("역할", role.mention), ("기존 유저", f"{given}명 부여")]
        if skipped:
            facts.append(("건너뜀", f"{skipped}명 (서버에 없거나 이용 정지)"))
        if failure:
            facts.append(("중단", failure))
        await interaction.edit_original_response(view=notice(
            "이제 입금자명을 지정하면 이 역할을 자동으로 주고, 해제하면 거둬들여요.",
            title="입금자 역할을 설정했어요" if not failure else "역할을 다 주지 못했어요",
            tone="warning" if failure else "success",
            facts=facts,
            hint="문제를 고친 뒤 같은 명령을 다시 실행하면 남은 유저에게도 줘요" if failure
            else "다른 역할로 바꿔도 예전 역할은 그대로 남아요",
        ))

    @app_commands.command(name="구매자역할설정", description="코인을 산 유저에게 자동으로 줄 역할을 정해요")
    @app_commands.rename(role="역할", existing="기존구매자")
    @app_commands.describe(
        role="줄 역할 · 비워 두면 자동 부여를 꺼요",
        existing="이미 코인을 산 유저에게도 지금 줄지 (기본: 줘요)",
    )
    async def buyer_role(
        self, interaction: discord.Interaction, role: Optional[discord.Role] = None, existing: bool = True
    ) -> None:
        bot = get_bot(interaction)
        if role is None:
            await bot.set_buyer_role(None)
            await bot.events.record(
                "admin.buyer_role", f"<@{interaction.user.id}>님이 구매자 역할 자동 부여를 껐어요."
            )
            await reply_ephemeral(
                interaction,
                "이제 코인을 사도 역할을 주지 않아요.",
                title="구매자 역할 자동 부여를 껐어요",
                tone="success",
                hint="이미 받은 역할은 그대로 남아요. 다시 켜려면 역할을 골라 실행해 주세요",
            )
            return
        problem = role_problem(role)
        if problem:
            title, message, hint = problem
            await reply_ephemeral(interaction, message, title=title, tone="error", hint=hint)
            return

        await interaction.response.send_message(view=loading("구매자 역할을 설정하는 중이에요"), ephemeral=True)
        await bot.set_buyer_role(role)
        given = skipped = 0
        failure = ""
        if existing:
            for user_id in await bot.db.list_buyers():
                ok, line = await bot.grant_buyer_role(user_id)
                if ok:
                    given += 1
                elif ok is None:
                    skipped += 1
                else:
                    failure = line
                    break
        await bot.events.record(
            "admin.buyer_role",
            f"<@{interaction.user.id}>님이 코인을 사면 {role.mention} 역할을 주도록 설정했어요."
            + (f" 기존 구매자 {given}명에게 역할을 줬어요." if existing else ""),
            level="warning" if failure else "success",
        )
        facts = [("역할", role.mention)]
        if existing:
            facts.append(("기존 구매자", f"{given}명 부여"))
        if skipped:
            facts.append(("건너뜀", f"{skipped}명 (서버에 없거나 이용 정지)"))
        if failure:
            facts.append(("중단", failure))
        await interaction.edit_original_response(view=notice(
            "이제 코인 송금이 완료될 때마다(블록체인 전송까지 끝나면) 이 역할을 자동으로 줘요.",
            title="구매자 역할을 설정했어요" if not failure else "역할을 다 주지 못했어요",
            tone="warning" if failure else "success",
            facts=facts,
            hint="문제를 고친 뒤 같은 명령을 다시 실행하면 남은 유저에게도 줘요" if failure
            else "다른 역할로 바꿔도 예전 역할은 그대로 남아요. 끄려면 역할 없이 실행하세요",
        ))

    @app_commands.command(name="미등록안내설정", description="입금자명이 없는 유저가 봇 버튼을 누르면 보이는 안내를 바꿔요")
    @app_commands.rename(reset="기본으로")
    @app_commands.describe(reset="참이면 처음 안내 문구로 되돌려요")
    async def deny_notice(self, interaction: discord.Interaction, reset: bool = False) -> None:
        bot = get_bot(interaction)
        if reset:
            await bot.save_settings({DENY_NOTICE: None})
            await bot.events.record(
                "admin.deny_notice",
                f"<@{interaction.user.id}>님이 입금자명이 없는 유저에게 보이는 안내를 기본 문구로 되돌렸어요.",
            )
            await interaction.response.send_message(
                view=deny_notice_view(deny_notice_fields(None)), ephemeral=True
            )
            await interaction.followup.send(view=notice(
                "위가 입금자명이 없는 유저가 봇 버튼을 누르면 보는 안내예요.",
                title="기본 안내 문구로 되돌렸어요", tone="success",
            ), ephemeral=True)
            return
        await interaction.response.send_modal(
            DenyNoticeModal(deny_notice_fields(bot.log_settings), interaction.guild)
        )

    @app_commands.command(name="시세채널설정", description="재고·김프를 이름으로 보여 줄 음성 채널 2개를 정해요")
    @app_commands.rename(
        stock="재고채널", premium="김프채널", stock_format="재고이름", premium_format="김프이름",
    )
    @app_commands.describe(
        stock="이름에 보유 재고를 보여 줄 음성 채널 · 비우면 이 채널은 꺼요",
        premium="이름에 김프를 보여 줄 음성 채널 · 비우면 이 채널은 꺼요",
        stock_format="재고 채널 이름 모양 · 기본 '📦 재고 {재고}' · {재고} {김프} {USDT} {환율} 사용",
        premium_format="김프 채널 이름 모양 · 기본 '📈 김프 {김프}' · 비우면 지금 모양 그대로",
    )
    async def status_channels(
        self,
        interaction: discord.Interaction,
        stock: Optional[discord.VoiceChannel] = None,
        premium: Optional[discord.VoiceChannel] = None,
        stock_format: Optional[app_commands.Range[str, 1, 100]] = None,
        premium_format: Optional[app_commands.Range[str, 1, 100]] = None,
    ) -> None:
        bot = get_bot(interaction)
        if stock is not None and premium is not None and stock.id == premium.id:
            await reply_ephemeral(
                interaction, "재고와 김프는 서로 다른 음성 채널에 보여 줘야 해요.",
                title="같은 채널을 두 번 골랐어요",
            )
            return
        for channel in filter(None, (stock, premium)):
            perms = channel.permissions_for(channel.guild.me)
            if not (perms.view_channel and perms.manage_channels):
                await reply_ephemeral(
                    interaction,
                    f"{channel.mention} 채널 이름을 봇이 바꿀 수 없어요.",
                    title="채널 권한이 필요해요",
                    tone="error",
                    hint="채널 설정 › 권한에서 봇에게 '채널 보기'와 '채널 관리'를 허용해 주세요",
                )
                return
        for name_format in filter(None, (stock_format, premium_format)):
            if not any(placeholder in name_format for placeholder in STATUS_PLACEHOLDERS):
                await reply_ephemeral(
                    interaction,
                    f"`{name_format}`에는 바뀌는 값이 없어요. `{{재고}}`, `{{김프}}`, `{{USDT}}`, `{{환율}}` 중 "
                    "하나 이상을 넣어 주세요.",
                    title="채널 이름 모양을 확인해 주세요",
                    hint="예: 📦 재고 {재고} · 📈 김프 {김프}",
                )
                return

        values: dict[str, int | str | None] = {
            STATUS_STOCK_CHANNEL: stock.id if stock else None,
            STATUS_PREMIUM_CHANNEL: premium.id if premium else None,
        }
        if stock_format:
            values[STATUS_STOCK_FORMAT] = " ".join(stock_format.split())
        if premium_format:
            values[STATUS_PREMIUM_FORMAT] = " ".join(premium_format.split())
        await interaction.response.send_message(view=loading("음성 채널 이름을 맞추는 중이에요"), ephemeral=True)
        await bot.save_settings(values)
        if stock is None and premium is None:
            await bot.events.record(
                "admin.status_channels", f"<@{interaction.user.id}>님이 재고·김프 음성 채널 갱신을 껐어요."
            )
            await interaction.edit_original_response(view=notice(
                "이제 음성 채널 이름을 바꾸지 않아요.",
                title="재고·김프 음성 채널을 껐어요",
                tone="success",
                hint="채널 이름은 마지막 값 그대로 남아요",
            ))
            return
        # 새로 고른 채널은 바로 한 번 이름을 맞춘다
        try:
            await bot.refresh_panels()
        except Exception:
            log.warning("Refreshing after the status channel setup failed", exc_info=True)
        facts = []
        for channel, format_key, default in (
            (stock, STATUS_STOCK_FORMAT, DEFAULT_STOCK_FORMAT),
            (premium, STATUS_PREMIUM_FORMAT, DEFAULT_PREMIUM_FORMAT),
        ):
            if channel is None:
                continue
            name_format = bot.log_settings.get(format_key) or default
            shown = render_status_name(name_format, bot.panel_status)
            facts.append((channel.mention, f"`{name_format}` → **{escape_markdown(shown) if shown else '시세 확인 중'}**"))
        where = ", ".join(c.mention for c in (stock, premium) if c)
        await bot.events.record(
            "admin.status_channels",
            f"<@{interaction.user.id}>님이 재고·김프를 음성 채널 이름으로 보여 주도록 설정했어요 · {where}",
            level="success",
        )
        await interaction.edit_original_response(view=notice(
            "패널 시세가 갱신될 때마다 음성 채널 이름도 맞춰 바꿔요.",
            title="재고·김프 음성 채널을 설정했어요",
            tone="success",
            facts=facts,
            hint=(f"디스코드는 채널 이름을 10분에 2번까지만 바꿀 수 있어서, 채널마다 "
                  f"{STATUS_RENAME_INTERVAL // 60}분에 한 번 최신 값으로 바꿔요\n끄려면 채널 없이 실행하세요"),
        ))

    @app_commands.command(name="이용정지", description="유저의 충전·송금을 막아요 (잔액은 그대로 보관)")
    @app_commands.rename(user="유저", reason="사유")
    @app_commands.describe(user="이용을 막을 유저", reason="정지 사유 · 기록에 남아요")
    async def block(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: app_commands.Range[str, 1, 100],
    ) -> None:
        # 정지한 뒤 답장을 놓치면 정지가 실패한 것처럼 보이므로 먼저 defer
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = get_bot(interaction)
        newly = await bot.deposits.block_user(user.id, reason, interaction.user.id)
        _, role_line = await bot.sync_depositor_role(user.id, grant=False)
        record = await bot.db.get_block(user.id)
        balance = (await bot.db.get_user(user.id)).balance
        await reply_ephemeral(
            interaction,
            f"{user.mention}님은 이제 패널과 명령어를 쓸 수 없고, 열어 둔 화면의 버튼도 막혀요."
            if newly else f"{user.mention}님은 이미 정지돼 있어서 사유만 바꿨어요.",
            title="이용을 정지했어요" if newly else "정지 사유를 바꿨어요",
            tone="warning",
            facts=[
                ("유저", user.mention),
                ("사유", escape_markdown(record.reason) if record else "-"),
                ("남은 잔액", f"{balance:,}원 · 그대로 보관돼요"),
            ] + ([("역할", role_line)] if role_line else []),
            hint="바이낸스에 이미 접수된 송금은 멈출 수 없어요. 풀려면 /관리자 이용정지해제",
        )

    @app_commands.command(name="이용정지해제", description="정지한 유저가 다시 봇을 쓸 수 있게 해요")
    @app_commands.rename(user="유저")
    @app_commands.describe(user="정지를 풀 유저")
    async def unblock(self, interaction: discord.Interaction, user: discord.User) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = get_bot(interaction)
        if not await bot.deposits.unblock_user(user.id, interaction.user.id):
            await reply_ephemeral(
                interaction,
                f"{user.mention}님은 정지된 유저가 아니에요.",
                title="풀 정지가 없어요",
                tone="info",
            )
            return
        usable = is_allowed(user.id)
        role_line = ""
        if await bot.deposits.get_depositor_name(user.id):
            _, role_line = await bot.sync_depositor_role(user.id, grant=True)
        await reply_ephemeral(
            interaction,
            f"{user.mention}님이 다시 봇을 쓸 수 있어요." if usable
            else f"{user.mention}님의 정지를 풀었어요. 입금자명이 없어서 아직 봇을 쓸 수는 없어요.",
            title="이용 정지를 풀었어요",
            tone="success",
            facts=[("역할", role_line)] if role_line else (),
            hint=None if usable else "/관리자 입금자명지정 으로 입금자명을 지정하면 쓸 수 있어요",
        )

    @app_commands.command(name="신규한도", description="본인 확인을 마친 신규 유저의 24시간 송금 한도를 확인하거나 바꿔요")
    @app_commands.rename(user="유저", limit="한도", reset="기본으로")
    @app_commands.describe(
        user="한도를 보거나 바꿀 유저",
        limit="이 유저에게만 줄 신규 24시간 한도(원) · 비워 두면 지금 한도만 보여줘요",
        reset="참이면 따로 정한 한도를 지우고 기본 신규 한도로 되돌려요",
    )
    async def new_user_limit(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        limit: Optional[app_commands.Range[int, 1, MAX_OWN_LIMIT_KRW]] = None,
        reset: bool = False,
    ) -> None:
        refuse_bot(user)
        if limit is not None and reset:
            raise UserFacingError(
                "한도를 정하려면 `한도`만, 기본 한도로 되돌리려면 `기본으로`만 넣어 주세요.",
                title="둘 중 하나만 넣어 주세요",
                tone="warning",
            )
        bot = get_bot(interaction)
        default = config.NEW_USER_DAILY_LIMIT_KRW
        if reset:
            old = await bot.transfers.clear_new_user_limit(user.id, interaction.user.id)
            status = await bot.transfers.new_user_status(user.id)
            await reply_ephemeral(
                interaction,
                f"{user.mention}님에게 따로 정한 한도가 없어요." if old is None
                else f"{user.mention}님에게 따로 정했던 한도 **{old:,}원**을 지웠어요.",
                title="되돌릴 한도가 없어요" if old is None else "기본 신규 한도로 되돌렸어요",
                tone="info" if old is None else "success",
                facts=limit_facts(user.id, status),
            )
            return
        if limit is None:
            status = await bot.transfers.new_user_status(user.id)
            await reply_ephemeral(
                interaction,
                title="24시간 송금 한도",
                tone="info",
                facts=[("유저", user.mention), *limit_facts(user.id, status)],
                hint="신규 한도를 바꾸려면 `한도`에 금액을 넣어요 (예: `한도:1000000`)\n"
                     f"기본 신규 한도 {default:,}원으로 되돌리려면 `기본으로:참`",
            )
            return

        status = await bot.transfers.new_user_limit_to_change(user.id)
        current = status.own_limit.new_user_limit if status.own_limit else default
        if limit == current:
            await reply_ephemeral(
                interaction, f"{user.mention}님의 신규 한도가 이미 **{current:,}원**이에요.",
                title="바뀐 게 없어요", tone="info",
            )
            return
        if limit > current:
            await send_ephemeral(interaction, _limit_confirm(
                interaction.user.id, user, limit, current,
                [("유저", user.mention), *limit_facts(user.id, status)],
            ))
            return
        await _apply_new_user_limit(interaction, user, limit)

    @app_commands.command(name="서비스중지", description="모든 유저의 새 충전 요청과 송금을 멈춰요 (지급정지 같은 비상시)")
    @app_commands.rename(reason="사유")
    @app_commands.describe(reason="멈추는 이유 · 기록에 남아요")
    async def pause(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 100]) -> None:
        bot = get_bot(interaction)
        reason = " ".join(reason.split())
        if not reason:
            raise UserFacingError("나중에 확인할 수 있게 멈추는 이유를 적어 주세요.",
                                  title="사유가 비어 있어요", tone="warning")
        newly = await bot.deposits.pause_service(reason, interaction.user.id)
        pause = await bot.db.get_service_pause()
        await reply_ephemeral(
            interaction,
            "새 충전 요청과 송금을 모두 받지 않아요. 이미 바이낸스에 접수된 송금은 멈출 수 없어요."
            if newly else "이미 멈춰 있어서 그대로 뒀어요.",
            title="충전·송금을 멈췄어요" if newly else "이미 멈춰 있어요",
            tone="warning",
            facts=[
                ("사유", escape_markdown(str(pause.get("reason", "-")))),
                ("멈춘 시각", format_kst(pause.get("since"))),
            ] if pause else (),
            hint="다시 열려면 /관리자 서비스재개",
        )

    @app_commands.command(name="서비스재개", description="멈춘 충전·송금을 다시 열어요")
    async def resume(self, interaction: discord.Interaction) -> None:
        bot = get_bot(interaction)
        pause = await bot.deposits.resume_service(interaction.user.id)
        if pause is None:
            await reply_ephemeral(
                interaction, "충전과 송금은 지금 열려 있어요.", title="멈춘 상태가 아니에요", tone="info",
            )
            return
        await reply_ephemeral(
            interaction,
            "새 충전 요청과 송금을 다시 받아요.",
            title="충전·송금을 다시 열었어요",
            tone="success",
            facts=[
                ("멈췄던 사유", escape_markdown(str(pause.get("reason", "-")))),
                ("멈춘 시각", format_kst(pause.get("since"))),
            ],
            hint="입금 취소 문자로 자동 정지된 유저는 /관리자 이용정지해제 로 따로 풀어야 해요",
        )

    @app_commands.command(name="미확인입금목록", description="자동 충전되지 않은 입금 알림 목록을 봐요")
    async def unmatched(self, interaction: discord.Interaction) -> None:
        notifications = await get_bot(interaction).deposits.list_unmatched()
        lines = []
        for n in notifications:
            owner = f"<@{n.owner_id}>" if n.owner_id else "지정된 유저 없음"
            lines.append(
                f"`#{n.id}` `{format_kst(n.received_at, '%m-%d %H:%M')}`　"
                f"**{escape_markdown(n.depositor_name)}** {n.amount:,}원 → {owner}"
            )
        await interaction.response.send_message(
            view=_list_card(
                "미확인 입금",
                lines,
                empty="미확인 입금이 없어요.",
                footer="/관리자 미확인입금충전 으로 충전하거나 /관리자 미확인입금제외 로 목록에서 빼세요",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="미확인입금충전", description="미확인 입금을 골라 유저 잔액으로 충전해요")
    @app_commands.rename(notification_id="입금번호", user="유저")
    @app_commands.describe(
        notification_id="미확인입금목록에 나온 #번호",
        user="충전받을 유저",
    )
    async def resolve(
        self, interaction: discord.Interaction, notification_id: int, user: discord.User
    ) -> None:
        refuse_bot(user)
        await resolve_checked(interaction, notification_id, user.id, how="send")

    @app_commands.command(
        name="잔액충전",
        description=f"유저 잔액을 직접 충전해요 ({CREDIT_CONFIRM_FROM // 10_000}만 원 이상은 한 번 더 확인해요)",
    )
    @app_commands.rename(user="유저", amount="금액", reason="사유")
    @app_commands.describe(
        user="충전받을 유저",
        amount="충전할 금액 (원)",
        reason="충전 사유 · 기록에 남아요",
    )
    async def credit(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: app_commands.Range[int, 1, 100_000_000],
        reason: app_commands.Range[str, 1, 100],
    ) -> None:
        refuse_bot(user)
        bot = get_bot(interaction)
        # 아래 확인이 3초를 넘길 수 있어 먼저 답한다
        await interaction.response.send_message(view=loading("충전해도 되는지 확인하는 중이에요"), ephemeral=True)
        # 이미 받은 돈일 수 있는 경우: 같은 금액의 미확인 입금, 방금 한 같은 충전, 문자가 아직 안 온 충전 요청
        try:
            revision = await bot.db.credit_revision()
            warnings, pending = await bot.deposits.credit_warnings(user.id, amount)
        except Exception:
            log.exception("Checking a manual credit failed: user=%s amount=%s", user.id, amount)
            await show_result(interaction, check_failed())
            return
        if warnings or amount >= CREDIT_CONFIRM_FROM:
            balance = (await bot.db.get_user(user.id)).balance
            await interaction.edit_original_response(view=_credit_confirm(
                interaction.user.id, user, amount, reason, balance, warnings, pending,
                expected_revision=revision,
            ))
            return
        await _apply_credit(
            interaction, user, amount, reason, how="update", expected_revision=revision,
        )

    @app_commands.command(name="잔액차감", description="잘못 충전한 금액 등을 유저 잔액에서 빼요")
    @app_commands.rename(user="유저", amount="금액", reason="사유")
    @app_commands.describe(
        user="잔액을 뺄 유저 (잘못 충전한 봇 계정도 고를 수 있어요)",
        amount="뺄 금액 (원, 현재 잔액까지)",
        reason="차감 사유 · 기록에 남아요",
    )
    async def debit(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: app_commands.Range[int, 1, 100_000_000],
        reason: app_commands.Range[str, 1, 100],
    ) -> None:
        bot = get_bot(interaction)
        # 충전과 마찬가지로 먼저 답한다. 차감 뒤 답장이 실패하면 한 번 더 빼게 된다
        await interaction.response.send_message(
            view=working(f"{user.mention}님의 잔액에서 {amount:,}원을 빼는 중이에요"), ephemeral=True
        )
        try:
            new_balance = await bot.deposits.manual_debit(user.id, amount, reason, interaction.user.id)
        except UserFacingError as exc:
            await show_result(interaction, error_notice(exc))
            return
        except Exception:
            log.exception("Manual debit failed: user=%s amount=%s", user.id, amount)
            await show_result(interaction, unclear_result("잔액 차감"))
            return
        reason = escape_markdown(" ".join(reason.split()))
        await show_result(interaction, notice(
            f"{user.mention}님의 잔액에서 **{amount:,}원**을 뺐어요.",
            title="잔액에서 뺐어요",
            tone="success",
            facts=[
                ("유저", user.mention),
                ("뺀 금액", f"**{amount:,}원**"),
                ("사유", reason),
                ("현재 잔액", f"{new_balance:,}원"),
            ],
            hint="사유는 운영 기록에 남아요" if user.bot
            else "유저에게 잔액 조정 DM을 보내요. 사유는 운영 기록에 남아요",
        ))
        await bot.notify_user(
            user.id,
            message="관리자가 잔액을 조정했어요.",
            title="잔액이 조정됐어요",
            tone="warning",
            facts=[("조정 금액", f"**-{amount:,}원**"), ("사유", reason), ("현재 잔액", f"{new_balance:,}원")],
            hint="궁금한 점은 관리자에게 문의해 주세요",
        )

    @app_commands.command(name="로그채널설정", description="충전·송금·오류 기록을 받을 채널을 정해요")
    @app_commands.rename(channel="채널", alert_channel="경고채널", mention="멘션역할")
    @app_commands.describe(
        channel="모든 기록(충전·송금·관리자 조작·오류)을 받을 채널",
        alert_channel="경고·오류만 따로 받을 채널 (선택)",
        mention="오류나 처리할 일이 생기면 멘션할 역할 (선택)",
    )
    async def log_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        alert_channel: discord.TextChannel | None = None,
        mention: discord.Role | None = None,
    ) -> None:
        bot = get_bot(interaction)
        for target in filter(None, (channel, alert_channel)):
            if not _sendable(target):
                await _refuse_unsendable(interaction, target)
                return
        left = await bot.set_log_channels(
            channel.id,
            alert_channel.id if alert_channel else None,
            mention.id if mention else None,
        )
        where = f"{channel.mention}" + (f", 경고·오류는 {alert_channel.mention}에도" if alert_channel else "")
        event = await bot.events.record(
            "admin.log_setup",
            f"<@{interaction.user.id}>님이 운영 로그를 {where} 보내도록 설정했어요."
            + (f" 처리가 필요한 일은 {mention.mention} 역할을 멘션해요." if mention else "")
            + (f"\n이제 기록을 받지 않는 채널: {', '.join(f'<#{c}>' for c in sorted(left))}" if left else ""),
            level="warning" if left else "success",
        )
        await reply_ephemeral(
            interaction,
            "이제 충전·송금·관리자 조작·오류 기록이 이 채널로 올라가요.",
            title="운영 로그 채널을 설정했어요",
            tone="success",
            facts=[
                ("모든 기록", channel.mention),
                ("경고·오류", alert_channel.mention if alert_channel else "같은 채널에만"),
                ("멘션 역할", mention.mention if mention else "없음"),
            ],
            hint="방금 설정 기록이 채널에 올라갔는지 확인해 주세요"
            + ("\n이전 채널과 다른 관리자에게도 바뀐 사실을 알렸어요" if left else ""),
        )
        if left and event is not None:
            # 답장부터 보낸 뒤에 알린다 (3초 제한)
            await bot.announce_log_move(event, left, interaction.user.id)

    @app_commands.command(name="구매알림설정", description="코인 구매 알림을 올릴 채널을 정해요 (비우면 꺼져요)")
    @app_commands.rename(channel="채널", show_buyer="구매자")
    @app_commands.describe(
        channel="구매 알림을 올릴 채널 · 비워 두면 구매 알림을 꺼요",
        show_buyer="구매한 유저를 보여줄지 (기본: 보여주기)",
    )
    @app_commands.choices(show_buyer=[
        app_commands.Choice(name="보여주기 (멘션 알림은 가지 않아요)", value="show"),
        app_commands.Choice(name="숨기기", value="hide"),
    ])
    async def purchase_channel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        show_buyer: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        bot = get_bot(interaction)
        shown = show_buyer is None or show_buyer.value == "show"
        if channel is None:
            await bot.set_purchase_channel(None, shown)
            await bot.events.record(
                "admin.purchase_setup", f"<@{interaction.user.id}>님이 구매 알림을 껐어요."
            )
            await reply_ephemeral(
                interaction,
                "이제 구매 알림을 따로 올리지 않아요.",
                title="구매 알림을 껐어요",
                tone="success",
                hint="송금 기록은 운영 로그 채널에 그대로 남아요",
            )
            return
        if not _sendable(channel):
            await _refuse_unsendable(interaction, channel)
            return
        await bot.set_purchase_channel(channel.id, shown)
        await bot.events.record(
            "admin.purchase_setup",
            f"<@{interaction.user.id}>님이 구매 알림을 {channel.mention}에 올리도록 설정했어요"
            + (" (구매자 표시)." if shown else " (구매자 숨김)."),
            level="success",
        )
        await reply_ephemeral(
            interaction,
            "송금이 바이낸스에 접수될 때마다 이 채널에 구매 알림이 올라가요.",
            title="구매 알림 채널을 설정했어요",
            tone="success",
            facts=[
                ("채널", channel.mention),
                ("구매자", "표시 (멘션 알림 없음)" if shown else "숨김"),
                ("올라가는 내용", "코인·수량·네트워크·결제 금액"),
            ],
            hint="지갑 주소와 TXID는 올리지 않아요. 운영 로그 채널에도 기록은 그대로 남아요",
        )

    @app_commands.command(name="로그조회", description="운영 기록을 중요도·종류·유저별로 찾아봐요")
    @app_commands.rename(level="중요도", category="종류", user="유저")
    @app_commands.describe(level="볼 기록의 중요도", category="볼 기록의 종류", user="이 유저와 관련된 기록만")
    @app_commands.choices(
        level=[
            app_commands.Choice(name="전체", value="all"),
            app_commands.Choice(name="경고·오류", value="alert"),
            app_commands.Choice(name="오류만", value="error"),
        ],
        category=[app_commands.Choice(name=label, value=key) for key, label in CATEGORIES.items()],
    )
    async def logs(
        self,
        interaction: discord.Interaction,
        level: app_commands.Choice[str] | None = None,
        category: app_commands.Choice[str] | None = None,
        user: discord.User | None = None,
    ) -> None:
        await send_log_list(interaction, LogFilter(
            level=level.value if level else "all",
            category=category.value if category else None,
            user_id=user.id if user else None,
        ))

    @app_commands.command(name="유저조회", description="유저의 잔액·입금자명·충전·송금 내역을 한눈에 봐요")
    @app_commands.rename(user="유저")
    @app_commands.describe(user="확인할 유저")
    async def user_info(self, interaction: discord.Interaction, user: discord.User) -> None:
        await interaction.response.send_message(view=loading("유저 정보를 불러오는 중이에요"), ephemeral=True)
        view = await build_user_view(get_bot(interaction), user.id, interaction.user.id)
        await interaction.edit_original_response(view=view)

    @app_commands.command(name="송금조회", description="송금 한 건을 자세히 보고 바이낸스 기록과 대조해요")
    @app_commands.rename(tx_id="송금번호")
    @app_commands.describe(tx_id="송금 번호 (#숫자)")
    async def tx(self, interaction: discord.Interaction, tx_id: app_commands.Range[int, 1]) -> None:
        await interaction.response.send_message(view=loading("송금 내역을 확인하는 중이에요"), ephemeral=True)
        view = await build_tx_view(get_bot(interaction), tx_id, interaction.user.id)
        await interaction.edit_original_response(view=view)

    @app_commands.command(name="운영상태", description="봇·거래소 연결, 자금, 처리 대기 건수를 확인해요")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(view=loading("상태를 확인하는 중이에요"), ephemeral=True)
        view = await build_status_view(get_bot(interaction), interaction.user.id)
        await interaction.edit_original_response(view=view)

    @app_commands.command(name="미확인입금제외", description="봇과 상관없는 입금 알림을 미확인 목록에서 빼요")
    @app_commands.rename(notification_id="입금번호")
    @app_commands.describe(notification_id="뺄 입금 알림의 #번호")
    async def dismiss(self, interaction: discord.Interaction, notification_id: int) -> None:
        notification = await get_bot(interaction).deposits.dismiss_notification(
            notification_id, interaction.user.id
        )
        await send_ephemeral(interaction, dismissed_view(notification))
