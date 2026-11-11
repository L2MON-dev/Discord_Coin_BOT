from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime
from decimal import Decimal
from typing import Any

import discord
from discord import app_commands, ui

from . import config
from .database import Database, Event, Panel, Transaction
from .services.binance_client import BinanceClient
from .services.event_log import ALERT_LEVELS, SKIP_EVENT, EventLog, EventLogHandler
from .services.monitor import HealthMonitor
from .services.price_service import PriceService
from .services.deposit_service import DepositService
from .services.status_channels import StatusChannels
from .services.transfer_service import TransferService
from .api.webhook_server import WebhookServer
from .commands import register_commands, role_problem
from .views.base import fee_percent, notice, premium_percent, show_error
from .views.deposit import DepositPrompts
from .views.main_panel import PanelLayout, PanelStatus, build_panel_status
from .views.ops import NEEDS_ACTION, OpsButton, event_card
from .views.purchase import purchase_card

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 60
PANEL_REFRESH_SECONDS = 60
HEALTH_CHECK_SECONDS = 60
DEPOSIT_EXPIRY_SECONDS = 30
SHUTDOWN_FLUSH_SECONDS = 2.0

# settings 테이블 키
LOG_CHANNEL = "log_channel"
ALERT_CHANNEL = "alert_channel"
ALERT_ROLE = "alert_role"
# PURCHASE_SHOW_BUYER가 "0"이면 구매자를 숨긴다
PURCHASE_CHANNEL = "purchase_channel"
PURCHASE_SHOW_BUYER = "purchase_show_buyer"
DEPOSITOR_ROLE = "depositor_role"
DEPOSITOR_ROLE_GUILD = "depositor_role_guild"
BUYER_ROLE = "buyer_role"
BUYER_ROLE_GUILD = "buyer_role_guild"
# /수수료, /김프로 바꾸는 값. DB에 저장된 값이 .env보다 우선한다
# 키 -> (config 이름, 하한, 상한 미만)
FEE_RATE_SETTING = "fee_rate"
PREMIUM_SETTING = "min_kimchi_premium_pct"
TUNABLES = {
    FEE_RATE_SETTING: ("FEE_RATE", 0.0, 0.5),  # 0.025 = 2.5%
    PREMIUM_SETTING: ("MIN_KIMCHI_PREMIUM_PCT", 0.0, 20.0),  # 퍼센트 단위
}


def apply_saved_setting(key: str, saved: str | None) -> None:
    """DB에 저장된 값이 범위 안이면 .env 값 대신 쓴다."""
    if saved is None:
        return
    attr, low, high = TUNABLES[key]
    try:
        value = float(saved)
    except ValueError:
        value = float("nan")
    if low <= value < high:
        setattr(config, attr, value)
    else:
        log.warning("Ignoring the saved %s %r; using %s %s", key, saved, attr, getattr(config, attr))


async def save_setting(bot: CoinBot, key: str, value: Decimal) -> float:
    """값을 바로 적용하고 DB에도 저장한다. 이전 값을 돌려준다."""
    attr, low, high = TUNABLES[key]
    if not low <= value < high:
        raise ValueError(f"{key} out of range: {value}")
    old = getattr(config, attr)
    await bot.db.set_setting(key, str(value))
    setattr(config, attr, float(value))
    bot.log_settings = await bot.db.get_settings()
    return old


class CoinBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)

        self.db = Database(config.DB_PATH)
        self.events = EventLog(self.db)
        self.binance = BinanceClient()
        self.prices = PriceService(self.binance)
        self.deposits = DepositService(self.db, self.events)
        self.transfers = TransferService(
            self.db,
            self.binance,
            self.prices,
            notify_user=self.notify_user,
            on_purchase=self.after_purchase,
            on_complete=self.after_completion,
            events=self.events,
        )
        self.monitor = HealthMonitor(self.db, self.binance, self.prices, self.events)
        self.webhook = WebhookServer(self)
        self.status_channels = StatusChannels(self)
        self.panel_status: PanelStatus | None = None
        self.deposit_prompts = DepositPrompts()
        self.log_settings: dict[str, str] = {}
        self.last_webhook_at: datetime | None = None
        self._log_handler: EventLogHandler | None = None
        self._started = False
        self._shutdown: asyncio.Future | None = None
        self._tasks: list[asyncio.Task] = []

        register_commands(self.tree)

        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            if isinstance(error, app_commands.CheckFailure):
                return  # 체크에서 이미 안내했다
            command = interaction.command.qualified_name if interaction.command else "?"
            await show_error(
                interaction, getattr(error, "original", error), context=f"/{command}"
            )

    async def setup_hook(self) -> None:
        await self.db.connect()
        log.info("Database connected: %s", config.DB_PATH)
        # .env의 허용 유저에 입금자명을 받은 유저를 더한다. 정지된 유저는 따로 걸러진다
        config.ALLOWED_USERS.update(await self.db.list_allowed_users())
        config.BLOCKED_USERS.update(await self.db.list_blocked_users())
        self.log_settings = await self.db.get_settings()
        for key in TUNABLES:
            apply_saved_setting(key, self.log_settings.get(key))

        with contextlib.suppress(NotImplementedError, RuntimeError):
            # SIGTERM을 받으면 쓰던 걸 마무리하고 닫는다
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM, lambda: asyncio.create_task(self.close())
            )

        self.events.set_sink(self.post_event)
        self.events.start()
        self._log_handler = EventLogHandler(self.events, asyncio.get_running_loop())
        logging.getLogger().addHandler(self._log_handler)

        self.add_view(PanelLayout())
        self.add_dynamic_items(OpsButton)
        log.info("Persistent views registered")

        await self.webhook.start()
        self._tasks = [
            asyncio.create_task(self._every(RECONCILE_INTERVAL_SECONDS, self.transfers.reconcile)),
            asyncio.create_task(self._every(PANEL_REFRESH_SECONDS, self.refresh_panels)),
            asyncio.create_task(self._every(HEALTH_CHECK_SECONDS, self.monitor.check)),
            asyncio.create_task(self._every(DEPOSIT_EXPIRY_SECONDS, self.expire_deposits)),
        ]

    async def _every(self, seconds: int, job) -> None:
        while True:
            try:
                await job()
            except Exception:
                log.exception("Background job %s failed", job.__name__)
            await asyncio.sleep(seconds)

    async def expire_deposits(self) -> None:
        for deposit in await self.deposits.collect_expired():
            await self.deposit_prompts.expire(deposit.id, deposit.amount)

    async def save_settings(self, values: dict[str, int | str | None]) -> None:
        """None이나 0을 주면 그 설정을 지운다."""
        for key, value in values.items():
            await self.db.set_setting(key, str(value) if value else None)
        self.log_settings = await self.db.get_settings()

    async def set_log_channels(
        self, log_channel: int, alert_channel: int | None, alert_role: int | None
    ) -> set[int]:
        """로그 채널을 저장하고, 이제 로그를 받지 않게 된 채널들을 돌려준다."""
        before = {self.log_settings.get(LOG_CHANNEL), self.log_settings.get(ALERT_CHANNEL)}
        await self.save_settings(
            {LOG_CHANNEL: log_channel, ALERT_CHANNEL: alert_channel, ALERT_ROLE: alert_role}
        )
        after = {self.log_settings.get(LOG_CHANNEL), self.log_settings.get(ALERT_CHANNEL)}
        return {int(channel) for channel in before - after if channel}

    async def announce_log_move(self, event: Event, left: set[int], admin_id: int) -> None:
        """로그 채널이 바뀐 사실을 예전 채널과 다른 관리자에게도 알린다.

        새 채널에만 남기면 탈취된 관리자 계정이 로그를 아무도 안 보는 곳으로 몰래 돌릴 수 있다.
        """
        for channel_id in left:
            await self._send_card(channel_id, event, None)
        for other in config.ADMIN_USERS - {admin_id}:
            await self.notify_user(other, view=event_card(event))

    async def set_purchase_channel(self, channel_id: int | None, show_buyer: bool) -> None:
        await self.save_settings({
            PURCHASE_CHANNEL: channel_id,
            PURCHASE_SHOW_BUYER: ("1" if show_buyer else "0") if channel_id else None,
        })

    async def set_depositor_role(self, role: discord.Role | None) -> None:
        await self.save_settings({
            DEPOSITOR_ROLE: role.id if role else None,
            DEPOSITOR_ROLE_GUILD: role.guild.id if role else None,
        })

    async def set_buyer_role(self, role: discord.Role | None) -> None:
        await self.save_settings({
            BUYER_ROLE: role.id if role else None,
            BUYER_ROLE_GUILD: role.guild.id if role else None,
        })

    async def set_fee_rate(self, rate: Decimal) -> float:
        return await save_setting(self, FEE_RATE_SETTING, rate)

    async def set_min_premium(self, pct: Decimal) -> float:
        return await save_setting(self, PREMIUM_SETTING, pct)

    async def post_event(self, event: Event) -> bool:
        """이벤트를 로그 채널(과 경고 채널)에 올린다. 올릴 곳이 없으면 경고·오류는 관리자 DM으로 보낸다."""
        log_id = self.log_settings.get(LOG_CHANNEL)
        alert_id = self.log_settings.get(ALERT_CHANNEL)
        role_id = int(self.log_settings[ALERT_ROLE]) if self.log_settings.get(ALERT_ROLE) else None
        urgent = event.level == "error" or event.kind in NEEDS_ACTION
        targets: list[tuple[int, bool]] = []
        if log_id:
            targets.append((int(log_id), urgent and not alert_id))
        if alert_id and alert_id != log_id and event.level in ALERT_LEVELS:
            targets.append((int(alert_id), urgent))

        delivered = False
        for channel_id, mention in targets:
            delivered |= await self._send_card(channel_id, event, role_id if mention else None)
        if not delivered and event.level in ALERT_LEVELS:
            for admin_id in config.ADMIN_USERS:
                sent = await self.notify_user(
                    admin_id, view=event_card(event), report_failure=False,
                )
                delivered = bool(sent) or delivered
        return delivered

    async def _send_card(self, channel_id: int, event: Event, role_id: int | None) -> bool:
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            await channel.send(
                view=event_card(event, mention_role=role_id),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False,
                    roles=[discord.Object(role_id)] if role_id else False,
                ),
            )
            return True
        except discord.HTTPException as exc:
            log.warning("Cannot post event #%s to channel %s: %s", event.id, channel_id, exc,
                        extra={SKIP_EVENT: True})
            return False

    async def after_purchase(self, tx: Transaction) -> None:
        # 바이낸스가 출금을 접수했을 때 (백그라운드)
        await self.announce_purchase(tx)

    async def after_completion(self, tx: Transaction) -> None:
        # 출금이 완료됐을 때만 역할을 준다. 접수된 출금도 실패해서 환불될 수 있다
        await self.grant_buyer_role(tx.discord_id)

    async def grant_buyer_role(self, user_id: int) -> tuple[bool | None, str]:
        """구매자 역할을 준다. 실패하면 운영 로그에 남기고 예외는 올리지 않는다."""
        ok, line = await self._sync_role(
            BUYER_ROLE, BUYER_ROLE_GUILD, user_id, grant=True,
            reason="코인 구매", command="/관리자 구매자역할설정",
        )
        if ok is False:
            await self.events.record(
                "system.buyer_role_failed",
                f"<@{user_id}>님에게 구매자 역할을 주지 못했어요.\n{line}",
                level="warning",
                user_id=user_id,
                dedup="buyer_role_failed",
            )
        return ok, line

    async def announce_purchase(self, tx: Transaction) -> None:
        channel_id = self.log_settings.get(PURCHASE_CHANNEL)
        if not channel_id:
            return
        show_buyer = self.log_settings.get(PURCHASE_SHOW_BUYER) != "0"
        try:
            channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
            # 구매자는 보여 주되 멘션 알림은 가지 않게
            await channel.send(
                view=purchase_card(tx, show_buyer=show_buyer),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            log.warning("Cannot post purchase #%s to channel %s: %s", tx.id, channel_id, exc,
                        extra={SKIP_EVENT: True})
            await self.events.record(
                "system.purchase_failed",
                f"구매 알림 채널 <#{channel_id}>에 송금 #{tx.id} 구매 알림을 올리지 못했어요. "
                "채널이 지워졌거나 봇의 메시지 보내기 권한이 없어요.",
                level="warning",
                dedup=f"purchase:{channel_id}",
            )

    async def sync_depositor_role(self, user_id: int, *, grant: bool) -> tuple[bool | None, str]:
        return await self._sync_role(
            DEPOSITOR_ROLE, DEPOSITOR_ROLE_GUILD, user_id, grant=grant,
            reason="입금자명 지정" if grant else "입금자명 해제", command="/관리자 입금자역할설정",
        )

    async def grant_depositor_role(self, user_id: int) -> tuple[bool | None, str]:
        """본인인증으로 이름이 지정될 때 쓴다. 지켜보는 관리자가 없으니 실패는 운영 로그에 남긴다."""
        ok, line = await self.sync_depositor_role(user_id, grant=True)
        if ok is False:
            await self.events.record(
                "system.depositor_role_failed",
                f"<@{user_id}>님에게 입금자 역할을 주지 못했어요.\n{line}",
                level="warning",
                user_id=user_id,
                dedup="depositor_role_failed",
            )
        return ok, line

    async def _sync_role(
        self, role_key: str, guild_key: str, user_id: int, *, grant: bool, reason: str, command: str
    ) -> tuple[bool | None, str]:
        """role_key에 저장된 역할을 주거나(grant) 회수한다. 예외를 올리지 않는다.

        (True 성공 / False 실패 / None 건너뜀, 관리자 카드에 넣을 한 줄)을 돌려준다.
        역할 설정이 없으면 줄은 빈 문자열이다.
        """
        role_id, guild_id = self.log_settings.get(role_key), self.log_settings.get(guild_key)
        if not role_id or not guild_id:
            return None, ""
        guild = self.get_guild(int(guild_id))
        role = guild.get_role(int(role_id)) if guild else None
        if role is None:
            return False, f"역할을 찾지 못했어요 · {command} 으로 다시 정해 주세요"
        if grant and user_id in config.BLOCKED_USERS:
            return None, f"이용 정지 중이라 {role.mention} 역할은 주지 않았어요"
        # 설정한 뒤에 역할 권한이 늘었을 수 있어 줄 때마다 다시 본다
        problem = role_problem(role) if grant else None
        if problem:
            title, _, hint = problem
            return False, f"{role.mention} 역할을 주지 않았어요: {title} · {hint}"
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return None, f"서버에 없는 유저라 {role.mention} 역할은 주지 않았어요"
        except discord.HTTPException:
            log.warning("Cannot look up member %s for role %s", user_id, role_key, exc_info=True)
            return False, f"{role.mention} 역할 처리에 실패했어요 · 잠시 후 다시 시도해 주세요"
        has_role = any(r.id == role.id for r in member.roles)
        try:
            if grant and not has_role:
                await member.add_roles(role, reason=reason)
            elif not grant and has_role:
                await member.remove_roles(role, reason=reason)
        except discord.Forbidden:
            return False, (f"{role.mention} 역할을 바꿀 권한이 없어요 · "
                           "봇에게 역할 관리 권한을 주고 봇 역할을 이 역할보다 위로 올려 주세요")
        except discord.HTTPException:
            log.warning("Cannot change role %s of %s", role_key, user_id, exc_info=True)
            return False, f"{role.mention} 역할 처리에 실패했어요 · 잠시 후 다시 시도해 주세요"
        return True, f"{role.mention} " + ("부여" if grant else "회수")

    def panel_view(self, guild: discord.Guild | None = None) -> PanelLayout:
        # 썸네일: 설정한 이미지 > 봇 아바타 > 서버 아이콘. 기본 아바타는 디스코드 로고라 건너뛴다
        avatar = self.user.avatar.url if self.user and self.user.avatar else None
        icon = guild.icon.url if guild and guild.icon else None
        return PanelLayout(self.panel_status, config.PANEL_THUMBNAIL_URL or avatar or icon)

    async def refresh_panels(self) -> None:
        """올라가 있는 패널과 음성 채널 이름의 시세를 갱신한다."""
        panels = await self.db.list_panels()
        if not panels and not self.status_channels.targets():
            return
        self.panel_status = await build_panel_status(self)
        for panel in panels:
            await self._edit_panel(panel)
        # 채널 이름 변경은 백그라운드로 돌아서 패널 갱신을 붙잡지 않는다
        await self.status_channels.update(self.panel_status)

    async def _edit_panel(self, panel: Panel) -> None:
        message = self.get_partial_messageable(panel.channel_id).get_partial_message(
            panel.message_id
        )
        try:
            channel = self.get_channel(panel.channel_id)
            await message.edit(view=self.panel_view(getattr(channel, "guild", None)))
        except (discord.NotFound, discord.Forbidden):
            await self.db.remove_panel(panel.message_id)
            log.info("Panel %s is gone; it will no longer be refreshed", panel.message_id)
        except discord.HTTPException:
            log.warning("Failed to refresh panel %s", panel.message_id, exc_info=True)

    async def notify_user(
        self, discord_id: int, *, view: ui.LayoutView | None = None,
        report_failure: bool = True, **card: Any,
    ) -> bool:
        """유저에게 DM을 보낸다. view가 없으면 card로 안내 카드를 만든다. 실패해도 예외를 올리지 않는다."""
        try:
            user = self.get_user(discord_id) or await self.fetch_user(discord_id)
            if user.bot:
                log.info("Not sending a DM to bot account %s", discord_id)
                return False
            await user.send(view=view or notice(**card))
            return True
        except Exception:
            log.warning("Failed to DM user %s", discord_id, exc_info=True, extra={SKIP_EVENT: True})
            if report_failure:
                await self.events.record(
                    "system.dm_failed",
                    f"<@{discord_id}>님에게 DM을 보내지 못했어요. DM을 막아 두었을 수 있어요.",
                    level="warning",
                    user_id=discord_id,
                    dedup=f"dm:{discord_id}",
                )
            return False

    async def on_ready(self) -> None:
        log.info("Bot logged in as %s", self.user)
        if not self._started:
            self._started = True
            await self.events.record(
                "system.start",
                f"**{self.user}** 로그인 · 서버 {len(self.guilds)}곳 · "
                f"입금 마감 {config.DEPOSIT_TIMEOUT_MINUTES}분 · 수수료 {fee_percent(config.FEE_RATE)} · "
                f"추가 김프 {premium_percent(config.MIN_KIMCHI_PREMIUM_PCT)}",
            )

    async def close(self) -> None:
        # SIGTERM과 discord.py가 둘 다 close를 불러도 종료는 한 번만 한다
        if self._shutdown is None:
            self._shutdown = asyncio.ensure_future(self._shut_down())
        await asyncio.shield(self._shutdown)

    async def _shut_down(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self.status_channels.stop()
        if self._started:
            await self.events.record("system.stop", "봇이 종료됐어요 (재시작·업데이트 포함).")
        # 종료가 길어지면 강제로 죽으니 남은 로그는 잠깐만 기다린다
        await self.events.stop(flush_timeout=SHUTDOWN_FLUSH_SECONDS)
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        await self.webhook.stop()
        await self.prices.close()
        await self.binance.close()
        await self.db.close()
        await super().close()
