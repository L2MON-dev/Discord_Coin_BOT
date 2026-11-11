import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 설정을 읽다가 생긴 문제. validate()에서 한꺼번에 알려 준다
_errors: list[str] = []


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _errors.append(f"{name} 값이 정수가 아닙니다.")
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _errors.append(f"{name} 값이 숫자가 아닙니다.")
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    _errors.append(f"{name} 값은 true 또는 false여야 합니다.")
    return default


def _get_str(name: str, default: str = "") -> str:
    # 비워 둔 값(NAME=)은 없는 값으로 친다. os.getenv의 기본값은 빈 문자열에는 적용되지 않는다
    return os.getenv(name, "").strip() or default


def _get_path(name: str, default: str = "") -> str:
    # 상대 경로는 프로젝트 폴더 기준
    raw = _get_str(name, default)
    return str(PROJECT_ROOT / raw) if raw else ""


def _parse_symbols(name: str, default: str) -> list[str]:
    # 쉼표로 구분, 대문자로, 중복은 처음 것만
    parts = (part.strip().upper() for part in _get_str(name, default).split(","))
    return list(dict.fromkeys(part for part in parts if part))


def _parse_id_set(name: str) -> set[int]:
    ids: set[int] = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            ids.add(int(part))
        else:
            _errors.append(f"{name}에 숫자가 아닌 ID가 있습니다.")
    return ids


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_BASE_URL = "https://api.binance.com"

# 기본값일 뿐이고 /수수료로 바꾼 값(DB)이 우선한다
FEE_RATE = _get_float("FEE_RATE", 0.02)
DEPOSIT_TIMEOUT_MINUTES = _get_int("DEPOSIT_TIMEOUT_MINUTES", 5)
MIN_DEPOSIT_KRW = 2_500
# 판매가 = 바이낸스 시세 × 환율 × (1 + 시장 김프 + 이 값(%))
# 이것도 /김프로 바꾼 값이 우선한다
MIN_KIMCHI_PREMIUM_PCT = _get_float("MIN_KIMCHI_PREMIUM_PCT", 0.05)
# 송금 한도(원). 0이면 무제한이고 일 한도는 최근 24시간 기준
MAX_TRANSFER_KRW = _get_int("MAX_TRANSFER_KRW", 1_000_000)
DAILY_TRANSFER_LIMIT_KRW = _get_int("DAILY_TRANSFER_LIMIT_KRW", 3_000_000)
# 첫 송금 후 NEW_USER_DAYS일이 지나기 전까지는 24시간 한도를 낮춘다 (0이면 끔, 관리자는 제외)
# 사기 피해금은 들어오자마자 빠져나가므로 새 계정이 큰돈을 옮기지 못하게 한다
NEW_USER_DAILY_LIMIT_KRW = _get_int("NEW_USER_DAILY_LIMIT_KRW", 300_000)
NEW_USER_DAYS = _get_int("NEW_USER_DAYS", 7)
# 누적 송금이 이 금액이 되기 전에도 신규로 본다 (0이면 기간만 본다)
NEW_USER_MIN_TOTAL_KRW = _get_int("NEW_USER_MIN_TOTAL_KRW", 0)
# 관리자를 포함한 전체 유저의 24시간 송금 합계 상한 (0이면 무제한)
# 관리자 계정이나 웹훅 키가 털려도 하루에 나갈 돈을 묶어 두는 값이라 디스코드에서는 못 바꾼다
GLOBAL_DAILY_LIMIT_KRW = _get_int("GLOBAL_DAILY_LIMIT_KRW", 10_000_000)
# 바이낸스가 거절한 출금은 전액 환불되지만 그때마다 코인을 사고 경고가 올라간다
# REJECTED_WINDOW_MINUTES분 안에 이만큼 거절되면 그 유저의 송금을 잠시 막는다 (0이면 끔, 관리자는 제외)
MAX_REJECTED_TRANSFERS = _get_int("MAX_REJECTED_TRANSFERS", 3)
REJECTED_WINDOW_MINUTES = _get_int("REJECTED_WINDOW_MINUTES", 60)
# 자동 매수에 쓸 USDT+USD가 이 아래로 내려가면 경고 (0이면 끔)
LOW_FUNDS_USDT = _get_float("LOW_FUNDS_USDT", 10)
# 판매하는 코인과 메뉴 순서. 바이낸스에서 출금이 열려 있을 때만 보인다
TRANSFER_COINS = _parse_symbols("TRANSFER_COINS", "USDT,BTC,ETH,XRP,LTC,SOL,TRX,BNB")
# 셀렉트 메뉴 하나에 옵션 25개까지
MAX_TRANSFER_COINS = 25

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
MIN_WEBHOOK_SECRET_LENGTH = 32
# true면 X-Timestamp·X-Signature 헤더(webhook_server.signature_for)가 있어야 받고,
# 본문에 key만 넣어 보내는 방식은 거절한다
WEBHOOK_SIGNED = _get_bool("WEBHOOK_SIGNED", False)
# 서명된 요청의 시각이 이보다 어긋나면 거절
WEBHOOK_MAX_SKEW_SECONDS = 5 * 60
API_HOST = _get_str("API_HOST", "127.0.0.1")
API_PORT = _get_int("API_PORT", _get_int("PORT", 8080))
# 웹훅을 직접 HTTPS로 받을 때만
API_SSL_CERT = _get_path("API_SSL_CERT")
API_SSL_KEY = _get_path("API_SSL_KEY")

# 인증 봇이 /api/verified로 보내는 요청의 서명 키. 비워 두면 연동을 끈다
# 봇 매니저 계정이 읽는 값이라 WEBHOOK_SECRET과 같은 값을 쓰면 안 된다
VERIFY_LINK_SECRET = _get_str("VERIFY_LINK_SECRET")

# 패널 맨 위 배너 이미지 (https, 선택)
PANEL_BANNER_URL = os.getenv("PANEL_BANNER_URL", "").strip()
# 시세 옆에 들어가는 정사각형 이미지 (https, 선택). 없으면 봇 아바타
PANEL_THUMBNAIL_URL = os.getenv("PANEL_THUMBNAIL_URL", "").strip()

BANK_NAME = os.getenv("BANK_NAME", "")
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "")
BANK_HOLDER = os.getenv("BANK_HOLDER", "")

ALLOWED_USERS = _parse_id_set("ALLOWED_USERS")
ADMIN_USERS = _parse_id_set("ADMIN_USERS")
# /관리자 이용정지로 막은 유저. 시작할 때 DB에서 읽고, ALLOWED_USERS에 있어도 막힌다
BLOCKED_USERS: set[int] = set()

DB_PATH = _get_path("DB_PATH", "data/bot.db")
# scripts/backup_db.py가 매일 복사본을 두는 곳
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "backups")

# 카드 왼쪽 띠 색. 상태를 알릴 때만 쓰고 일반 카드는 띠가 없다
BRAND_COLOR = 0xFFF01F
PANEL_COLOR = 0xFFFFFF
SUCCESS_COLOR = 0x39FF14
WARNING_COLOR = 0xFF9F1C
ERROR_COLOR = 0xFF073A

_PLACEHOLDER_WORDS = ("change", "your", "example", "placeholder", "secret")


def validate() -> list[str]:
    """시작 전에 고쳐야 할 설정 문제들."""
    problems = list(_errors)
    if not DISCORD_TOKEN:
        problems.append("DISCORD_TOKEN이 설정되지 않았습니다.")
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        problems.append("BINANCE_API_KEY와 BINANCE_API_SECRET을 설정해야 합니다.")
    lowered = WEBHOOK_SECRET.lower()
    if len(WEBHOOK_SECRET) < MIN_WEBHOOK_SECRET_LENGTH or any(
        word in lowered for word in _PLACEHOLDER_WORDS
    ):
        problems.append(
            f"WEBHOOK_SECRET은 추측할 수 없는 {MIN_WEBHOOK_SECRET_LENGTH}자 이상의 "
            "무작위 값이어야 합니다. 생성: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if VERIFY_LINK_SECRET:
        if len(VERIFY_LINK_SECRET) < MIN_WEBHOOK_SECRET_LENGTH or any(
            word in VERIFY_LINK_SECRET.lower() for word in _PLACEHOLDER_WORDS
        ):
            problems.append(
                f"VERIFY_LINK_SECRET은 추측할 수 없는 {MIN_WEBHOOK_SECRET_LENGTH}자 이상의 "
                "무작위 값이어야 합니다 (쓰지 않으려면 비워 두세요)."
            )
        elif VERIFY_LINK_SECRET == WEBHOOK_SECRET:
            problems.append("VERIFY_LINK_SECRET은 WEBHOOK_SECRET과 다른 값이어야 합니다.")
    if not 0 <= FEE_RATE < 0.5:
        problems.append("FEE_RATE는 0 이상 0.5 미만이어야 합니다 (0.02 = 2%).")
    if not 0 <= MIN_KIMCHI_PREMIUM_PCT < 20:
        problems.append("MIN_KIMCHI_PREMIUM_PCT는 0 이상 20 미만이어야 합니다 (0.05 = 0.05%).")
    if DEPOSIT_TIMEOUT_MINUTES < 1:
        problems.append("DEPOSIT_TIMEOUT_MINUTES는 1 이상이어야 합니다.")
    if MAX_TRANSFER_KRW < 0 or DAILY_TRANSFER_LIMIT_KRW < 0:
        problems.append("MAX_TRANSFER_KRW와 DAILY_TRANSFER_LIMIT_KRW는 0(무제한) 이상이어야 합니다.")
    if NEW_USER_DAILY_LIMIT_KRW < 0 or NEW_USER_DAYS < 0 or NEW_USER_MIN_TOTAL_KRW < 0:
        problems.append(
            "NEW_USER_DAILY_LIMIT_KRW, NEW_USER_DAYS, NEW_USER_MIN_TOTAL_KRW는 0(끔) 이상이어야 합니다."
        )
    if GLOBAL_DAILY_LIMIT_KRW < 0:
        problems.append("GLOBAL_DAILY_LIMIT_KRW는 0(무제한) 이상이어야 합니다.")
    if MAX_REJECTED_TRANSFERS < 0 or REJECTED_WINDOW_MINUTES < 0:
        problems.append("MAX_REJECTED_TRANSFERS와 REJECTED_WINDOW_MINUTES는 0(끔) 이상이어야 합니다.")
    if LOW_FUNDS_USDT < 0:
        problems.append("LOW_FUNDS_USDT는 0(끔) 이상이어야 합니다.")
    if not 1 <= len(TRANSFER_COINS) <= MAX_TRANSFER_COINS:
        problems.append(f"TRANSFER_COINS에는 코인을 1개 이상 {MAX_TRANSFER_COINS}개 이하로 적어야 합니다.")
    if not 1 <= API_PORT <= 65535:
        problems.append("API_PORT가 올바르지 않습니다.")
    if bool(API_SSL_CERT) != bool(API_SSL_KEY):
        problems.append("API_SSL_CERT와 API_SSL_KEY는 함께 설정해야 합니다.")
    if PANEL_BANNER_URL and not PANEL_BANNER_URL.startswith("https://"):
        problems.append("PANEL_BANNER_URL은 https:// 로 시작하는 이미지 주소여야 합니다.")
    if PANEL_THUMBNAIL_URL and not PANEL_THUMBNAIL_URL.startswith("https://"):
        problems.append("PANEL_THUMBNAIL_URL은 https:// 로 시작하는 이미지 주소여야 합니다.")
    if not ADMIN_USERS:
        problems.append("ADMIN_USERS가 비어 있습니다. 입금자명 지정에 관리자가 필요합니다.")
    return problems
