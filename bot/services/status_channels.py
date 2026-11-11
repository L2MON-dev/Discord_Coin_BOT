# 재고와 김프를 음성 채널 이름으로 보여 준다 (/관리자 시세채널설정)
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import discord

from .event_log import SKIP_EVENT

if TYPE_CHECKING:
    from ..views.main_panel import PanelStatus

log = logging.getLogger(__name__)

# settings 키
STOCK_CHANNEL = "status_stock_channel"
PREMIUM_CHANNEL = "status_premium_channel"
STOCK_FORMAT = "status_stock_format"
PREMIUM_FORMAT = "status_premium_format"
DEFAULT_STOCK_FORMAT = "📦 재고 {재고}"
DEFAULT_PREMIUM_FORMAT = "📈 김프 {김프}"
PLACEHOLDERS = ("{재고}", "{김프}", "{USDT}", "{환율}")
# 디스코드는 채널 이름을 10분에 2번까지만 바꿔 준다. 채널마다 이 간격에 한 번만 바꾼다
RENAME_INTERVAL_SECONDS = 5 * 60
# 디스코드가 붙잡고 있는 변경(재시작 직후 등)은 버리고 다음 간격에 다시 한다
RENAME_TIMEOUT_SECONDS = 30
MAX_NAME_LENGTH = 100


def figures(status: PanelStatus | None) -> dict[str, str | None]:
    """자리표시자별 값. 패널과 같은 표기를 쓰고, 아직 모르는 값은 None."""
    if status is None:
        return dict.fromkeys(PLACEHOLDERS)
    # 패널과 마찬가지로 환율을 못 받으면 김프도 보여 주지 않는다
    premium_known = status.fx_rate is not None and status.btc_applied is not None
    return {
        "{재고}": f"₩{status.stock_krw:,.0f}" if status.stock_krw is not None else None,
        "{김프}": f"+{status.btc_applied:.2f}%" if premium_known else None,
        "{USDT}": f"₩{status.usdt_krw:,.0f}" if status.usdt_krw is not None else None,
        "{환율}": f"₩{status.fx_rate:,.2f}" if status.fx_rate is not None else None,
    }


def render(name_format: str, status: PanelStatus | None) -> str | None:
    """이 형식으로 만든 채널 이름. 들어갈 값을 아직 모르면 None (틀린 값을 보여 주느니 예전 이름을 둔다)."""
    name = name_format
    for placeholder, value in figures(status).items():
        if placeholder in name:
            if value is None:
                return None
            name = name.replace(placeholder, value)
    name = " ".join(name.split())[:MAX_NAME_LENGTH]
    return name or None


class StatusChannels:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._last: dict[int, float] = {}  # 채널 id -> 마지막으로 이름 변경을 시작한 시각
        self._tasks: dict[int, asyncio.Task] = {}

    def targets(self) -> list[tuple[int, str]]:
        """설정된 채널들의 (채널 id, 이름 형식)"""
        settings = self._bot.log_settings
        found = []
        for channel_key, format_key, default in (
            (STOCK_CHANNEL, STOCK_FORMAT, DEFAULT_STOCK_FORMAT),
            (PREMIUM_CHANNEL, PREMIUM_FORMAT, DEFAULT_PREMIUM_FORMAT),
        ):
            raw = settings.get(channel_key)
            if raw and str(raw).isdigit():
                found.append((int(raw), settings.get(format_key) or default))
        return found

    async def update(self, status: PanelStatus | None) -> None:
        """이름이 달라진 채널의 변경을 시작한다. 끝나기를 기다리지 않는다."""
        is_ready = getattr(self._bot, "is_ready", None)
        if callable(is_ready) and not is_ready():
            # 시작 직후에는 서버 정보가 아직 안 와서 채널이 전부 없는 것처럼 보인다
            return
        now = time.monotonic()
        for channel_id, name_format in self.targets():
            name = render(name_format, status)
            if name is None:
                continue
            running = self._tasks.get(channel_id)
            if running is not None and not running.done():
                continue
            if now - self._last.get(channel_id, float("-inf")) < RENAME_INTERVAL_SECONDS:
                continue
            channel = self._bot.get_channel(channel_id)
            if channel is not None and channel.name == name:
                continue  # 바꿀 게 없으면 간격을 쓰지 않는다
            self._last[channel_id] = now
            self._tasks[channel_id] = asyncio.create_task(self._sync(channel_id, channel, name))

    async def _sync(self, channel_id: int, channel: Any, name: str) -> None:
        if channel is None:
            channel, gone = await self._fetch(channel_id)
            if channel is None:
                if gone:
                    await self._bot.events.record(
                        "system.status_channel",
                        f"재고·김프를 보여 줄 음성 채널(`{channel_id}`)을 찾지 못했어요. 지워졌거나 봇이 볼 수 "
                        "없는 채널이에요 · `/관리자 시세채널설정`으로 다시 정해 주세요.",
                        level="warning",
                        dedup=f"status_channel:{channel_id}",
                    )
                return
            if channel.name == name:
                return
        await self._rename(channel, name)

    async def _fetch(self, channel_id: int) -> tuple[Any, bool]:
        """캐시에 없는 채널을 디스코드에 물어본다: (채널 또는 None, 정말 없는 채널인지). 일시적 오류면 다음에 다시 시도한다."""
        fetch = getattr(self._bot, "fetch_channel", None)
        if fetch is None:
            return None, True
        try:
            return await asyncio.wait_for(fetch(channel_id), RENAME_TIMEOUT_SECONDS), False
        except (discord.NotFound, discord.Forbidden):
            return None, True
        except Exception:
            log.info("Looking up status channel %s failed; trying again later", channel_id, exc_info=True)
            return None, False

    async def _rename(self, channel: Any, name: str) -> None:
        try:
            await asyncio.wait_for(channel.edit(name=name, reason="재고·김프 갱신"), RENAME_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.info("Renaming channel %s waits on Discord's limit; trying again later", channel.id)
        except discord.Forbidden:
            await self._bot.events.record(
                "system.status_channel",
                f"<#{channel.id}> 채널 이름을 바꿀 권한이 없어요. 봇에게 이 채널의 '채널 관리' 권한을 주세요.",
                level="warning",
                dedup=f"status_channel:{channel.id}",
            )
        except Exception:
            log.warning("Renaming channel %s failed", channel.id, exc_info=True, extra={SKIP_EVENT: True})

    def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
