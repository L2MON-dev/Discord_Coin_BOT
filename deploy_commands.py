"""슬래시 명령을 디스코드에 등록한다. 명령을 추가하거나 바꾼 뒤에 한 번 실행한다."""
import asyncio
import logging
import sys

import discord
from discord import app_commands

from bot import config
from bot.commands import register_commands


async def main() -> None:
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree)

    print("슬래시 명령 등록 중")
    async with client:
        await client.login(config.DISCORD_TOKEN)
        try:
            await tree.sync()
            print("등록 완료")
        except discord.HTTPException as error:
            print(f"등록 실패: {error}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    if not config.DISCORD_TOKEN:
        sys.exit("DISCORD_TOKEN이 .env 파일에 설정되지 않았습니다.")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
