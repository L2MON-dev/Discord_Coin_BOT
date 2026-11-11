"""봇 DB를 <DB 폴더>/backups 에 복사하고 최근 것만 남긴다.

서버 cron이 봇 계정으로 하루 한 번 돌린다 (/etc/cron.d/coin-bot-backup, 04:30 KST = 19:30 UTC):
    30 19 * * * coinbot /opt/coin-bot/venv/bin/python -I /opt/coin-bot/app/scripts/backup_db.py \
        /var/lib/coin-bot/bot.db >> /var/lib/coin-bot/backups/backup.log 2>&1

SQLite 온라인 백업이라 봇이 쓰는 중에도 복사본이 깨지지 않는다. 표준 라이브러리만 쓴다.
최근 복사본이 하루 넘게 없으면 봇이 일일 요약에서 알려 준다 (bot/services/monitor.py).
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
BACKUP_FOLDER = "backups"  # DB 옆 폴더. bot/config.py의 BACKUP_DIR과 같아야 한다
DEFAULT_KEEP = 30


def backup(db_path: str, keep: int = DEFAULT_KEEP) -> str:
    """검사를 통과한 복사본 하나를 만들고 keep개를 넘는 오래된 것은 지운다. 복사본 경로를 돌려준다."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"no database at {db_path}")
    folder = os.path.join(os.path.dirname(os.path.abspath(db_path)), BACKUP_FOLDER)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    target = os.path.join(folder, f"bot-{datetime.now(KST):%Y%m%d-%H%M}.db")
    partial = target + ".part"

    # mode=rw: 경로가 틀렸을 때 빈 DB를 새로 만들지 않게
    source = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=30)
    copy = sqlite3.connect(partial)
    try:
        source.backup(copy)
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
        source.close()
    if result != "ok":
        os.remove(partial)
        raise RuntimeError(f"the copy failed its integrity check: {result}")
    os.chmod(partial, 0o600)
    os.replace(partial, target)

    copies = sorted(name for name in os.listdir(folder) if name.startswith("bot-") and name.endswith(".db"))
    for old in copies[:-keep]:
        os.remove(os.path.join(folder, old))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db_path", help="봇 DB 파일 (DB_PATH)")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="남길 복사본 수 (기본 30)")
    args = parser.parse_args()
    os.umask(0o077)  # 잔액과 이름이 들어 있으니 소유자만 읽게
    stamp = f"{datetime.now(KST):%Y-%m-%d %H:%M:%S} KST"
    try:
        target = backup(args.db_path, max(args.keep, 1))
    except Exception as exc:  # noqa: BLE001
        print(f"{stamp} backup FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"{stamp} backup ok: {target} ({os.path.getsize(target):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
