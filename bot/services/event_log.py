from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from typing import Any, Awaitable, Callable

from ..database import Database, Event

log = logging.getLogger(__name__)

LEVELS = ("info", "success", "warning", "error")
ALERT_LEVELS = ("warning", "error")
# 같은 dedup 키의 이벤트는 이 시간에 한 번만 올리고 나머지는 횟수로 요약한다
DEDUP_WINDOW_SECONDS = 10 * 60
QUEUE_SIZE = 500
# 일반 기록(info, success)은 큐를 여기까지만 채운다. 한꺼번에 몰려도 경고가 밀리지 않게
ROUTINE_QUEUE_LIMIT = 400
# 디스코드 전송 간격 (속도 제한 방지)
POST_INTERVAL_SECONDS = 0.5
RETRY_POLL_SECONDS = 1
MAX_MESSAGE_LENGTH = 1500

# 이미 이벤트로 남긴 문제를 로그로도 찍을 때는 extra={SKIP_EVENT: True}를 준다
SKIP_EVENT = "skip_event"

Sink = Callable[[Event], Awaitable[bool | None]]


def exception_details(exc: BaseException | None) -> dict[str, str]:
    if exc is None:
        return {}
    return {"오류": f"{type(exc).__name__}: {exc}"[:300], "traceback": "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )}


class EventLog:
    """운영 로그. 이벤트를 DB에 저장한 뒤 순서대로 디스코드에 올린다.

    저장에 실패해도 예외를 올리지 않고 일반 로그에만 남긴다. 기록 때문에 원래 작업이 깨지면 안 된다.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._sink: Sink | None = None
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._worker: asyncio.Task | None = None
        self._retry_worker: asyncio.Task | None = None
        self._queued_ids: set[int] = set()
        # dedup 키 -> [구간 시작, 이 구간에서 생략한 횟수]
        self._dedup: dict[str, list[float]] = {}
        self.dropped = 0

    def set_sink(self, sink: Sink | None) -> None:
        self._sink = sink

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="event-log")
            self._retry_worker = asyncio.create_task(self._retry(), name="event-log-retry")

    async def stop(self, flush_timeout: float = 5.0) -> None:
        """큐에 남은 것을 잠깐 기다려 올린 뒤 멈춘다."""
        if self._worker is None:
            return
        if self._retry_worker is not None:
            self._retry_worker.cancel()
            try:
                await self._retry_worker
            except asyncio.CancelledError:
                pass
            self._retry_worker = None
        try:
            await asyncio.wait_for(self._queue.join(), flush_timeout)
        except asyncio.TimeoutError:
            pass
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    async def record(
        self,
        kind: str,
        message: str,
        *,
        level: str = "info",
        user_id: int | None = None,
        ref: tuple[str, int | None] | None = None,
        details: dict[str, Any] | None = None,
        dedup: str | None = None,
    ) -> Event | None:
        """이벤트를 저장하고 전송 큐에 넣는다. dedup으로 생략되면 None."""
        if level not in LEVELS:
            level = "info"
        if dedup is not None:
            now = time.monotonic()
            # 키가 유저·로그 줄마다 생기므로 지난 구간은 지운다
            for key in [k for k, w in self._dedup.items() if now - w[0] >= DEDUP_WINDOW_SECONDS]:
                if key != dedup:
                    del self._dedup[key]
            window = self._dedup.get(dedup)
            if window is not None and now - window[0] < DEDUP_WINDOW_SECONDS:
                window[1] += 1
                return None
            if window is not None and window[1]:
                message += (
                    f"\n-# 직전 {DEDUP_WINDOW_SECONDS // 60}분 동안 같은 일이 "
                    f"{int(window[1])}번 더 있었어요"
                )
            self._dedup[dedup] = [now, 0]

        message = message if len(message) <= MAX_MESSAGE_LENGTH else message[:MAX_MESSAGE_LENGTH - 1] + "…"
        ref_type, ref_id = ref if ref else (None, None)
        payload = json.dumps(details, ensure_ascii=False, default=str) if details else None
        try:
            event = await self._db.add_event(
                kind, level, message,
                user_id=user_id, ref_type=ref_type, ref_id=ref_id, details=payload,
            )
        except Exception:
            log.warning("Failed to store event %s: %s", kind, message, exc_info=True,
                        extra={SKIP_EVENT: True})
            event = Event(0, kind, level, message, user_id, ref_type, ref_id, payload)
        if level not in ALERT_LEVELS and self._queue.qsize() >= ROUTINE_QUEUE_LIMIT:
            self.dropped += 1
            return event
        if not self._enqueue(event):
            self.dropped += 1
        return event

    def _enqueue(self, event: Event) -> bool:
        if event.id and event.id in self._queued_ids:
            return True
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return False  # 경고는 DB에 남아 있어 나중에 다시 보낸다
        if event.id:
            self._queued_ids.add(event.id)
        return True

    async def _retry(self) -> None:
        while True:
            try:
                for event in await self._db.pending_event_deliveries():
                    if not self._enqueue(event):
                        break
            except Exception:
                log.warning("Cannot read pending alert deliveries", exc_info=True,
                            extra={SKIP_EVENT: True})
            await asyncio.sleep(RETRY_POLL_SECONDS)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if self._sink is None:
                    raise RuntimeError("No event destination available")
                if await self._sink(event) is False:
                    raise RuntimeError("No event destination accepted the message")
                if event.id and event.level in ALERT_LEVELS:
                    await self._db.finish_event_delivery(event.id, delivered=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Failed to post event #%s", event.id, exc_info=True,
                            extra={SKIP_EVENT: True})
                if event.id and event.level in ALERT_LEVELS:
                    try:
                        await self._db.finish_event_delivery(event.id, delivered=False)
                    except Exception:
                        log.warning("Cannot defer alert #%s; it remains pending", event.id,
                                    exc_info=True, extra={SKIP_EVENT: True})
            finally:
                self._queued_ids.discard(event.id)
                self._queue.task_done()
            await asyncio.sleep(POST_INTERVAL_SECONDS)


class NullEventLog:
    # 이벤트 로그 없이 서비스를 쓸 때 (스크립트, 테스트)
    async def record(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class EventLogHandler(logging.Handler):
    """ERROR 로그와 traceback이 붙은 WARNING 로그를 이벤트로 바꾼다."""

    def __init__(self, events: EventLog, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(logging.WARNING)
        self._events = events
        self._loop = loop
        self._root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _short_path(self, path: str) -> str:
        try:
            relative = os.path.relpath(path, self._root)
        except ValueError:  # 윈도우에서 드라이브가 다를 때
            return path
        return path if relative.startswith("..") else relative

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, SKIP_EVENT, False):
            return
        if record.levelno < logging.ERROR and not record.exc_info:
            return
        if record.name == __name__:
            return  # 이벤트 로그 자신의 실패는 다시 넣지 않는다
        try:
            exc = record.exc_info[1] if record.exc_info else None
            details: dict[str, Any] = {
                "위치": f"{self._short_path(record.pathname)}:{record.lineno} ({record.funcName})",
                "로거": record.name,
            }
            details.update(exception_details(exc))
            coro = self._events.record(
                "system.error",
                record.getMessage(),
                level="error" if record.levelno >= logging.ERROR else "warning",
                details=details,
                dedup=f"log:{record.name}:{record.msg}:{type(exc).__name__ if exc else ''}",
            )
            try:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:
                coro.close()  # 종료 중이라 루프가 없다
        except Exception:
            self.handleError(record)
