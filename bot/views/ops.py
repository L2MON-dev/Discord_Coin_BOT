from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import discord
from discord import ui
from discord.utils import escape_markdown

from .base import (
    KST, MESSAGE_TEXT_LIMIT, BaseLayout, Fact, answer, card, check_failed, code, deny_non_admin, divider,
    error_notice, fee_percent, figure, format_kst, get_bot, heading, is_admin, is_allowed,
    join_limited, loading, notice, premium_percent, quietly, refuse_bot, reply_ephemeral, rows,
    show_error, show_result, subtext, timestamp, to_kst, truncate, unclear_result, working,
)
from .deposit import deposit_complete_view
from .history import STATUS_LABELS, explorer_url, needs_lookup
from .main_panel import build_panel_status
from .. import config
from ..database import (
    NOTIFICATION_STATUS, SYSTEM_ACTOR, Event, NewUserStatus, ProfitTotals, Transaction,
)
from ..errors import UserFacingError
from ..services.monitor import backup_status

log = logging.getLogger(__name__)

LEVEL_LABELS = {"info": "정보", "success": "완료", "warning": "주의", "error": "오류"}
LEVEL_FILTERS = {
    "all": None,
    "alert": ("warning", "error"),
    "error": ("error",),
}
CATEGORIES = {
    "deposit": "충전",
    "transfer": "송금",
    "admin": "관리자 조작",
    "webhook": "입금 알림 API",
    "health": "상태 감시",
    "system": "시스템·오류",
    "summary": "일일 요약",
}
KIND_TITLES = {
    "system.start": "봇 시작",
    "system.stop": "봇 종료",
    "system.error": "오류 발생",
    "system.ui_error": "화면 처리 오류",
    "system.dm_failed": "DM 전송 실패",
    "system.purchase_failed": "구매 알림 전송 실패",
    "system.buyer_role_failed": "구매자 역할 부여 실패",
    "system.depositor_role_failed": "입금자 역할 부여 실패",
    "system.status_channel": "재고·김프 음성 채널 갱신 실패",
    "deposit.request": "충전 요청",
    "deposit.cancel": "충전 요청 취소",
    "deposit.expire": "충전 요청 마감",
    "deposit.credit": "자동 충전 완료",
    "deposit.unmatched": "미확인 입금",
    "deposit.reversal": "입금 취소·반환 문자",
    "deposit.duplicate": "중복 입금 문자",
    "deposit.sms_unparsed": "입금 문자 해석 실패",
    "deposit.wrong_account": "다른 계좌 입금 문자",
    "deposit.rate_limited": "충전 요청 제한",
    "deposit.hold_limited": "입금 없는 충전 요청 반복 제한",
    "deposit.pause": "지급정지로 충전·송금 멈춤",
    "deposit.verified": "본인인증 · 입금자명 자동 지정",
    "deposit.verify_conflict": "본인인증 · 입금자명이 이미 사용 중",
    "deposit.verify_invalid": "본인인증 · 입금자명 자동 지정 불가",
    "deposit.verify_removed": "본인인증 · 해제한 입금자명은 자동 지정 안 함",
    "webhook.bad_key": "잘못된 키로 입금 알림 요청",
    "webhook.bad_link": "서명이 틀린 본인인증 완료 알림",
    "webhook.invalid": "형식이 잘못된 입금 알림",
    "webhook.insecure": "http로 들어온 입금 알림",
    "transfer.request": "송금 요청",
    "transfer.convert": "USD → USDT 전환",
    "transfer.buy": "코인 자동 매수",
    "transfer.submit": "출금 접수",
    "transfer.complete": "출금 완료",
    "transfer.reject": "송금 실패·환불",
    "transfer.reject_limited": "출금 거절 반복 제한",
    "transfer.refund": "송금 환불",
    "transfer.unknown": "송금 결과 확인 불가",
    "transfer.overdue": "송금 최종 결과 확인 지연",
    "transfer.funds_low": "매수 자금 부족",
    "transfer.global_limit": "전체 송금 상한 도달",
    "transfer.partial_buy": "자동 매수 일부 체결",
    "transfer.buy_rejected": "자동 매수 거절",
    "transfer.error": "송금 처리 오류",
    "transfer.state_conflict": "송금 상태 충돌",
    "admin.credit": "수동 충전",
    "admin.debit": "잔액 차감",
    "admin.resolve": "미확인 입금 처리",
    "admin.dismiss": "입금 알림 제외",
    "admin.depositor_set": "입금자명 지정",
    "admin.depositor_remove": "입금자명 해제",
    "admin.log_setup": "로그 채널 설정",
    "admin.purchase_setup": "구매 알림 채널 설정",
    "admin.depositor_role": "입금자 역할 설정",
    "admin.buyer_role": "구매자 역할 설정",
    "admin.deny_notice": "미등록 안내 문구 변경",
    "admin.status_channels": "재고·김프 음성 채널 설정",
    "admin.fee": "수수료 변경",
    "admin.premium": "추가 김프 변경",
    "admin.block": "이용 정지",
    "admin.unblock": "이용 정지 해제",
    "admin.new_user_limit": "신규 한도 변경",
    "admin.pause": "충전·송금 멈춤",
    "admin.resume": "충전·송금 다시 열기",
    "health.binance": "바이낸스 연결",
    "health.prices": "시세 수신",
    "health.funds": "매수 자금",
    "summary.daily": "일일 요약",
}
# 사람이 직접 봐야 하는 일. 이 종류와 error 등급은 알림 역할을 멘션한다
NEEDS_ACTION = {
    "deposit.unmatched",
    "deposit.reversal",
    "deposit.pause",
    "deposit.sms_unparsed",
    "deposit.wrong_account",
    # 인증은 끝났는데 관리자가 이름을 지정해 줘야 쓸 수 있는 유저
    "deposit.verify_conflict",
    "deposit.verify_invalid",
    "transfer.unknown",
    "transfer.funds_low",
    "transfer.global_limit",
    "transfer.partial_buy",
    "transfer.buy_rejected",
    "transfer.error",
    "transfer.state_conflict",
}
DEPOSIT_STATUS = {
    "pending": "입금 대기",
    "completed": "완료",
    "manual": "수동 충전으로 마감",
    "expired": "마감",
    "cancelled": "취소",
}
REF_LABELS = {"tx": "송금 #{}", "deposit": "충전 요청 #{}", "notification": "입금 알림 #{}"}
TRACEBACK_LIMIT = 2500
LOG_PAGE_SIZE = 10
_LEVEL_TONES = {"success": "success", "warning": "warning", "error": "error"}


def kind_title(kind: str) -> str:
    return KIND_TITLES.get(kind, kind)


def event_details(event: Event) -> dict:
    if not event.details:
        return {}
    try:
        data = json.loads(event.details)
    except ValueError:
        return {"내용": event.details}
    return data if isinstance(data, dict) else {"내용": data}


def _when(event: Event, style: str = "f") -> str:
    moment = to_kst(event.created_at)
    return timestamp(moment, style) if moment else event.created_at


def _now_kst() -> str:
    return datetime.now(KST).strftime("%m-%d %H:%M")


def _short(utc_text: str | None, fmt: str = "%m-%d %H:%M") -> str:
    return format_kst(utc_text, fmt)


def _level(event: Event) -> str:
    return LEVEL_LABELS.get(event.level, event.level)


def _tx_status(tx: Transaction) -> str:
    return STATUS_LABELS.get(tx.status, tx.status)


def _first_line(text: str, limit: int = 90) -> str:
    line = next((part for part in text.splitlines() if part.strip()), "")
    return truncate(line.replace("**", ""), limit)


def _ref_facts(event: Event, *, with_id: bool = False) -> list[Fact]:
    facts: list[Fact] = []
    if event.user_id:
        user = f"<@{event.user_id}>"
        facts.append(("유저", f"{user} · `{event.user_id}`" if with_id else user))
    if event.ref_type in REF_LABELS and event.ref_id:
        facts.append(("대상", REF_LABELS[event.ref_type].format(event.ref_id)))
    return facts


def _short_facts(event: Event) -> list[Fact]:
    facts = _ref_facts(event)
    for key, value in event_details(event).items():
        text = str(value)
        if key == "traceback" or len(text) > 80 or len(facts) >= 6:
            continue
        facts.append((key, code(text)))
    return facts


def event_card(
    event: Event, *, closed: str | None = None, mention_role: int | None = None
) -> ui.LayoutView:
    """로그 채널에 올리는 카드. 처리에 필요한 버튼이 같이 붙는다."""
    view = ui.LayoutView(timeout=None)
    items: list[ui.Item] = []
    if mention_role:
        items.append(ui.TextDisplay(f"<@&{mention_role}>"))
    items.append(heading(kind_title(event.kind), event.message))
    facts = _short_facts(event)
    if closed:
        facts.append(("처리됨", closed))
    if facts:
        items += [divider(), ui.TextDisplay(rows(*facts))]
    buttons = _card_buttons(event, closed=closed is not None)
    if buttons:
        items.append(ui.ActionRow(*buttons))
    category = CATEGORIES.get(event.kind.split(".")[0], "")
    items.append(subtext(f"{_level(event)} · {category} · {_when(event)} · #{event.id}"))
    view.add_item(card(*items, tone=_LEVEL_TONES.get(event.level, "info")))
    return view


def _card_buttons(event: Event, *, closed: bool) -> list[ui.Item]:
    buttons: list[ui.Item] = []
    if event.kind == "deposit.unmatched" and event.ref_id and not closed:
        buttons.append(OpsButton("credit", event.ref_id, label="유저에게 충전", style=discord.ButtonStyle.primary))
        buttons.append(OpsButton("dismiss", event.ref_id, label="봇과 무관 · 제외"))
    if event.ref_type == "tx" and event.ref_id:
        buttons.append(OpsButton("tx", event.ref_id, label="송금 상세"))
    if event.user_id:
        buttons.append(OpsButton("user", event.user_id, label="유저 보기"))
    if event.id and event_details(event):
        buttons.append(OpsButton("event", event.id, label="자세히"))
    return buttons[:5]


class OpsButton(ui.DynamicItem[ui.Button], template=r"ops:(?P<action>[a-z]+):(?P<target>\d+)"):
    """로그 카드 버튼. 동작과 대상이 custom_id에 들어 있어 재시작 뒤나 오래된 카드에서도 눌린다."""

    def __init__(
        self,
        action: str,
        target: int,
        *,
        label: str = "열기",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(ui.Button(label=label, style=style, custom_id=f"ops:{action}:{target}"))
        self.action = action
        self.target = target

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: ui.Button, match, /
    ) -> OpsButton:
        return cls(match["action"], int(match["target"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user.id):
            await deny_non_admin(interaction)
            return
        handler = _ACTIONS.get(self.action)
        try:
            if handler is None:
                await reply_ephemeral(
                    interaction,
                    "예전 버전에서 만든 버튼이라 지금은 쓸 수 없어요.",
                    title="지원하지 않는 버튼이에요",
                )
                return
            await handler(interaction, self.target)
        except Exception as exc:
            await show_error(interaction, exc, context=f"로그 버튼 {self.action}")


def event_detail_view(event: Event, viewer_id: int) -> BaseLayout:
    view = BaseLayout(owner_id=viewer_id)
    details = event_details(event)
    trace = details.pop("traceback", None)
    items: list[ui.Item] = [
        heading(
            kind_title(event.kind),
            f"-# {_level(event)} · {_when(event)} · #{event.id} · {event.kind}",
        ),
        divider(),
        ui.TextDisplay(event.message),
    ]
    facts = _ref_facts(event, with_id=True)
    facts += [(key, code(truncate(str(value), 300))) for key, value in details.items()]
    if facts:
        items.append(ui.TextDisplay(join_limited(rows(*facts).split("\n"), limit=1500)))
    # traceback은 남은 글자 수만큼만 보여 준다
    used = sum(len(item.content) for item in items if isinstance(item, ui.TextDisplay))
    room = min(TRACEBACK_LIMIT, MESSAGE_TEXT_LIMIT - used - 50)
    if trace and room > 200:
        tail = trace if len(trace) <= room else "…" + trace[-room:]
        items += [divider(), ui.TextDisplay(f"-# traceback\n```py\n{tail.replace('```', 'ˋˋˋ')}\n```")]
    buttons = _quick_buttons(user_id=event.user_id, tx_id=event.ref_id if event.ref_type == "tx" else None)
    if buttons:
        items.append(ui.ActionRow(*buttons))
    view.add_item(card(*items, tone=_LEVEL_TONES.get(event.level, "info")))
    return view


def _quick_buttons(*, user_id: int | None = None, tx_id: int | None = None) -> list[ui.Item]:
    buttons: list[ui.Item] = []
    if tx_id:
        buttons.append(OpsButton("tx", tx_id, label="송금 상세"))
    if user_id:
        buttons.append(OpsButton("user", user_id, label="유저 보기"))
    return buttons


async def _open_event(interaction: discord.Interaction, event_id: int) -> None:
    event = await get_bot(interaction).db.get_event(event_id)
    if event is None:
        await reply_ephemeral(
            interaction,
            f"#{event_id} 기록이 없어요. 보관 기간이 지나 지워졌을 수 있어요.",
            title="기록을 찾을 수 없어요",
        )
        return
    await interaction.response.send_message(
        view=event_detail_view(event, interaction.user.id), ephemeral=True
    )


async def build_profit_view(bot) -> BaseLayout:
    """/이익 화면. 관리자 계정의 송금은 매출이 아니라서 뺀다."""
    now = datetime.now(timezone.utc)
    since = {
        "오늘": datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0),
        "최근 7일": now - timedelta(days=7),
        "최근 30일": now - timedelta(days=30),
        "전체": None,
    }
    report = await bot.db.profit_report(config.ADMIN_USERS, {
        label: moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if moment else None
        for label, moment in since.items()
    })

    def line(totals: ProfitTotals) -> str:
        if not totals.transfers:
            return "송금 없음"
        return f"**{totals.profit:,}원** · 송금 {totals.transfers}건 {totals.krw:,}원"

    everything = report.periods["전체"]
    facts: list[Fact] = [(label, line(report.periods[label])) for label in since]
    facts.append((
        "전체 이익 내역",
        f"수수료 {everything.fees:,}원 + 김프 차익 {everything.profit - everything.fees:,}원",
    ))
    if report.admin_transfers:
        facts.append(("제외한 관리자 송금", f"{report.admin_transfers}건 · {report.admin_krw:,}원"))
    if report.open_transfers:
        facts.append((
            "전송 중", f"{report.open_transfers}건 · {report.open_krw:,}원 · 전송이 끝나면 더해져요",
        ))
    notes = []
    unmeasured = everything.transfers - everything.measured
    if unmeasured:
        notes.append(f"시세 기록이 없는 예전 송금 {unmeasured}건은 수수료만 이익으로 계산했어요")
    notes.append("재고(USDT)를 산 뒤의 시세 변동과 업비트 수수료는 들어 있지 않아요")
    return notice(
        "고객이 낸 돈에서, 보낸 코인을 그때 업비트 USDT 시세로 다시 채우는 값을 뺀 금액이에요.\n"
        "관리자 계정의 송금은 빼고, 블록체인 전송이 끝난 송금만 세요.",
        title="이익",
        facts=facts,
        hint="\n".join(notes),
    )


def limit_facts(user_id: int, status: NewUserStatus) -> list[Fact]:
    """관리자에게 보여 주는 유저의 24시간 한도. 첫 줄이 지금 적용되는 한도다."""
    daily = config.DAILY_TRANSFER_LIMIT_KRW
    usual = f"{daily:,}원" if daily else "없음"
    days, min_total = config.NEW_USER_DAYS, config.NEW_USER_MIN_TOTAL_KRW
    own = status.own_limit
    if is_admin(user_id):
        return [("24시간 한도", f"{usual} · 관리자는 신규 한도가 없어요")]
    if not (config.NEW_USER_DAILY_LIMIT_KRW and days):
        return [("24시간 한도", f"{usual} · 신규 이용자 한도를 쓰지 않는 설정이에요")]
    if status.cap:
        limit = f"**{status.cap:,}원** · 신규 이용자 " + ("· 관리자가 정한 한도" if own else "기본 한도")
    elif status.is_new and own:
        limit = f"**{usual}** · 신규 한도 없이 일반 한도 (관리자가 {own.new_user_limit:,}원으로 정함)"
    elif status.is_new:
        limit = usual  # 신규 한도가 일반 한도 이상이라 걸릴 일이 없는 경우
    else:
        limit = f"{usual} · 신규 기간이 끝났어요" + (
            f" (따로 정한 {own.new_user_limit:,}원은 쓰이지 않아요)" if own else ""
        )
    ceiling = status.cap or daily
    sent = f"{status.spent_24h:,}원"
    if ceiling:
        sent += f" · 남은 한도 {max(ceiling - status.spent_24h, 0):,}원"
    facts = [("24시간 한도", limit), ("최근 24시간 송금", sent)]
    if status.is_new:
        if status.first_transfer_at:
            so_far = f"첫 송금 {_short(status.first_transfer_at)} · 누적 {status.total_krw:,}원"
        else:
            so_far = "아직 송금한 적이 없어요"
        until = f"첫 송금 후 {days}일이 지나" + (f"고 누적 {min_total:,}원 이상 보내" if min_total else "")
        facts.append(("신규 기간", f"{so_far} · {until}면 끝나요"))
    if own:
        facts.append(("한도를 정한 관리자", f"<@{own.set_by}> · {_short(own.set_at)}"))
    return facts


async def build_user_view(bot, user_id: int, viewer_id: int) -> BaseLayout:
    stats = await bot.db.get_user_stats(user_id)
    user = await bot.db.get_user(user_id)
    name = await bot.deposits.get_depositor_name(user_id)
    deposits = await bot.db.get_user_deposits(user_id, 5)
    txs = await bot.db.get_user_transactions(user_id, 5)
    adjustments = await bot.db.get_user_adjustments(user_id, 3)
    events = await bot.db.list_events(user_id=user_id, limit=6)
    block = await bot.db.get_block(user_id)
    limit = limit_facts(user_id, await bot.transfers.new_user_status(user_id))[0]

    if block is not None:
        by = "봇 자동 정지" if block.blocked_by == SYSTEM_ACTOR else f"<@{block.blocked_by}>"
        access = (
            f"**정지됨** · {escape_markdown(block.reason)} "
            f"({_short(block.blocked_at)} {by}, 풀려면 `/관리자 이용정지해제`)"
        )
    elif is_allowed(user_id):
        access = "있음"
    else:
        access = "**없음** (입금자명을 지정하면 생겨요)"
    profile = rows(
        ("총 충전", f"{stats['total_deposited']:,}원"),
        ("총 사용", f"{stats['total_spent']:,}원"),
        ("송금 건수", f"{stats['transaction_count']}건"),
        ("입금자명", escape_markdown(name) if name else "미지정"),
        ("봇 사용 권한", access),
        limit,
        ("처음 기록", format_kst(user.created_at) if user.created_at else "-"),
    )
    sections = [
        ("최근 충전 요청", [
            f"`{_short(d.created_at)}` {d.amount:,}원 · {DEPOSIT_STATUS.get(d.status, d.status)}"
            for d in deposits
        ]),
        ("최근 송금", [
            f"`#{t.id}` `{_short(t.created_at)}` {t.coin_amount} {t.coin} · "
            f"{t.krw_amount:,}원 · {_tx_status(t)}"
            for t in txs
        ]),
        ("수동 충전·차감", [
            f"`{_short(a['created_at'])}` {a['amount']:+,}원 · "
            f"{escape_markdown(a['reason'])} · <@{a['admin_id']}>"
            for a in adjustments
        ]),
        ("최근 기록", [
            f"`#{e.id}` `{_short(e.created_at)}` {_level(e)} · {kind_title(e.kind)}"
            for e in events
        ]),
    ]
    body = "\n\n".join(
        f"-# {title}\n" + ("\n".join(lines) if lines else "없음") for title, lines in sections
    )
    items: list[ui.Item] = [
        heading("유저 정보", f"<@{user_id}> · `{user_id}`"),
        figure("잔액", f"{stats['balance']:,}원"),
        divider(),
        ui.TextDisplay(profile),
        divider(),
        ui.TextDisplay(join_limited(body.split("\n"))),
        ui.ActionRow(OpsLogButton(user_id)),
    ]
    if txs:
        select = ui.Select(
            placeholder="송금 상세 보기",
            options=[
                discord.SelectOption(
                    label=truncate(f"#{t.id} {t.coin_amount} {t.coin}", 100),
                    value=str(t.id),
                    description=truncate(_tx_status(t), 100),
                )
                for t in txs
            ],
        )

        async def pick(interaction: discord.Interaction) -> None:
            await _open_tx(interaction, int(select.values[0]))

        select.callback = pick
        items.append(ui.ActionRow(select))
    view = BaseLayout(owner_id=viewer_id)
    view.add_item(card(*items))
    return view


class OpsLogButton(ui.Button):
    def __init__(self, user_id: int) -> None:
        super().__init__(label="이 유저의 기록 전체", style=discord.ButtonStyle.secondary)
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await send_log_list(interaction, LogFilter(user_id=self.user_id))


async def _open_user(interaction: discord.Interaction, user_id: int) -> None:
    await interaction.response.send_message(view=loading("유저 정보를 불러오는 중이에요"), ephemeral=True)
    view = await build_user_view(get_bot(interaction), user_id, interaction.user.id)
    await interaction.edit_original_response(view=view)


class AdminTxLayout(BaseLayout):
    def __init__(self, tx: Transaction, events: list[Event], viewer_id: int, *, note: str | None = None) -> None:
        super().__init__(owner_id=viewer_id)
        self.tx_id = tx.id
        facts = [
            ("상태", f"**{_tx_status(tx)}** (`{tx.status}`)"),
            ("유저", f"<@{tx.discord_id}>"),
            ("일시", format_kst(tx.created_at)),
            ("코인", f"{tx.coin} · {tx.network}"),
            ("보낸 수량", f"`{tx.coin_amount}` {tx.coin}"),
            ("차감 금액", f"{tx.krw_amount:,}원 (수수료 {tx.fee_krw:,}원)"),
            ("적용 시세", f"{tx.exchange_rate:,.2f}원"),
            ("받는 주소", code(tx.address)),
        ]
        if tx.memo:
            facts.append(("메모/태그", code(tx.memo)))
        if tx.withdraw_order_id:
            facts.append(("주문 ID", code(tx.withdraw_order_id)))
        if tx.binance_withdraw_id:
            facts.append(("출금 ID", code(tx.binance_withdraw_id)))
        if tx.buy_order_id:
            facts.append(("자동 매수", f"{code(tx.buy_order_id)} · {tx.buy_cost_usdt} USDT"))
        if tx.updated_at:
            facts.append(("마지막 변경", format_kst(tx.updated_at)))
        if tx.error:
            facts.append(("오류", code(truncate(tx.error, 300))))
        facts.append(("TXID", code(tx.txid) if tx.txid else "아직 없어요"))
        history = "\n".join(
            f"`{_short(e.created_at, '%m-%d %H:%M:%S')}` {kind_title(e.kind)} · {_first_line(e.message, 70)}"
            for e in reversed(events)
        ) or "기록 없음"

        buttons: list[ui.Item] = []
        if needs_lookup(tx):
            recheck = ui.Button(label="바이낸스와 다시 대조", style=discord.ButtonStyle.primary)
            recheck.callback = self._recheck
            buttons.append(recheck)
        buttons.append(OpsButton("user", tx.discord_id, label="유저 보기"))
        url = explorer_url(tx.network, tx.txid)
        if url:
            buttons.append(ui.Button(label="블록 탐색기", style=discord.ButtonStyle.link, url=url))

        items: list[ui.Item] = [
            heading(f"송금 #{tx.id}"),
            ui.TextDisplay(rows(*facts)),
            divider(),
            ui.TextDisplay(f"-# 처리 과정\n{history}"),
            ui.ActionRow(*buttons),
        ]
        if note:
            items.append(subtext(note))
        tone = {"failed": "warning", "unknown": "error", "completed": "success"}.get(tx.status, "info")
        self.add_item(card(*items, tone=tone))

    async def _recheck(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = await build_tx_view(get_bot(interaction), self.tx_id, interaction.user.id, recheck=True)
        self.stop()
        await interaction.edit_original_response(view=view)


async def build_tx_view(bot, tx_id: int, viewer_id: int, *, recheck: bool = False) -> BaseLayout:
    tx = await bot.db.get_transaction(tx_id)
    if tx is None:
        return notice(
            f"#{tx_id} 송금이 없어요. 번호를 다시 확인해 주세요.",
            title="송금을 찾을 수 없어요",
            tone="warning",
        )
    note = None
    if recheck:
        try:
            looked_up = await bot.transfers.lookup_txid(tx)
        except Exception as exc:
            looked_up = tx
            note = f"바이낸스 조회 실패: {type(exc).__name__}: {truncate(str(exc), 120)}"
        if looked_up is None:
            note = "바이낸스 조회가 몰려 있어요. 10초 뒤에 다시 눌러 주세요."
        else:
            changed = looked_up.status != tx.status or looked_up.txid != tx.txid
            tx = looked_up
            note = note or ("바이낸스 기록과 대조해 상태를 갱신했어요." if changed else
                            "바이낸스와 대조했어요. 바뀐 내용은 없어요.")
            if tx.status in ("pending", "unknown"):
                note += " 송금 30분 이후 미접수가 확인되면 자동 환불돼요. 오래된 미확정 건은 직접 확인해 주세요."
    events = await bot.db.list_events(ref=("tx", tx.id), limit=12)
    return AdminTxLayout(tx, events, viewer_id, note=note)


async def _open_tx(interaction: discord.Interaction, tx_id: int) -> None:
    await interaction.response.send_message(view=loading("송금 내역을 확인하는 중이에요"), ephemeral=True)
    view = await build_tx_view(get_bot(interaction), tx_id, interaction.user.id)
    await interaction.edit_original_response(view=view)


async def _close_card(bot, message: discord.Message | None, notification_id: int, note: str) -> None:
    """미확인 입금 카드에 처리 결과를 적고 버튼을 뗀다."""
    if message is None:
        return
    event = await bot.db.find_event("deposit.unmatched", "notification", notification_id)
    if event is None:
        return
    try:
        await message.edit(view=event_card(event, closed=note))
    except discord.HTTPException:
        log.info("Could not update the card for notification #%s", notification_id)


def _notification_facts(notification) -> str:
    return rows(
        ("입금자명", f"**{escape_markdown(notification.depositor_name)}**"),
        ("금액", f"**{notification.amount:,}원**"),
        ("받은 시각", format_kst(notification.received_at)),
    )


class ResolveLayout(BaseLayout):
    def __init__(self, notification, viewer_id: int, source: discord.Message | None) -> None:
        super().__init__(owner_id=viewer_id)
        self.notification = notification
        self.source = source
        self._done = False

        owner = notification.owner_id
        hint = (
            f"이 입금자명은 <@{owner}>님에게 지정돼 있어요."
            if owner else "이 입금자명은 아무에게도 지정되지 않았어요. 받을 유저를 직접 골라 주세요."
        )
        items: list[ui.Item] = [
            heading("미확인 입금 충전", f"입금 알림 #{notification.id}"),
            divider(),
            ui.TextDisplay(_notification_facts(notification)),
            ui.TextDisplay(hint),
        ]
        if owner:
            to_owner = ui.Button(label="지정된 유저에게 충전", style=discord.ButtonStyle.primary)
            to_owner.callback = self._to_owner
            items.append(ui.ActionRow(to_owner))
        picker = ui.UserSelect(placeholder="다른 유저에게 충전하려면 고르세요")

        async def picked(interaction: discord.Interaction) -> None:
            refuse_bot(picker.values[0])
            await self._resolve(interaction, picker.values[0].id)

        picker.callback = picked
        items.append(ui.ActionRow(picker))
        items.append(subtext("고르는 즉시 충전되고 유저에게 DM이 가요"))
        self.add_item(card(*items, tone="warning"))

    async def _to_owner(self, interaction: discord.Interaction) -> None:
        await self._resolve(interaction, self.notification.owner_id)

    async def _resolve(self, interaction: discord.Interaction, user_id: int) -> None:
        if self._done:
            await _already_resolved(interaction)
            return
        self.stop()

        async def credit(confirmed: discord.Interaction, how: str, revision: int) -> None:
            if self._done:
                await _already_resolved(confirmed)
                return
            self._done = True
            await credit_unmatched(
                confirmed, self.notification.id, user_id, how=how, source=self.source,
                expected_revision=revision,
            )

        await resolve_checked(
            interaction, self.notification.id, user_id, how="edit", source=self.source, credit=credit
        )


async def resolve_checked(
    interaction: discord.Interaction,
    notification_id: int,
    user_id: int,
    *,
    how: str,
    source: discord.Message | None = None,
    credit=None,
) -> None:
    """미확인 입금을 충전하기 전에 이중 충전 가능성을 확인한다 (DepositService.resolve_warnings).

    경고가 있으면 관리자에게 한 번 더 묻는다. 확인이 3초를 넘길 수 있어 디스코드에 먼저 답한다.
    실제 충전은 credit(interaction, how, revision)이 한다.
    """
    bot = get_bot(interaction)
    await answer(interaction, loading("충전해도 되는지 확인하는 중이에요"), how=how)
    try:
        revision = await bot.db.credit_revision()
        notification = await bot.db.get_notification(notification_id)
        # 없거나 이미 처리된 알림은 credit_unmatched가 이유를 보여 준다
        warnings = await bot.deposits.resolve_warnings(notification, user_id) if notification else []
    except Exception:
        log.exception("Checking unmatched transfer #%s failed", notification_id)
        await show_result(interaction, check_failed())
        return

    async def default(target: discord.Interaction, target_how: str, revision: int) -> None:
        await credit_unmatched(
            target, notification_id, user_id, how=target_how, source=source,
            expected_revision=revision,
        )

    credit = credit or default
    if warnings:
        async def confirmed(target: discord.Interaction) -> None:
            await credit(target, "edit", revision)

        await interaction.edit_original_response(view=ResolveConfirmLayout(
            notification, user_id, warnings, interaction.user.id, on_confirm=confirmed
        ))
        return
    await credit(interaction, "update", revision)


async def credit_unmatched(
    interaction: discord.Interaction,
    notification_id: int,
    user_id: int,
    *,
    how: str,
    source: discord.Message | None = None,
    expected_revision: int | None = None,
) -> None:
    """미확인 입금을 유저에게 충전한다 (로그 카드 버튼, /관리자 미확인입금충전).

    돈이 움직인 뒤의 단계는 예외를 올리지 않는다. 오류 카드가 뜨면 '다시 시도'로 읽혀서
    다른 경로로 한 번 더 충전하게 된다.
    """
    bot = get_bot(interaction)
    await answer(
        interaction, working(f"<@{user_id}>님에게 미확인 입금 #{notification_id}을 충전하는 중이에요"), how=how
    )
    try:
        notification, new_balance = await bot.deposits.resolve_notification(
            notification_id, user_id, interaction.user.id, expected_revision=expected_revision,
        )
    except UserFacingError as exc:
        await show_result(interaction, error_notice(exc))
        return
    except Exception:
        log.exception("Crediting unmatched transfer #%s failed", notification_id)
        await show_result(interaction, unclear_result("미확인 입금 충전"))
        return
    await show_result(interaction, resolved_view(notification, user_id, new_balance))
    await bot.notify_user(user_id, view=deposit_complete_view(notification.amount, new_balance))
    await quietly(_close_card(
        bot, source, notification.id,
        f"<@{interaction.user.id}>님이 <@{user_id}>님에게 충전 · {_now_kst()}",
    ), "Closing the unmatched-transfer card")


async def _already_resolved(interaction: discord.Interaction) -> None:
    await reply_ephemeral(
        interaction,
        "같은 입금이 두 번 충전되지 않도록 한 번만 처리해요.",
        title="이미 처리한 입금이에요",
        tone="info",
    )


def resolved_view(notification, user_id: int, new_balance: int) -> BaseLayout:
    return notice(
        f"<@{user_id}>님에게 **{notification.amount:,}원**을 충전했어요.",
        title="미확인 입금을 충전했어요",
        tone="success",
        facts=[
            ("입금 알림", f"#{notification.id} · {escape_markdown(notification.depositor_name)}"),
            ("받은 유저", f"<@{user_id}>"),
            ("현재 잔액", f"{new_balance:,}원"),
        ],
        hint="유저에게 충전 완료 DM을 보내요",
    )


class ResolveConfirmLayout(BaseLayout):
    """수동 충전과 겹치거나 이미 빠져나간 돈일 수 있을 때 한 번 더 묻는 화면."""

    def __init__(
        self, notification, user_id: int, warnings: list[str], viewer_id: int, *, on_confirm
    ) -> None:
        super().__init__(owner_id=viewer_id)
        self._user_id = user_id
        self._on_confirm = on_confirm
        self._handled = False
        confirm = ui.Button(label=f"그래도 {notification.amount:,}원 충전", style=discord.ButtonStyle.danger)
        confirm.callback = self._confirm
        cancel = ui.Button(label="충전하지 않기", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(card(
            heading("이미 충전했거나 빠져나간 돈일 수 있어요", f"입금 알림 #{notification.id}"),
            divider(),
            ui.TextDisplay(
                f"<@{user_id}>님에게 이 입금을 충전하기 전에 아래를 확인해 주세요. 같은 돈이면 두 번 "
                "충전되거나, 이미 계좌에서 빠져나간 돈을 충전하게 돼요.\n" + join_limited(warnings)
            ),
            ui.TextDisplay(_notification_facts(notification)),
            ui.ActionRow(confirm, cancel),
            subtext(
                "은행 앱에서 이 돈이 계좌에 있고 위 충전과 **다른 돈**인지 확인한 뒤에만 충전하세요\n"
                "같은 돈이거나 빠져나간 돈이면 로그 카드의 '봇과 무관 · 제외'나 `/관리자 미확인입금제외`로 정리해요"
            ),
            tone="warning",
        ))

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if self._handled:
            await _already_resolved(interaction)
            return
        self._handled = True
        self.stop()
        await self._on_confirm(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if self._handled:
            await _already_resolved(interaction)
            return
        self._handled = True
        self.stop()
        await interaction.response.edit_message(view=notice(
            f"<@{self._user_id}>님에게 충전하지 않았어요. 입금 알림은 미확인 목록에 그대로 있어요.",
            title="충전하지 않았어요",
        ))


class DismissLayout(BaseLayout):
    def __init__(self, notification, viewer_id: int, source: discord.Message | None) -> None:
        super().__init__(owner_id=viewer_id)
        self.notification = notification
        self.source = source
        confirm = ui.Button(label="제외", style=discord.ButtonStyle.danger)
        confirm.callback = self._confirm
        self.add_item(card(
            heading("입금 알림 제외", f"입금 알림 #{notification.id}"),
            divider(),
            ui.TextDisplay(
                f"**{escape_markdown(notification.depositor_name)}** · **{notification.amount:,}원**을 "
                "봇과 무관한 입금으로 보고 미확인 목록에서 뺄까요?\n아무에게도 충전되지 않아요."
            ),
            ui.ActionRow(confirm),
            tone="warning",
        ))

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        bot = get_bot(interaction)
        notification = await bot.deposits.dismiss_notification(self.notification.id, interaction.user.id)
        await interaction.response.edit_message(view=dismissed_view(notification))
        await _close_card(
            bot, self.source, notification.id, f"<@{interaction.user.id}>님이 제외 · {_now_kst()}"
        )


def dismissed_view(notification) -> BaseLayout:
    return notice(
        "미확인 목록에서 뺐고, 아무에게도 충전하지 않았어요.",
        title="입금 알림을 제외했어요",
        tone="success",
        facts=[
            ("입금 알림", f"#{notification.id}"),
            ("입금자명", escape_markdown(notification.depositor_name)),
            ("금액", f"{notification.amount:,}원"),
        ],
    )


async def _start_resolution(interaction: discord.Interaction, notification_id: int, layout) -> None:
    bot = get_bot(interaction)
    notification = await bot.db.get_notification(notification_id)
    if notification is None:
        await reply_ephemeral(
            interaction,
            f"#{notification_id} 입금 알림이 없어요.",
            title="입금 알림을 찾을 수 없어요",
        )
        return
    if notification.status != "unmatched":
        state = NOTIFICATION_STATUS.get(notification.status, notification.status)
        await reply_ephemeral(
            interaction,
            "다른 관리자가 먼저 처리했을 수 있어요.",
            title="이미 처리된 입금 알림이에요",
            tone="info",
            facts=[("입금 알림", f"#{notification_id}"), ("상태", state)],
        )
        await _close_card(bot, interaction.message, notification_id, state)
        return
    await interaction.response.send_message(
        view=layout(notification, interaction.user.id, interaction.message), ephemeral=True
    )


async def _credit(interaction: discord.Interaction, notification_id: int) -> None:
    await _start_resolution(interaction, notification_id, ResolveLayout)


async def _dismiss(interaction: discord.Interaction, notification_id: int) -> None:
    await _start_resolution(interaction, notification_id, DismissLayout)


_ACTIONS = {
    "event": _open_event,
    "user": _open_user,
    "tx": _open_tx,
    "credit": _credit,
    "dismiss": _dismiss,
}


@dataclass
class LogFilter:
    level: str = "all"
    category: str | None = None
    user_id: int | None = None

    def describe(self) -> str:
        parts = [{"all": "전체", "alert": "경고·오류", "error": "오류만"}.get(self.level, self.level)]
        if self.category:
            parts.append(CATEGORIES.get(self.category, self.category))
        if self.user_id:
            parts.append(f"<@{self.user_id}>")
        return " · ".join(parts)


@dataclass
class _Page:
    events: list[Event]
    has_more: bool
    # 지나온 쪽들의 before_id. 마지막 값이 지금 쪽
    cursors: list[int | None] = field(default_factory=lambda: [None])


async def _load_page(bot, filters: LogFilter, cursors: list[int | None]) -> _Page:
    events = await bot.db.list_events(
        levels=LEVEL_FILTERS.get(filters.level),
        kind_prefix=f"{filters.category}." if filters.category else None,
        user_id=filters.user_id,
        before_id=cursors[-1],
        limit=LOG_PAGE_SIZE + 1,
    )
    return _Page(events[:LOG_PAGE_SIZE], len(events) > LOG_PAGE_SIZE, cursors)


class LogListLayout(BaseLayout):
    def __init__(self, page: _Page, filters: LogFilter, viewer_id: int) -> None:
        super().__init__(owner_id=viewer_id)
        self.filters = filters
        self.page = page

        lines = [
            f"`#{e.id}` `{_short(e.created_at)}` **{kind_title(e.kind)}** · {_level(e)}"
            + (f" · <@{e.user_id}>" if e.user_id else "")
            + f"\n-# {_first_line(e.message)}"
            for e in page.events
        ]
        page_no = len(page.cursors)
        items: list[ui.Item] = [
            heading("운영 기록", f"-# {filters.describe()} · {page_no}쪽"),
            divider(),
            ui.TextDisplay(join_limited(lines) if lines else "조건에 맞는 기록이 없어요."),
        ]
        if page.events:
            select = ui.Select(
                placeholder="기록 자세히 보기",
                options=[
                    discord.SelectOption(
                        label=truncate(f"#{e.id} {kind_title(e.kind)}", 100),
                        value=str(e.id),
                        description=truncate(_first_line(e.message, 100), 100) or None,
                    )
                    for e in page.events
                ],
            )

            async def pick(interaction: discord.Interaction) -> None:
                await _open_event(interaction, int(select.values[0]))

            select.callback = pick
            items.append(ui.ActionRow(select))

        newer = ui.Button(label="이전", style=discord.ButtonStyle.secondary, disabled=page_no == 1)
        newer.callback = self._newer
        older = ui.Button(label="다음", style=discord.ButtonStyle.secondary, disabled=not page.has_more)
        older.callback = self._older
        refresh = ui.Button(label="새로고침", style=discord.ButtonStyle.secondary)
        refresh.callback = self._refresh
        items.append(ui.ActionRow(newer, older, refresh))
        self.add_item(card(*items))

    async def _show(self, interaction: discord.Interaction, cursors: list[int | None]) -> None:
        page = await _load_page(get_bot(interaction), self.filters, cursors)
        self.stop()
        await interaction.response.edit_message(view=LogListLayout(page, self.filters, self.owner_id))

    async def _newer(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, self.page.cursors[:-1] or [None])

    async def _older(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, self.page.cursors + [self.page.events[-1].id])

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, [None])


async def send_log_list(interaction: discord.Interaction, filters: LogFilter) -> None:
    page = await _load_page(get_bot(interaction), filters, [None])
    await interaction.response.send_message(
        view=LogListLayout(page, filters, interaction.user.id), ephemeral=True
    )


def _ago(moment: datetime | None) -> str:
    return timestamp(moment, "R") if moment else "기록 없음"


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분"


def _probe_text(probe) -> str:
    if probe.ok is None:
        return "아직 확인 전"
    if probe.ok:
        return f"정상 · {_ago(probe.checked_at)} 확인"
    return f"**장애** ({probe.streak}회 연속 실패) · {code(truncate(probe.error or '', 120))}"


def _setting(settings: dict, key: str, mention: str, command: str) -> str:
    # mention은 "<#{}>"나 "<@&{}>" 같은 틀
    return mention.format(settings[key]) if settings.get(key) else f"꺼짐 (`{command}`)"


async def build_status_view(bot, viewer_id: int) -> BaseLayout:
    monitor = bot.monitor
    counts = await bot.db.ops_counts(config.DEPOSIT_TIMEOUT_MINUTES)
    activity = await bot.db.activity_summary(24)
    panel = await build_panel_status(bot)
    settings = bot.log_settings
    pause = await bot.db.get_service_pause()
    backup_ok, backup_line = backup_status()

    if pause is not None:
        service = (
            f"**멈춤** · {escape_markdown(str(pause.get('reason', '-')))} · "
            f"{format_kst(pause.get('since'))}부터 · 다시 열려면 `/관리자 서비스재개`"
        )
    else:
        service = "정상 운영 중"
    if panel.fx_rate is not None and panel.btc_premium is not None and panel.usdt_premium is not None:
        fx = (
            f"{panel.fx_source} ₩{panel.fx_rate:,.2f} · BTC 김프 {panel.btc_premium:+.2f}% · "
            f"USDT 김프 {panel.usdt_premium:+.2f}% · 판매 김프(BTC) +{panel.btc_applied:.2f}%"
        )
    else:
        fx = "**받지 못하고 있어요** · 업비트 USDT 가격으로 계산 중"
    status_channels = ", ".join(
        f"<#{settings[key]}>" for key in ("status_stock_channel", "status_premium_channel")
        if settings.get(key)
    ) or "꺼짐 (`/관리자 시세채널설정`)"
    system = rows(
        ("충전·송금", service),
        ("봇", f"가동 {_duration(time.monotonic() - monitor.started_monotonic)} · "
               f"수수료 {fee_percent(config.FEE_RATE)} · 추가 김프 {premium_percent(config.MIN_KIMCHI_PREMIUM_PCT)}"),
        ("바이낸스 API", _probe_text(monitor.binance)),
        ("시세", _probe_text(monitor.prices) + (f" · USDT ₩{panel.usdt_krw:,.0f}" if panel.usdt_krw else "")),
        ("환율", fx),
        ("입금 알림 API", f"마지막 수신 {_ago(bot.last_webhook_at)}"),
        ("DB 백업", backup_line),
    )
    channels = rows(
        ("로그 채널", f"<#{settings['log_channel']}>" if settings.get("log_channel")
         else "**미설정** (`/관리자 로그채널설정`)"),
        ("구매 알림", _setting(settings, "purchase_channel", "<#{}>", "/관리자 구매알림설정")),
        ("입금자 역할", _setting(settings, "depositor_role", "<@&{}>", "/관리자 입금자역할설정")),
        ("구매자 역할", _setting(settings, "buyer_role", "<@&{}>", "/관리자 구매자역할설정")),
        ("재고·김프 채널", status_channels),
    )

    liabilities = activity["user_balances"]
    money = [
        ("매수 자금", f"USDT {monitor.balances.get('USDT', 0):,.2f} · USD {monitor.balances.get('USD', 0):,.2f}"),
        ("거래소 보유 자산", f"≈ ₩{panel.stock_krw:,.0f}" if panel.stock_krw is not None else "확인 실패"),
        ("유저 잔액 합계", f"₩{liabilities:,}"),
    ]
    short = panel.stock_krw is not None and liabilities > panel.stock_krw
    cap = config.GLOBAL_DAILY_LIMIT_KRW
    if cap:
        used = activity["transfer_krw"] / cap
        cap_line = f"{activity['transfer_krw']:,} / {cap:,}원 ({used:.0%})"
        if used >= 0.8:
            cap_line = f"**{cap_line} · 곧 송금이 막혀요**"
    else:
        cap_line = "없음 (`GLOBAL_DAILY_LIMIT_KRW`)"
    waiting = rows(
        ("충전 대기", f"{counts['pending_deposits']}건"),
        ("미확인 입금", f"**{counts['unmatched']}건**"),
        ("진행 중 송금", f"{counts['open_transfers']}건"),
        ("확인 불가 송금", f"**{counts['unknown_transfers']}건**"),
    )
    day = rows(
        ("충전", f"{activity['deposits']}건 {activity['deposit_krw']:,}원"),
        ("송금", f"{activity['transfers']}건 {activity['transfer_krw']:,}원 (수수료 {activity['fee_krw']:,}원)"),
        ("전체 송금 상한", cap_line),
        ("실패·경고", f"송금 실패 {activity['failed_transfers']}건 · 경고 {activity['warnings']}건 · "
                      f"오류 **{activity['errors']}**건"),
    )
    bad = (
        not monitor.binance.ok or not monitor.prices.ok
        or counts["unknown_transfers"] or counts["unmatched"] or not backup_ok
        or pause is not None
    )
    items: list[ui.Item] = [
        heading("운영 상태", f"-# {timestamp(datetime.now(timezone.utc), 'T')} 기준"),
        divider(),
        ui.TextDisplay(system),
        divider(),
        ui.TextDisplay(channels),
        divider(),
        ui.TextDisplay(rows(*money)),
    ]
    if short:
        items.append(ui.TextDisplay(
            f"유저 잔액이 보유 자산보다 **₩{liabilities - panel.stock_krw:,.0f}** 많아요. 모두 송금하면 재고가 모자라요."
        ))
    items += [
        divider(),
        ui.TextDisplay(f"-# 처리 대기\n{waiting}"),
        ui.TextDisplay(f"-# 지난 24시간\n{day}"),
        ui.ActionRow(StatusRefreshButton(), AlertLogButton()),
    ]
    view = BaseLayout(owner_id=viewer_id)
    view.add_item(card(*items, tone="warning" if bad else "success"))
    return view


class StatusRefreshButton(ui.Button):
    def __init__(self) -> None:
        super().__init__(label="새로고침", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = await build_status_view(get_bot(interaction), interaction.user.id)
        await interaction.edit_original_response(view=view)


class AlertLogButton(ui.Button):
    def __init__(self) -> None:
        super().__init__(label="최근 경고·오류", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        await send_log_list(interaction, LogFilter(level="alert"))
