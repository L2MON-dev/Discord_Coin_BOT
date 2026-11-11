#!/usr/bin/env bash
# 검토한 봇 코드만 설치한다. 소스 폴더에 든 패키지를 root로 설치하는 일은 없다.
# root 소유로 /usr/local/sbin/coin-bot-deploy 에 두고 쓴다.
# --check: 운영 중인 봇은 건드리지 않고 검사와 변경 내용 출력만 한다.
set -euo pipefail
export LC_ALL=C.UTF-8
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

SRC="${COIN_BOT_SRC:-/home/ubuntu/coin-bot-hardening-20261108}"
APP=/opt/coin-bot/app
VENV=/opt/coin-bot/venv
DB=/var/lib/coin-bot/bot.db
ENV_FILE=/etc/coin-bot/coin-bot.env
SERVICE=coin-bot
HEALTH=http://127.0.0.1:804/api/health

yes=0 force=0 sync=0 check=0
for arg in "$@"; do
  case "$arg" in
    --yes) yes=1 ;;
    --force) force=1 ;;
    --sync-commands) sync=1 ;;
    --check) check=1 ;;
    *) echo "usage: coin-bot-deploy [--check] [--yes] [--force] [--sync-commands]" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "sudo로 실행해 주세요" >&2; exit 1; }
# 코드는 root와 서버 주인 계정만 넣을 수 있다. 다른 봇 계정이 상위 폴더를
# 바꿔치기할 수 없어야 한다 (ACL로 열린 그룹 쓰기 권한 포함).
SRC=$(realpath -e -- "$SRC")
trusted_uid=$(id -u ubuntu)
ancestor=$SRC
while :; do
  read -r owner mode < <(stat -c '%u %a' -- "$ancestor")
  if [[ $owner != 0 && $owner != "$trusted_uid" ]] || (( (8#$mode & 0022) != 0 )); then
    echo "신뢰할 수 없는 배포 경로: $ancestor (root/ubuntu 소유, 다른 계정 쓰기 금지 필요)" >&2
    exit 1
  fi
  [[ $ancestor == / ]] && break
  ancestor=$(dirname -- "$ancestor")
done
unsafe=$(find "$SRC" \( -type f -o -type d \) \
  \( \( ! -user root -a ! -user ubuntu \) -o -perm /0022 \) -print -quit)
[[ -z $unsafe ]] || { echo "다른 계정이 바꿀 수 있는 소스 항목: $unsafe" >&2; exit 1; }

# 배포는 한 번에 하나만. stage와 app은 같은 파일시스템이라 rename 한 번으로 바뀐다.
exec 9>/opt/coin-bot/.deploy.lock
flock -n 9 || { echo "다른 배포가 진행 중이에요" >&2; exit 1; }
stage=$(mktemp -d /opt/coin-bot/.stage.XXXXXX)
previous=/opt/coin-bot/previous-$(date -u +%Y%m%dT%H%M%SZ)-$$
stopped=0 saved=0 committed=0
cleanup() {
  rc=$?
  trap - EXIT
  if (( ! committed && stopped )); then
    systemctl stop "$SERVICE" || true
    if (( saved )); then
      if [[ -d $APP ]]; then
        mv -- "$APP" /opt/coin-bot/failed-$(date -u +%Y%m%dT%H%M%SZ)-$$
      fi
      mv -- "$previous" "$APP"
    fi
    systemctl start "$SERVICE" || echo "이전 서비스 재시작도 실패했어요. systemctl status coin-bot 확인 필요" >&2
    echo "배포를 완료하지 못해 이전 코드를 유지/복구했어요" >&2
  fi
  [[ ! -d $stage ]] || rm -rf -- "$stage"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 링크, 특수 파일, 캐시, 테스트, 환경 파일은 옮기지 않는다.
rsync -r --no-links --prune-empty-dirs --exclude='__pycache__/' \
  --include='/bot/' --include='/bot/**/' --include='/bot/**.py' \
  --include='/main.py' --include='/deploy_commands.py' \
  --include='/requirements.txt' --include='/requirements.lock' \
  --include='/scripts/' --include='/scripts/backup_db.py' \
  --exclude='*' "$SRC"/ "$stage"/
[[ -f $stage/main.py && -d $stage/bot && -f $stage/requirements.lock ]] \
  || { echo "$SRC 에 봇 코드나 requirements.lock이 없어요" >&2; exit 1; }

# 믿는 기준은 이미 설치돼 있는 root 소유의 의존성 목록이다. 소스 폴더 안의 해시는 승인이 아니다.
# 의존성을 바꾸려면 따로 검토해서 설치해야 하고, 이 스크립트에는 패키지를 설치하는 경로가 없다.
for manifest in requirements.lock requirements.txt; do
  if ! cmp -s "$stage/$manifest" "$APP/$manifest"; then
    echo "의존성 변경은 자동 배포하지 않아요: $manifest. 검토된 별도 패키지 갱신이 필요해요" >&2
    exit 1
  fi
done
chown -R root:root "$stage"
find "$stage" -type d -exec chmod 755 {} +
find "$stage" -type f -exec chmod 644 {} +
runuser -u coinbot -- /usr/bin/python3 -I - "$stage" <<'PY_CHECK'
import ast, pathlib, sys
for path in pathlib.Path(sys.argv[1]).rglob('*.py'):
    ast.parse(path.read_text(), filename=str(path))
print('Python 구문 검사 통과')
PY_CHECK

echo "== 배포할 변경 (실행 중 → 새 코드) =="
diff -ruN "$APP" "$stage" || [[ $? == 1 ]]
(( ! check )) || exit 0
if (( ! yes )); then
  read -r -p "이대로 배포할까요? (yes 입력) " answer
  [[ $answer == yes ]] || { echo "취소했어요"; exit 1; }
fi

check_idle() {
  (( ! force )) || return 0
  busy=$(runuser -u coinbot -- /usr/bin/python3 -I - "$DB" <<'PY_BUSY'
import sqlite3, sys
with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=10) as db:
    deposits = db.execute("SELECT COUNT(*) FROM deposits WHERE status = 'pending'").fetchone()[0]
    transfers = db.execute("SELECT COUNT(*) FROM transactions WHERE status IN ('pending', 'unknown')").fetchone()[0]
print(deposits, transfers)
PY_BUSY
)
  if [[ $busy != "0 0" ]]; then
    echo "열린 충전/진행 중 송금이 있어요 ($busy). 처리 후 다시 배포해 주세요" >&2
    return 1
  fi
}
check_idle
stopped=1
systemctl stop "$SERVICE"
# 멈춘 뒤에 한 번 더 본다. 첫 확인 뒤에 요청이 들어왔을 수 있다.
check_idle
runuser -u coinbot -- /usr/bin/python3 -I "$APP/scripts/backup_db.py" "$DB"
mv -- "$APP" "$previous"
saved=1
mv -- "$stage" "$APP"
systemctl start "$SERVICE"
healthy=0
for _ in $(seq 1 30); do
  sleep 1
  if systemctl is-active --quiet "$SERVICE" && curl -fsS -m 3 "$HEALTH" >/dev/null 2>&1; then
    healthy=1
    break
  fi
done
(( healthy )) || { echo "기동/상태 검사 실패, 이전 코드로 복구합니다" >&2; exit 1; }
committed=1

sum=$(cd "$APP" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
echo "$(date -u '+%F %T') UTC  $sum  from $SRC  previous $previous" >> /opt/coin-bot/DEPLOYS
if (( sync )); then
  systemd-run --quiet --wait --pipe --collect -p User=coinbot -p Group=coinbot \
    -p EnvironmentFile="$ENV_FILE" -p WorkingDirectory="$APP" \
    "$VENV/bin/python" -B -u deploy_commands.py
fi
echo "배포 완료 ($sum), 이전 코드: $previous"
