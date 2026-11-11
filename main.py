import logging
import os
import sys

from bot import config
from bot.bot import CoinBot
from bot.restarts import StartRecord


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 최근 시작이 계속 실패했다면 여기서 먼저 기다린다 (bot/restarts.py)
    starts = StartRecord(os.path.join(os.path.dirname(config.DB_PATH), "start-record.json"))
    starts.begin()

    problems = config.validate()
    if problems:
        sys.exit("설정을 확인해주세요:\n" + "\n".join(f"- {p}" for p in problems))

    # 로깅은 위에서 설정했으니 discord.py가 핸들러를 또 붙이지 않게 한다
    CoinBot().run(config.DISCORD_TOKEN, log_handler=None)
    # 정상 종료(SIGTERM)일 때만 여기까지 온다. 죽으면 예외가 나서 시작 기록이 남는다
    starts.end()


if __name__ == "__main__":
    main()
