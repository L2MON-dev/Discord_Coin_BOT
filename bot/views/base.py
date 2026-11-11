from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Sequence

import discord
from discord import ui

from .. import config
from ..errors import UserFacingError
from ..kst import KST, format_kst, to_kst  # noqa: F401
from ..services.event_log import SKIP_EVENT, exception_details

if TYPE_CHECKING:
    from ..bot import CoinBot

log = logging.getLogger(__name__)

VIEW_TIMEOUT = 15 * 60
# 디스코드는 메시지 하나의 글자를 4000자까지만 받는다. 목록 하나에는 3000자까지만 쓴다
MESSAGE_TEXT_LIMIT = 4000
TEXT_BUDGET = 3000

# 색은 상태를 알릴 때만 쓴다. info는 띠 없는 기본 카드
TONES = {
    "brand": config.BRAND_COLOR,
    "success": config.SUCCESS_COLOR,
    "warning": config.WARNING_COLOR,
    "error": config.ERROR_COLOR,
    "info": None,
}

Fact = tuple[str, str]


def get_bot(interaction: discord.Interaction) -> CoinBot:
    return interaction.client  # type: ignore[return-value]


def is_allowed(user_id: int) -> bool:
    if user_id in config.ADMIN_USERS:
        return True
    return user_id in config.ALLOWED_USERS and user_id not in config.BLOCKED_USERS


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_USERS


def refuse_bot(user: discord.abc.User) -> None:
    if user.bot:
        raise UserFacingError(
            "충전과 입금자명은 사람 계정에만 줄 수 있어요.",
            title="봇 계정은 고를 수 없어요",
            tone="warning",
            facts=[("고른 계정", user.mention)],
        )


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def code(text: str) -> str:
    # 유저가 넣은 백틱이 코드 표시를 깨지 않게 바꿔 둔다
    return f"`{text.replace('`', 'ˋ')}`"


def join_limited(lines: list[str], sep: str = "\n", limit: int = TEXT_BUDGET) -> str:
    """limit을 넘기 전까지만 잇고 나머지는 '외 N개'로 줄인다."""
    text = ""
    for index, line in enumerate(lines):
        candidate = f"{text}{sep}{line}" if text else line
        if len(candidate) > limit - 20:
            if not text:
                return truncate(line, limit)
            return f"{text}{sep}… 외 {len(lines) - index}개"
        text = candidate
    return text


def _percent(value: Decimal) -> str:
    text = f"{value:.2f}"
    return (text[:-1] if text.endswith("0") else text) + "%"


def fee_percent(rate: float | Decimal) -> str:
    # 0.02 -> "2.0%", 0.0225 -> "2.25%"
    return _percent(Decimal(str(rate)) * 100)


def premium_percent(pct: float | Decimal) -> str:
    # 이미 퍼센트 단위인 값: 0.05 -> "0.05%"
    return _percent(Decimal(str(pct)))


def rows(*pairs: Fact) -> str:
    return "\n".join(f"{label}　{value}" for label, value in pairs)


def timestamp(moment: datetime, style: str = "R") -> str:
    return f"<t:{int(moment.timestamp())}:{style}>"


def heading(text: str, sub: str | None = None) -> ui.TextDisplay:
    return ui.TextDisplay(f"### {text}" + (f"\n{sub}" if sub else ""))


def figure(label: str, value: str) -> ui.TextDisplay:
    """카드에서 가장 중요한 숫자 하나. 작은 라벨 아래 크게 보여 준다."""
    return ui.TextDisplay(f"-# {label}\n## {value}")


def subtext(text: str) -> ui.TextDisplay:
    return ui.TextDisplay("\n".join(f"-# {line}" for line in text.split("\n")))


def divider() -> ui.Separator:
    return ui.Separator(spacing=discord.SeparatorSpacing.small)


def card(*children: ui.Item, tone: str = "info") -> ui.Container:
    return ui.Container(*children, accent_colour=TONES.get(tone))


class BaseLayout(ui.LayoutView):
    """owner_id를 주면 그 유저만 누를 수 있다."""

    def __init__(
        self, *, timeout: float | None = VIEW_TIMEOUT, owner_id: int | None = None
    ) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_allowed(interaction.user.id):
            await deny_user(interaction)
            return False
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await reply_ephemeral(
                interaction,
                "다른 사람이 연 화면은 누를 수 없어요.",
                title="내 화면이 아니에요",
                tone="error",
                hint="패널에서 직접 시작해 주세요",
            )
            return False
        return True

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: ui.Item
    ) -> None:
        await show_error(interaction, error, context=type(self).__name__)


class PersistentLayout(BaseLayout):
    # 재시작 뒤에도 눌리는 화면: timeout 없이 고정 custom_id만 쓴다
    def __init__(self) -> None:
        super().__init__(timeout=None)


def notice(
    message: str | None = None,
    *,
    title: str,
    tone: str = "info",
    amount: Fact | None = None,
    facts: Sequence[Fact] = (),
    buttons: Sequence[ui.Button] = (),
    hint: str | None = None,
    owner_id: int | None = None,
) -> BaseLayout:
    """안내 카드 하나. 위에서부터 제목·본문, 큰 숫자, 항목, 버튼, 작은 글씨."""
    items: list[ui.Item] = [heading(title, message)]
    if amount:
        items.append(figure(*amount))
    if facts:
        items += [divider(), ui.TextDisplay(rows(*facts))]
    if buttons:
        items.append(ui.ActionRow(*buttons))
    if hint:
        items.append(subtext(hint))
    view = BaseLayout(owner_id=owner_id)
    view.add_item(card(*items, tone=tone))
    return view


def error_notice(error: UserFacingError) -> BaseLayout:
    return notice(str(error), title=error.title, tone=error.tone, facts=error.facts, hint=error.hint)


def loading(message: str) -> BaseLayout:
    view = BaseLayout()
    view.add_item(card(ui.TextDisplay(f"{message}…")))
    return view


def working(message: str) -> BaseLayout:
    """관리자가 잔액을 바꾸는 동안 띄우는 카드. 화면이 안 바뀌어도 이미 반영됐을 수 있다."""
    view = BaseLayout()
    view.add_item(card(
        ui.TextDisplay(f"{message}…"),
        subtext("화면이 바뀌지 않아도 다시 실행하지 마세요. 결과는 로그 채널이나 `/관리자 유저조회`에서 확인해요"),
    ))
    return view


async def answer(interaction: discord.Interaction, view: ui.LayoutView, *, how: str) -> None:
    """send: 새 비공개 답장 / edit: 버튼이 붙은 메시지 수정 / update: 이미 보낸 답장 수정"""
    if how == "send":
        await interaction.response.send_message(view=view, ephemeral=True)
    elif how == "edit":
        await interaction.response.edit_message(view=view)
    elif how == "update":
        await interaction.edit_original_response(view=view)
    else:
        raise ValueError(f"unknown answer: {how}")


def check_failed() -> BaseLayout:
    # 충전 전 확인 단계에서 난 오류. 아무것도 안 바뀌었으니 다시 해도 된다
    return notice(
        "충전해도 되는지 확인하다 오류가 났어요. 아무것도 충전하지 않았어요.",
        title="확인하지 못했어요",
        tone="error",
        hint="잠시 후 다시 시도해 주세요. 오류는 로그 채널에 남았어요",
    )


def unclear_result(what: str) -> BaseLayout:
    # 잔액 변경 도중 난 오류. 반영됐을 수도 있어서 재시도하라고 하면 안 된다
    return notice(
        f"{what} 중 예상하지 못한 오류가 났어요. 이미 반영됐을 수도 있어요.",
        title="결과를 먼저 확인해 주세요",
        tone="error",
        hint="다시 하기 전에 `/관리자 유저조회`로 잔액과 기록을 확인하세요. 오류는 로그 채널에 남았어요",
    )


async def quietly(step: Awaitable[Any], what: str) -> None:
    """잔액이 이미 바뀐 뒤의 후속 작업용. 실패해도 로그만 남긴다.

    여기서 예외를 올리면 '다시 시도' 카드가 떠서 같은 충전을 두 번 하게 된다.
    """
    try:
        await step
    except Exception:
        log.warning("%s failed after a balance change", what, exc_info=True)


async def show_result(interaction: discord.Interaction, view: ui.LayoutView) -> None:
    """잔액 변경 결과를 보여 준다. 수정이 안 되면 새 메시지로. 절대 예외를 올리지 않는다."""
    try:
        await interaction.edit_original_response(view=view)
        return
    except Exception:
        log.warning("Could not update the reply; sending the result separately",
                    exc_info=True, extra={SKIP_EVENT: True})
    try:
        await interaction.followup.send(view=view, ephemeral=True)
    except Exception:
        log.warning("Could not show the result of a balance change", exc_info=True, extra={SKIP_EVENT: True})


async def send_ephemeral(interaction: discord.Interaction, view: ui.LayoutView) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


async def reply_ephemeral(
    interaction: discord.Interaction,
    message: str | None = None,
    *,
    title: str,
    tone: str = "warning",
    **extra: Any,
) -> None:
    await send_ephemeral(interaction, notice(message, title=title, tone=tone, **extra))


# 권한 없는 유저에게 보이는 안내. /관리자 미등록안내설정으로 바꾸면 settings에 JSON으로 저장된다
DENY_NOTICE = "deny_notice"
DENY_NOTICE_DEFAULT = {
    "title": "아직 이용 권한이 없어요",
    "message": "관리자가 입금자명을 지정해 주면 바로 쓸 수 있어요.",
    "hint": "은행 입금 때 표시될 이름을 관리자에게 알려 주세요",
}


def deny_notice_fields(settings: dict | None) -> dict[str, str]:
    """저장된 문구가 있으면 그걸, 없거나 깨졌으면 기본 문구. hint만 빈 값 저장을 허용한다."""
    fields = dict(DENY_NOTICE_DEFAULT)
    raw = (settings or {}).get(DENY_NOTICE)
    if not raw:
        return fields
    try:
        saved = json.loads(raw)
    except ValueError:
        return fields
    if isinstance(saved, dict):
        for key in fields:
            value = saved.get(key)
            if isinstance(value, str) and (value.strip() or key == "hint"):
                fields[key] = value
    return fields


def deny_notice_view(fields: dict[str, str]) -> BaseLayout:
    return notice(
        fields["message"], title=fields["title"], tone="warning", hint=fields["hint"].strip() or None
    )


async def deny_user(interaction: discord.Interaction) -> None:
    if interaction.user.id in config.BLOCKED_USERS:
        await reply_ephemeral(
            interaction,
            "관리자가 이 계정의 봇 이용을 멈췄어요. 잔액은 그대로 보관돼요.",
            title="이용이 정지된 계정이에요",
            tone="error",
            hint="관리자에게 문의해 주세요",
        )
        return
    settings = getattr(interaction.client, "log_settings", None)
    await send_ephemeral(interaction, deny_notice_view(deny_notice_fields(settings)))


async def deny_non_admin(interaction: discord.Interaction) -> None:
    await reply_ephemeral(
        interaction,
        "관리자로 등록된 계정만 쓸 수 있어요.",
        title="관리자 전용 기능이에요",
        tone="error",
    )


async def show_error(
    interaction: discord.Interaction, error: BaseException, *, context: str
) -> None:
    """UserFacingError는 그대로 보여 주고, 그 밖의 오류는 내용을 숨기고 운영 로그에 남긴다."""
    if isinstance(error, UserFacingError):
        view = error_notice(error)
    else:
        log.error("%s failed", context, exc_info=error, extra={SKIP_EVENT: True})
        view = notice(
            "요청을 처리하다 오류가 났어요. 잠시 후 다시 시도해 주세요.",
            title="문제가 생겼어요",
            tone="error",
            hint="계속되면 관리자에게 알려 주세요",
        )
        events = getattr(interaction.client, "events", None)
        if events is not None:
            await events.record(
                "system.ui_error",
                f"`{context}` 처리 중 오류가 났어요. 유저에게는 '잠시 후 다시 시도' 안내가 나갔어요.",
                level="error",
                user_id=interaction.user.id,
                details={"화면": context, **exception_details(error)},
                dedup=f"ui:{context}:{type(error).__name__}:{interaction.user.id}",
            )
    try:
        await send_ephemeral(interaction, view)
    except discord.HTTPException:
        pass


class BaseModal(ui.Modal):
    # custom_id를 고정하면 안 된다. discord.py가 모달을 custom_id로만 찾아서
    # 한 유저의 제출이 다른 유저의 모달로 들어간다
    def __init__(self, *, title: str) -> None:
        super().__init__(title=title, timeout=VIEW_TIMEOUT)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_allowed(interaction.user.id):
            await deny_user(interaction)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await show_error(interaction, error, context=type(self).__name__)
