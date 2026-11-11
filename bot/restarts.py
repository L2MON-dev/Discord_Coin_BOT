"""시작에 계속 실패할 때 재시작 간격을 벌린다.

systemd는 실패 10초 뒤에 몇 번이고 다시 띄운다 (scripts/coin-bot.service). 그런데 시작할 때마다
디스코드에 로그인하고, 로그인이 하루 1000번을 넘으면 디스코드가 봇 토큰을 초기화해 버린다.
그래서 실패가 이어지면 로그인 전에 기다리고, 그 시간을 MAX_DELAY까지 두 배씩 늘린다.

정상 종료(SIGTERM: 배포, systemctl stop/restart)하면 기록을 지우므로 다음 시작은 기다리지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time

log = logging.getLogger(__name__)

# WINDOW 안에서 이만큼까지는 기다리지 않고 시작한다
FREE_STARTS = 3
# 그다음 시작이 기다리는 시간(초). 실패가 이어질 때마다 두 배
FIRST_DELAY = 15
MAX_DELAY = 300
WINDOW = 60 * 60


def delay_for(starts: list[float], now: float) -> int:
    """이번 시작 전에 기다릴 시간(초). starts는 정상 종료하지 못한 이전 시작 시각들."""
    recent = sum(1 for t in starts if now - WINDOW < t <= now)
    if recent < FREE_STARTS:
        return 0
    return min(FIRST_DELAY * 2 ** (recent - FREE_STARTS), MAX_DELAY)


class StartRecord:
    """정상 종료하지 못한 시작 시각들. DB 옆의 작은 JSON 파일에 둔다."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _load(self) -> list[float]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("Unreadable start record %s; starting afresh", self.path)
            return []
        if not isinstance(data, list):
            return []
        return [float(t) for t in data if isinstance(t, (int, float)) and not isinstance(t, bool)]

    def _save(self, starts: list[float]) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(starts, f)
        os.replace(tmp, self.path)

    def begin(self) -> int:
        """이번 시작을 기록하고, 필요하면 기다린다. 기다린 시간(초)을 돌려준다.

        기다리는 중에 SIGTERM이 오면(배포 등) 기록을 지우고 그대로 종료한다.
        """
        now = time.time()
        starts = [t for t in self._load() if now - WINDOW < t <= now]
        delay = delay_for(starts, now)
        try:
            self._save(starts + [now])
        except OSError:
            log.warning("Cannot write the start record %s", self.path, exc_info=True)
        if not delay:
            return 0
        log.warning(
            "Started %d times in the last hour without a clean stop: waiting %d s before logging in "
            "to Discord (too many logins in a day get the bot token reset)", len(starts), delay,
        )
        previous = signal.signal(signal.SIGTERM, self._stopped)
        try:
            time.sleep(delay)
        finally:
            signal.signal(signal.SIGTERM, previous)
        return delay

    def end(self) -> None:
        """정상 종료했을 때 부른다. 다음 시작은 기다리지 않는다."""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Cannot clear the start record %s", self.path, exc_info=True)

    def _stopped(self, signum: int, _frame: object) -> None:
        self.end()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
