from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Iterable

import aiosqlite

from .errors import GlobalLimitError, RejectedLimitError, ServicePausedError, UserFacingError


@dataclass
class User:
    discord_id: int
    balance: int = 0
    total_deposited: int = 0
    total_spent: int = 0
    created_at: str = ""


@dataclass
class DepositorName:
    discord_id: int
    name: str
    name_key: str
    assigned_by: int
    assigned_at: str = ""


@dataclass
class UserBlock:
    discord_id: int
    reason: str
    blocked_by: int
    blocked_at: str = ""


@dataclass
class UserLimit:
    # 관리자가 정해 준 신규 유저 전용 24시간 한도 (set_new_user_limit)
    discord_id: int
    new_user_limit: int
    set_by: int
    set_at: str = ""


@dataclass
class NewUserStatus:
    # 신규 한도 기준으로 본 유저의 현재 상태 (new_user_status)
    cap: int  # 지금 걸리는 신규 한도(원). 0이면 없음
    is_new: bool
    first_transfer_at: str | None  # 실패하지 않은 첫 송금 시각 (UTC)
    total_krw: int  # 실패하지 않은 송금 합계
    spent_24h: int
    own_limit: UserLimit | None  # 관리자가 따로 정한 한도


@dataclass
class ProfitTotals:
    # 완료된 고객 송금의 기간별 합계 (profit_report)
    transfers: int = 0
    krw: int = 0  # 고객이 낸 돈
    fees: int = 0  # 그중 수수료
    # 낸 돈에서 코인의 시장 가치를 뺀 값. 시장 가치 기록이 없는 예전 송금은 수수료만 센다
    profit: int = 0
    measured: int = 0  # 시장 가치가 기록된 송금 수


@dataclass
class ProfitReport:
    periods: dict[str, ProfitTotals]
    admin_transfers: int = 0  # 관리자 본인 송금. 이익에는 넣지 않는다
    admin_krw: int = 0
    open_transfers: int = 0  # 아직 끝나지 않은 고객 송금
    open_krw: int = 0


@dataclass
class Deposit:
    id: int | None
    discord_id: int
    amount: int
    depositor_name: str
    status: str = "pending"
    channel_id: int | None = None
    message_id: int | None = None
    created_at: str = ""
    completed_at: str | None = None
    expiry_logged: int = 0


@dataclass
class BankNotification:
    id: int
    depositor_name: str
    name_key: str
    amount: int
    status: str  # matched | unmatched | resolved | dismissed | reversal(입금 취소)
    deposit_id: int | None = None
    credited_user_id: int | None = None
    resolved_by: int | None = None
    received_at: str = ""
    resolved_at: str | None = None
    sms_key: str | None = None  # 같은 문자인지 가리는 키 (api/bank_sms.py)
    owner_id: int | None = None  # 입금자명의 주인. 목록 조회에서만 채워진다


@dataclass
class Transaction:
    id: int | None
    discord_id: int
    coin: str
    network: str
    address: str
    memo: str | None
    coin_amount: str
    krw_amount: int
    fee_krw: int
    exchange_rate: float
    binance_withdraw_id: str | None = None
    created_at: str = ""
    status: str = "pending"  # pending | unknown | submitted | completed | failed
    withdraw_order_id: str | None = None
    updated_at: str | None = None
    error: str | None = None
    buy_order_id: str | None = None  # 자동 매수를 했을 때만
    buy_cost_usdt: str | None = None
    txid: str | None = None


@dataclass
class Wallet:
    id: int | None
    discord_id: int
    label: str
    coin: str
    network: str
    address: str
    memo: str | None = None
    created_at: str = ""


@dataclass
class Panel:
    message_id: int
    channel_id: int
    created_at: str = ""


@dataclass
class Event:
    id: int
    kind: str
    level: str  # info | success | warning | error
    message: str
    user_id: int | None = None
    ref_type: str | None = None  # deposit | notification | tx
    ref_id: int | None = None
    details: str | None = None  # JSON
    created_at: str = ""


class DepositorNameTakenError(Exception):
    def __init__(self, owner_id: int) -> None:
        super().__init__(f"depositor name already assigned to {owner_id}")
        self.owner_id = owner_id


class DepositorNameRemovedError(Exception):
    # 관리자가 이름을 해제했던 유저 (claim_depositor_name)
    pass


class DuplicateNotificationError(Exception):
    # 같은 입금 문자가 또 온 경우. notification은 처음 저장된 것
    def __init__(self, notification: BankNotification) -> None:
        super().__init__(f"bank notification #{notification.id} received again")
        self.notification = notification


class DepositSlotBusyError(Exception):
    # 다른 유저의 충전 요청이 열려 있는 경우. opened_at은 그 요청을 연 시각(UTC)
    def __init__(self, opened_at: str) -> None:
        super().__init__("another user's deposit request is open")
        self.opened_at = opened_at


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    discord_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    total_deposited INTEGER NOT NULL DEFAULT 0,
    total_spent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS depositor_names (
    discord_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    assigned_by INTEGER NOT NULL,
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (discord_id) REFERENCES users(discord_id)
);

CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    depositor_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    channel_id INTEGER,
    message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    FOREIGN KEY (discord_id) REFERENCES users(discord_id)
);

CREATE TABLE IF NOT EXISTS bank_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    depositor_name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    deposit_id INTEGER,
    credited_user_id INTEGER,
    resolved_by INTEGER,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    sms_key TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    coin TEXT NOT NULL,
    network TEXT NOT NULL,
    address TEXT NOT NULL,
    memo TEXT,
    coin_amount TEXT NOT NULL,
    krw_amount INTEGER NOT NULL,
    fee_krw INTEGER NOT NULL,
    exchange_rate REAL NOT NULL,
    binance_withdraw_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'pending',
    withdraw_order_id TEXT,
    updated_at TEXT,
    error TEXT,
    buy_order_id TEXT,
    buy_cost_usdt TEXT,
    txid TEXT,
    FOREIGN KEY (discord_id) REFERENCES users(discord_id)
);

CREATE TABLE IF NOT EXISTS balance_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    admin_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deposit_id INTEGER,
    notification_id INTEGER,
    covers_transfer INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    coin TEXT NOT NULL,
    network TEXT NOT NULL,
    address TEXT NOT NULL,
    memo TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (discord_id) REFERENCES users(discord_id),
    UNIQUE(discord_id, coin, network, address)
);

CREATE TABLE IF NOT EXISTS panels (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    user_id INTEGER,
    ref_type TEXT,
    ref_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 경고는 디스코드가 받아 준 뒤에야 전달된 것으로 표시한다
CREATE TABLE IF NOT EXISTS event_deliveries (
    event_id INTEGER PRIMARY KEY REFERENCES events(id),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT
);

-- 관리자가 확인한 뒤 충전하기까지 그사이 기록이 바뀌었는지 보는 번호
CREATE TABLE IF NOT EXISTS credit_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO credit_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- .env의 ALLOWED_USERS 말고도 봇을 쓸 수 있는 유저: 입금자명을 받은 사람 전부
CREATE TABLE IF NOT EXISTS allowed_users (
    discord_id INTEGER PRIMARY KEY,
    granted_by INTEGER NOT NULL,
    granted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- /관리자 이용정지로 막은 유저. 잔액은 그대로지만 위 목록이나 ALLOWED_USERS에 있어도 못 쓴다
CREATE TABLE IF NOT EXISTS blocked_users (
    discord_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    blocked_by INTEGER NOT NULL,
    blocked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 견적 시점에 그 코인의 시장 가치(원): 드는 USDT × 업비트 USDT 시세
-- 결제 금액에서 이 값을 빼면 그 판매의 이익이다 (/이익). 이 테이블 이전의 송금은 행이 없다
CREATE TABLE IF NOT EXISTS transfer_values (
    tx_id INTEGER PRIMARY KEY,
    market_krw INTEGER NOT NULL CHECK (typeof(market_krw) = 'integer' AND market_krw >= 0)
);

-- /관리자 신규한도로 정한 유저별 신규 24시간 한도. 신규인 동안에만 NEW_USER_DAILY_LIMIT_KRW 대신 쓴다
-- 1회·일·전체 한도는 다른 유저와 똑같이 걸린다
CREATE TABLE IF NOT EXISTS user_limits (
    discord_id INTEGER PRIMARY KEY,
    new_user_limit INTEGER NOT NULL
        CHECK (typeof(new_user_limit) = 'integer' AND new_user_limit > 0),
    set_by INTEGER NOT NULL,
    set_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 첫 버전 이후에 추가한 컬럼. 예전 버전은 출금에 성공한 건만 저장했으므로
# 그때 행들의 status는 'submitted'로 채운다
_TRANSACTION_COLUMNS = {
    "status": "status TEXT NOT NULL DEFAULT 'submitted'",
    "withdraw_order_id": "withdraw_order_id TEXT",
    "updated_at": "updated_at TEXT",
    "error": "error TEXT",
    "buy_order_id": "buy_order_id TEXT",
    "buy_cost_usdt": "buy_cost_usdt TEXT",
    "txid": "txid TEXT",
}
# 수동 충전이 닫은 충전 요청과, 그 충전이 대신한 입금 알림. 충전 한 건이 입금 두 건을 덮지 않게 한다
# covers_transfer가 0이면 입금과 무관한 돈(환불 등)이라 어떤 입금도 덮지 않는다
_ADJUSTMENT_COLUMNS = {
    "deposit_id": "deposit_id INTEGER",
    "notification_id": "notification_id INTEGER",
    "covers_transfer": "covers_transfer INTEGER NOT NULL DEFAULT 1",
}

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_deposits_user_status ON deposits (discord_id, status);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON bank_notifications (status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_sms_key ON bank_notifications (sms_key);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions (discord_id, created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions (status);
CREATE INDEX IF NOT EXISTS idx_transactions_created ON transactions (created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_order_id ON transactions (withdraw_order_id);
CREATE INDEX IF NOT EXISTS idx_events_user ON events (user_id, id);
CREATE INDEX IF NOT EXISTS idx_events_level ON events (level, id);
CREATE INDEX IF NOT EXISTS idx_events_ref ON events (ref_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at);
CREATE INDEX IF NOT EXISTS idx_event_delivery_due ON event_deliveries (next_attempt_at)
    WHERE delivered_at IS NULL;
"""

# 관리자가 충전 전에 확인한 내용이 그사이 낡았는지 알 수 있게, 관련 테이블이 바뀔 때마다 번호를 올린다
# 트리거라서 다른 커넥션이 쓴 것도 잡힌다
_CREDIT_TRIGGERS = "\n".join(
    f"CREATE TRIGGER IF NOT EXISTS credit_revision_{table}_{action.lower()} "
    f"AFTER {action} ON {table} BEGIN "
    "UPDATE credit_state SET revision = revision + 1 WHERE id = 1; END;"
    for table in ("users", "deposits", "bank_notifications", "balance_adjustments", "depositor_names")
    for action in ("INSERT", "UPDATE", "DELETE")
)

NOTIFICATION_STATUS = {
    "matched": "자동 충전됨",
    "unmatched": "미확인",
    "resolved": "관리자가 충전함",
    "dismissed": "제외됨",
    "reversal": "입금 취소·반환 문자",
}

# 출금 상태 전이: 목적 상태 -> 올 수 있는 상태들
# failed(환불)로는 fail_transaction()으로만 간다
_TX_TRANSITIONS = {
    "unknown": ("pending",),
    "submitted": ("pending", "unknown"),
    "completed": ("pending", "unknown", "submitted"),
}
_OPEN_TX_STATUSES = ("pending", "unknown", "submitted")

# 관리자 id 자리에 들어가는 값. 봇이 직접 건 정지나 중지를 뜻한다
SYSTEM_ACTOR = 0
# 충전·송금 중지 상태를 JSON으로 담는 settings 키
PAUSE_SETTING = "service_pause"


def _bind(params: Iterable | dict) -> tuple | dict:
    return params if isinstance(params, dict) else tuple(params)


class Database:
    """커넥션 하나를 같이 쓰므로 공개 메서드는 모두 _lock을 잡는다.

    여러 문장을 묶는 변경은 _transaction() 안에서 한 번에 커밋된다.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.executescript(_INDEXES)
        await self._db.executescript(_CREDIT_TRIGGERS)
        # allowed_users 테이블이 생기기 전에 지정된 이름도 이용 권한으로 옮긴다
        await self.db.execute(
            "INSERT OR IGNORE INTO allowed_users (discord_id, granted_by, granted_at) "
            "SELECT discord_id, assigned_by, assigned_at FROM depositor_names"
        )
        await self._db.commit()

    async def _migrate(self) -> None:
        columns = {row["name"] for row in await self._fetchall("PRAGMA table_info(transactions)")}
        for name, ddl in _TRANSACTION_COLUMNS.items():
            if name not in columns:
                await self.db.execute(f"ALTER TABLE transactions ADD COLUMN {ddl}")
        columns = {row["name"] for row in await self._fetchall("PRAGMA table_info(deposits)")}
        if "expiry_logged" not in columns:
            await self.db.execute(
                "ALTER TABLE deposits ADD COLUMN expiry_logged INTEGER NOT NULL DEFAULT 0"
            )
            # 이전에 마감된 요청은 이제 와서 알리지 않는다
            await self.db.execute("UPDATE deposits SET expiry_logged = 1 WHERE status = 'expired'")
        columns = {row["name"] for row in await self._fetchall("PRAGMA table_info(bank_notifications)")}
        if "sms_key" not in columns:
            await self.db.execute("ALTER TABLE bank_notifications ADD COLUMN sms_key TEXT")
        columns = {row["name"] for row in await self._fetchall("PRAGMA table_info(balance_adjustments)")}
        for name, ddl in _ADJUSTMENT_COLUMNS.items():
            if name not in columns:
                await self.db.execute(f"ALTER TABLE balance_adjustments ADD COLUMN {ddl}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        async with self._lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                yield
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def credit_revision(self) -> int:
        async with self._lock:
            row = await self._fetchone("SELECT revision FROM credit_state WHERE id = 1")
            return row["revision"]

    async def _check_credit_revision(self, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        row = await self._fetchone("SELECT revision FROM credit_state WHERE id = 1")
        if row["revision"] != expected_revision:
            raise UserFacingError(
                "확인하는 동안 입금이나 잔액 기록이 바뀌었어요. 중복 충전을 막기 위해 충전하지 않았어요.",
                title="충전 기록을 다시 확인해 주세요",
                tone="warning",
                hint="잔액과 입금 내역을 확인한 뒤 명령을 다시 실행해 주세요",
            )

    async def _fetchone(self, sql: str, params: Iterable | dict = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, _bind(params)) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: Iterable | dict = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, _bind(params)) as cur:
            return list(await cur.fetchall())

    async def _ensure_user(self, discord_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO users (discord_id) VALUES (?)", (discord_id,)
        )

    async def get_user(self, discord_id: int) -> User:
        async with self._lock:
            row = await self._fetchone(
                "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
            )
        return User(**dict(row)) if row else User(discord_id=discord_id)

    async def _credit_deposit(self, discord_id: int, amount: int) -> int:
        await self._ensure_user(discord_id)
        await self.db.execute(
            "UPDATE users SET balance = balance + ?, total_deposited = total_deposited + ? "
            "WHERE discord_id = ?",
            (amount, amount, discord_id),
        )
        row = await self._fetchone(
            "SELECT balance FROM users WHERE discord_id = ?", (discord_id,)
        )
        return row["balance"]

    async def get_depositor_name(self, discord_id: int) -> DepositorName | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT * FROM depositor_names WHERE discord_id = ?", (discord_id,)
            )
        return DepositorName(**dict(row)) if row else None

    async def set_depositor_name(
        self, discord_id: int, name: str, name_key: str, assigned_by: int
    ) -> bool:
        """입금자명을 지정하고 이용 권한을 준다. 권한을 이번에 새로 줬으면 True.

        다른 유저가 쓰는 이름이면 DepositorNameTakenError.
        """
        async with self._transaction():
            owner = await self._fetchone(
                "SELECT discord_id FROM depositor_names WHERE name_key = ? AND discord_id != ?",
                (name_key, discord_id),
            )
            if owner is not None:
                raise DepositorNameTakenError(owner["discord_id"])
            await self._ensure_user(discord_id)
            await self.db.execute(
                "INSERT INTO depositor_names (discord_id, name, name_key, assigned_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET name = excluded.name, "
                "name_key = excluded.name_key, assigned_by = excluded.assigned_by, "
                "assigned_at = datetime('now')",
                (discord_id, name, name_key, assigned_by),
            )
            async with self.db.execute(
                "INSERT OR IGNORE INTO allowed_users (discord_id, granted_by) VALUES (?, ?)",
                (discord_id, assigned_by),
            ) as cur:
                return cur.rowcount > 0

    async def claim_depositor_name(
        self, discord_id: int, name: str, name_key: str, assigned_by: int
    ) -> tuple[DepositorName, bool]:
        """이름이 없는 유저에게만 이름과 이용 권한을 준다. (현재 이름, 이번에 지정했는지)를 돌려준다.

        set_depositor_name과 달리 이미 있는 이름은 바꾸지 않는다.
        관리자가 이름을 해제했던 유저면 DepositorNameRemovedError, 남이 쓰는 이름이면 DepositorNameTakenError.
        """
        async with self._transaction():
            row = await self._fetchone(
                "SELECT * FROM depositor_names WHERE discord_id = ?", (discord_id,)
            )
            if row is not None:
                return DepositorName(**dict(row)), False
            if await self._fetchone(
                "SELECT 1 FROM allowed_users WHERE discord_id = ?", (discord_id,)
            ) is not None:
                raise DepositorNameRemovedError()
            owner = await self._fetchone(
                "SELECT discord_id FROM depositor_names WHERE name_key = ?", (name_key,)
            )
            if owner is not None:
                raise DepositorNameTakenError(owner["discord_id"])
            await self._ensure_user(discord_id)
            await self.db.execute(
                "INSERT INTO depositor_names (discord_id, name, name_key, assigned_by) "
                "VALUES (?, ?, ?, ?)",
                (discord_id, name, name_key, assigned_by),
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO allowed_users (discord_id, granted_by) VALUES (?, ?)",
                (discord_id, assigned_by),
            )
            row = await self._fetchone(
                "SELECT * FROM depositor_names WHERE discord_id = ?", (discord_id,)
            )
        return DepositorName(**dict(row)), True

    async def list_allowed_users(self) -> set[int]:
        async with self._lock:
            rows = await self._fetchall("SELECT discord_id FROM allowed_users")
        return {r["discord_id"] for r in rows}

    async def was_given_name(self, discord_id: int) -> bool:
        """입금자명으로 이용 권한을 받은 적이 있는지. 지금은 이름이 없을 수도 있다."""
        async with self._lock:
            row = await self._fetchone(
                "SELECT 1 FROM allowed_users WHERE discord_id = ?", (discord_id,)
            )
        return row is not None

    async def remove_depositor_name(self, discord_id: int) -> bool:
        async with self._transaction():
            async with self.db.execute(
                "DELETE FROM depositor_names WHERE discord_id = ?", (discord_id,)
            ) as cur:
                return cur.rowcount > 0

    async def list_depositor_names(self) -> list[DepositorName]:
        async with self._lock:
            rows = await self._fetchall("SELECT * FROM depositor_names ORDER BY name")
        return [DepositorName(**dict(r)) for r in rows]

    async def block_user(self, discord_id: int, reason: str, admin_id: int) -> bool:
        """이미 정지된 유저였으면 False (사유만 바뀐다)."""
        async with self._transaction():
            existing = await self._fetchone(
                "SELECT 1 FROM blocked_users WHERE discord_id = ?", (discord_id,)
            )
            await self.db.execute(
                "INSERT INTO blocked_users (discord_id, reason, blocked_by) VALUES (?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET reason = excluded.reason, "
                "blocked_by = excluded.blocked_by",
                (discord_id, reason, admin_id),
            )
        return existing is None

    async def auto_block_user(self, discord_id: int, reason: str) -> bool:
        """봇이 직접 거는 정지. 관리자가 건 정지는 사유까지 그대로 둔다. 이미 정지돼 있었으면 False."""
        async with self._transaction():
            async with self.db.execute(
                "INSERT OR IGNORE INTO blocked_users (discord_id, reason, blocked_by) VALUES (?, ?, ?)",
                (discord_id, reason, SYSTEM_ACTOR),
            ) as cur:
                return cur.rowcount > 0

    async def unblock_user(self, discord_id: int) -> bool:
        async with self._transaction():
            async with self.db.execute(
                "DELETE FROM blocked_users WHERE discord_id = ?", (discord_id,)
            ) as cur:
                return cur.rowcount > 0

    async def get_block(self, discord_id: int) -> UserBlock | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT * FROM blocked_users WHERE discord_id = ?", (discord_id,)
            )
        return UserBlock(**dict(row)) if row else None

    async def list_blocked_users(self) -> set[int]:
        async with self._lock:
            rows = await self._fetchall("SELECT discord_id FROM blocked_users")
        return {r["discord_id"] for r in rows}

    async def _expire_deposits(self, timeout_minutes: int) -> None:
        # completed_at에는 요청이 닫힌 시각을 넣는다. 자리를 얼마나 잡고 있었는지 셀 때 쓴다 (list_unpaid_holds)
        minutes = int(timeout_minutes)
        await self.db.execute(
            "UPDATE deposits SET status = 'expired', completed_at = datetime(created_at, ?) "
            "WHERE status = 'pending' AND created_at <= datetime('now', ?)",
            (f"+{minutes} minutes", f"-{minutes} minutes"),
        )

    async def get_pending_deposit(
        self, discord_id: int, timeout_minutes: int
    ) -> Deposit | None:
        async with self._transaction():
            await self._expire_deposits(timeout_minutes)
            row = await self._fetchone(
                "SELECT * FROM deposits WHERE discord_id = ? AND status = 'pending' "
                "ORDER BY id DESC LIMIT 1",
                (discord_id,),
            )
        return Deposit(**dict(row)) if row else None

    async def _other_pending_since(self, discord_id: int) -> str | None:
        row = await self._fetchone(
            "SELECT MAX(created_at) AS opened_at FROM deposits "
            "WHERE status = 'pending' AND discord_id != ?",
            (discord_id,),
        )
        return row["opened_at"]

    async def other_pending_since(self, discord_id: int, timeout_minutes: int) -> str | None:
        """다른 유저의 열린 요청 중 가장 최근 것을 연 시각(UTC). 없으면 None."""
        async with self._transaction():
            await self._expire_deposits(timeout_minutes)
            return await self._other_pending_since(discord_id)

    async def create_deposit(
        self,
        discord_id: int,
        amount: int,
        depositor_name: str,
        timeout_minutes: int,
        channel_id: int | None = None,
    ) -> Deposit:
        """충전 요청을 연다.

        요청은 전체 유저를 통틀어 한 번에 하나만 열린다. 입금 문자를 넘겨 주는 단축어가 동시에 온 문자
        둘 중 하나를 놓칠 수 있어서다. 다음 사람은 앞 요청이 닫힐 때까지(충전, 취소, 마감) 기다린다.
        다른 유저의 요청이 열려 있으면 DepositSlotBusyError, 서비스가 멈춰 있으면 ServicePausedError.
        """
        async with self._transaction():
            if await self._service_pause() is not None:
                raise ServicePausedError()
            await self._expire_deposits(timeout_minutes)
            if await self._fetchone(
                "SELECT 1 FROM deposits WHERE discord_id = ? AND status = 'pending'",
                (discord_id,),
            ):
                raise UserFacingError(
                    "먼저 진행 중인 요청을 입금하거나 취소해 주세요.",
                    title="진행 중인 충전 요청이 있어요",
                    tone="warning",
                )
            # insert와 같은 트랜잭션에서 확인해야 둘이 동시에 신청해도 한 명만 통과한다
            opened_at = await self._other_pending_since(discord_id)
            if opened_at is not None:
                raise DepositSlotBusyError(opened_at)
            await self._ensure_user(discord_id)
            async with self.db.execute(
                "INSERT INTO deposits (discord_id, amount, depositor_name, channel_id) "
                "VALUES (?, ?, ?, ?)",
                (discord_id, amount, depositor_name, channel_id),
            ) as cur:
                deposit_id = cur.lastrowid
            row = await self._fetchone("SELECT * FROM deposits WHERE id = ?", (deposit_id,))
        return Deposit(**dict(row))

    async def collect_expired_deposits(self, timeout_minutes: int) -> list[Deposit]:
        """지난 요청을 마감 처리하고, 새로 마감된 것만 한 번씩 돌려준다."""
        async with self._transaction():
            await self._expire_deposits(timeout_minutes)
            rows = await self._fetchall(
                "SELECT * FROM deposits WHERE status = 'expired' AND expiry_logged = 0 ORDER BY id"
            )
            if rows:
                ids = [r["id"] for r in rows]
                await self.db.execute(
                    f"UPDATE deposits SET expiry_logged = 1 WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
        return [Deposit(**dict(r)) for r in rows]

    async def get_last_deposit(self, discord_id: int) -> Deposit | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT * FROM deposits WHERE discord_id = ? ORDER BY id DESC LIMIT 1",
                (discord_id,),
            )
        return Deposit(**dict(row)) if row else None

    async def count_cancelled_deposits(self, discord_id: int, minutes: int) -> int:
        async with self._lock:
            row = await self._fetchone(
                "SELECT COUNT(*) AS n FROM deposits WHERE discord_id = ? AND status = 'cancelled' "
                "AND created_at >= datetime('now', ?)",
                (discord_id, f"-{int(minutes)} minutes"),
            )
        return row["n"]

    async def list_unpaid_holds(
        self, discord_id: int, minutes: int, timeout_minutes: int
    ) -> list[tuple[str, int]]:
        """최근 minutes분 동안 입금 없이 닫힌(마감·취소) 요청들: (연 시각, 자리를 잡고 있던 초), 오래된 순.

        닫힌 시각이 저장되기 전의 요청은 마감이면 전체 시간으로, 취소면 0으로 친다.
        """
        timeout_seconds = int(timeout_minutes) * 60
        async with self._transaction():
            await self._expire_deposits(timeout_minutes)
            rows = await self._fetchall(
                "SELECT created_at, CASE "
                " WHEN completed_at IS NOT NULL THEN MAX(0, MIN(:timeout, "
                "  CAST(strftime('%s', completed_at) AS INTEGER) - CAST(strftime('%s', created_at) AS INTEGER))) "
                " WHEN status = 'expired' THEN :timeout ELSE 0 END AS held "
                "FROM deposits WHERE discord_id = :user AND status IN ('expired', 'cancelled') "
                "AND created_at >= datetime('now', :since) ORDER BY created_at, id",
                {"timeout": timeout_seconds, "user": discord_id, "since": f"-{int(minutes)} minutes"},
            )
        return [(r["created_at"], r["held"]) for r in rows]

    async def get_user_deposits(self, discord_id: int, limit: int = 5) -> list[Deposit]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT * FROM deposits WHERE discord_id = ? ORDER BY id DESC LIMIT ?",
                (discord_id, limit),
            )
        return [Deposit(**dict(r)) for r in rows]

    async def cancel_deposit(self, deposit_id: int, discord_id: int) -> bool:
        async with self._transaction():
            async with self.db.execute(
                "UPDATE deposits SET status = 'cancelled', completed_at = datetime('now') "
                "WHERE id = ? AND discord_id = ? AND status = 'pending'",
                (deposit_id, discord_id),
            ) as cur:
                return cur.rowcount > 0

    async def record_incoming_transfer(
        self,
        depositor_name: str,
        name_key: str,
        amount: int,
        timeout_minutes: int,
        *,
        opened_by: str | None = None,
        sms_key: str | None = None,
        manual_since: str | None = None,
    ) -> tuple[BankNotification, Deposit | None, int | None, dict | None]:
        """입금 알림을 저장하고, 맞는 충전 요청이 있으면 완료 처리한다.

        요청이 맞으려면 그 유저에게 지정된 입금자명이고, 금액이 같고, 마감 전이어야 한다.
        opened_by(UTC)가 있으면 그 시각까지 열린 요청만 본다. 맞지 않은 알림은 관리자가 처리하도록 남긴다.

        manual_since(UTC, 돈을 보낸 시각)가 있으면 그 뒤에 같은 금액으로 한 수동 충전을 찾는다.
        문자가 늦는 사이 관리자가 먼저 넣어 준 돈일 수 있어서, 그런 충전이 있으면 자동 충전하지 않고
        알림을 그 충전에 묶어 미확인으로 남긴다. 맞는 요청은 'manual'로 닫는다.

        (알림, 맞았거나 닫힌 요청, 그 뒤 잔액, 묶인 수동 충전)을 돌려준다.
        sms_key가 이미 저장돼 있으면 DuplicateNotificationError.
        """
        async with self._transaction():
            if sms_key is not None:
                seen = await self._fetchone(
                    "SELECT id FROM bank_notifications WHERE sms_key = ?", (sms_key,)
                )
                if seen is not None:
                    raise DuplicateNotificationError(await self._get_notification(seen["id"]))
            await self._expire_deposits(timeout_minutes)
            match = await self._fetchone(
                "SELECT d.* FROM deposits d "
                "JOIN depositor_names n ON n.discord_id = d.discord_id "
                "WHERE n.name_key = ? AND d.amount = ? AND d.status = 'pending' "
                "AND (? IS NULL OR d.created_at <= ?) "
                "ORDER BY d.id LIMIT 1",
                (name_key, amount, opened_by, opened_by),
            )
            owner = await self._fetchone(
                "SELECT discord_id FROM depositor_names WHERE name_key = ?", (name_key,)
            )
            cover = None
            if owner is not None and manual_since is not None:
                credits = await self._manual_credits(
                    owner["discord_id"], amount, manual_since, 1, unclaimed=True,
                    for_deposit=match["id"] if match is not None else None,
                )
                cover = credits[0] if credits else None
            if match is not None:
                # 요청을 pending에서 빼내는 update가 성공한 쪽만 충전한다. 같은 요청이 두 번 충전되지 않게
                # 수동 충전으로 이미 받은 요청이면 충전 없이 'manual'로 닫는다
                async with self.db.execute(
                    "UPDATE deposits SET status = ?, completed_at = datetime('now') "
                    "WHERE id = ? AND status = 'pending'",
                    ("manual" if cover is not None else "completed", match["id"]),
                ) as cur:
                    if cur.rowcount == 0:
                        match = None
            credit = match is not None and cover is None
            async with self.db.execute(
                "INSERT INTO bank_notifications "
                "(depositor_name, name_key, amount, status, deposit_id, credited_user_id, sms_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    depositor_name, name_key, amount,
                    "matched" if credit else "unmatched",
                    match["id"] if match is not None else None,
                    match["discord_id"] if credit else None,
                    sms_key,
                ),
            ) as cur:
                notification_id = cur.lastrowid
            if cover is not None:
                # 이 수동 충전은 이제 이 입금 하나만 대신한다
                await self.db.execute(
                    "UPDATE balance_adjustments SET notification_id = ?, "
                    "deposit_id = COALESCE(deposit_id, ?) WHERE id = ?",
                    (notification_id, match["id"] if match is not None else None, cover["id"]),
                )
                cover = await self._fetchone("SELECT * FROM balance_adjustments WHERE id = ?", (cover["id"],))
                cover = dict(cover)

            deposit = None
            new_balance = None
            if match is not None:
                if credit:
                    new_balance = await self._credit_deposit(match["discord_id"], match["amount"])
                else:
                    row = await self._fetchone(
                        "SELECT balance FROM users WHERE discord_id = ?", (match["discord_id"],)
                    )
                    new_balance = row["balance"] if row else 0
                row = await self._fetchone("SELECT * FROM deposits WHERE id = ?", (match["id"],))
                deposit = Deposit(**dict(row))
            notification = await self._get_notification(notification_id)
        return notification, deposit, new_balance, cover

    async def _manual_credits(
        self, discord_id: int, amount: int, since: str, limit: int, *,
        unclaimed: bool = False, for_deposit: int | None = None,
    ) -> list[dict]:
        # 충전은 양수, 차감은 음수로 저장되므로 양수 금액으로 찾으면 충전만 나온다
        # unclaimed: 아직 어떤 입금에도 묶이지 않은 것만 / for_deposit: 다른 요청을 닫은 충전은 제외
        rows = await self._fetchall(
            "SELECT * FROM balance_adjustments WHERE discord_id = ? AND amount = ? AND created_at >= ? "
            "AND (? = 0 OR (notification_id IS NULL AND covers_transfer = 1)) "
            "AND (? IS NULL OR deposit_id IS NULL OR deposit_id = ?) ORDER BY id DESC LIMIT ?",
            (discord_id, amount, since, int(unclaimed), for_deposit, for_deposit, limit),
        )
        return [dict(r) for r in rows]

    async def find_manual_credits(
        self, discord_id: int, amount: int, since: str, limit: int = 5
    ) -> list[dict]:
        """since(UTC) 이후 이 유저에게 정확히 amount를 넣은 수동 충전. 최근 것부터."""
        async with self._lock:
            return await self._manual_credits(discord_id, amount, since, limit)

    async def credit_by_hand(
        self, discord_id: int, amount: int, reason: str, admin_id: int, timeout_minutes: int,
        *, other_money: bool = False, for_request: int | None = None,
        expected_revision: int | None = None,
    ) -> tuple[int, Deposit | None]:
        """수동 충전. 같은 금액의 열린 충전 요청이 있으면 같이 닫는다.

        그래야 나중에 그 요청의 입금 문자가 와도 다시 충전되지 않고 관리자 확인으로 넘어간다.
        other_money: 입금과 무관한 돈(환불 등). 요청을 닫지 않고 어떤 입금도 보류시키지 않는다.
        for_request: 관리자가 확인 카드에서 고른 요청. 그사이 이미 충전됐으면 거절한다.
        (새 잔액, 'manual'로 닫힌 요청 또는 None)을 돌려준다.
        """
        async with self._transaction():
            await self._check_credit_revision(expected_revision)
            await self._expire_deposits(timeout_minutes)
            if for_request is not None:
                row = await self._fetchone(
                    "SELECT id, status FROM deposits WHERE id = ? AND discord_id = ?",
                    (for_request, discord_id),
                )
                if row is not None and row["status"] in ("completed", "manual"):
                    raise UserFacingError(
                        f"충전 요청 #{for_request}은 확인 카드를 띄운 사이 이미 충전됐어요"
                        + (" (입금 문자로 자동 충전)." if row["status"] == "completed" else " (다른 수동 충전)."),
                        title="이미 충전된 요청이라 충전하지 않았어요",
                        tone="warning",
                        hint="잔액은 그대로예요 · 다른 돈이 맞다면 `/관리자 잔액충전`을 다시 실행하세요",
                    )
                # 마감·취소된 요청이면 닫을 게 없다. 늦게 오는 문자는 이 충전이 보류시킨다
                pending = row if row is not None and row["status"] == "pending" else None
            elif other_money:
                pending = None
            else:
                pending = await self._fetchone(
                    "SELECT id FROM deposits WHERE discord_id = ? AND amount = ? AND status = 'pending' "
                    "ORDER BY id DESC LIMIT 1",
                    (discord_id, amount),
                )
            deposit = None
            if pending is not None:
                await self.db.execute(
                    "UPDATE deposits SET status = 'manual', completed_at = datetime('now') "
                    "WHERE id = ? AND status = 'pending'",
                    (pending["id"],),
                )
                row = await self._fetchone("SELECT * FROM deposits WHERE id = ?", (pending["id"],))
                deposit = Deposit(**dict(row))
            await self.db.execute(
                "INSERT INTO balance_adjustments "
                "(discord_id, amount, reason, admin_id, deposit_id, covers_transfer) VALUES (?, ?, ?, ?, ?, ?)",
                (discord_id, amount, reason, admin_id, deposit.id if deposit else None, int(not other_money)),
            )
            balance = await self._credit_deposit(discord_id, amount)
        return balance, deposit

    async def record_reversal(
        self, depositor_name: str, name_key: str, amount: int, *, sms_key: str | None = None
    ) -> tuple[BankNotification, bool]:
        """입금 취소·반환 문자를 저장한다 (status 'reversal', 충전하지 않음).

        (저장된 알림, 이미 저장돼 있던 문자인지)를 돌려준다. 같은 문자가 재시작 뒤에 또 와도 경고는 한 번만 나간다.
        """
        async with self._transaction():
            if sms_key is not None:
                seen = await self._fetchone(
                    "SELECT id FROM bank_notifications WHERE sms_key = ?", (sms_key,)
                )
                if seen is not None:
                    return await self._get_notification(seen["id"]), True
            async with self.db.execute(
                "INSERT INTO bank_notifications (depositor_name, name_key, amount, status, sms_key) "
                "VALUES (?, ?, ?, 'reversal', ?)",
                (depositor_name, name_key, amount, sms_key),
            ) as cur:
                notification_id = cur.lastrowid
            return await self._get_notification(notification_id), False

    async def find_notifications(
        self,
        *,
        statuses: tuple[str, ...],
        amount: int,
        since: str,
        name_key: str | None = None,
        credited_user_id: int | None = None,
        exclude_id: int | None = None,
        limit: int = 5,
    ) -> list[BankNotification]:
        """since(UTC) 이후에 받았거나 처리한 amount짜리 입금 알림. 최근 것부터.

        며칠 전에 온 입금을 관리자가 방금 충전했을 수도 있어서 처리한 시각도 같이 본다.
        """
        query = (
            "SELECT b.*, n.discord_id AS owner_id FROM bank_notifications b "
            "LEFT JOIN depositor_names n ON n.name_key = b.name_key "
            f"WHERE b.status IN ({','.join('?' * len(statuses))}) AND b.amount = ? "
            "AND COALESCE(b.resolved_at, b.received_at) >= ?"
        )
        params: list = [*statuses, amount, since]
        if name_key is not None:
            query += " AND b.name_key = ?"
            params.append(name_key)
        if credited_user_id is not None:
            query += " AND b.credited_user_id = ?"
            params.append(credited_user_id)
        if exclude_id is not None:
            query += " AND b.id != ?"
            params.append(exclude_id)
        query += " ORDER BY b.id DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            rows = await self._fetchall(query, params)
        return [BankNotification(**dict(r)) for r in rows]

    async def find_unmatched_for_credit(
        self, discord_id: int, amount: int, unowned_since: str, limit: int = 5
    ) -> list[BankNotification]:
        """이 유저 이름으로 온 미확인 입금과, unowned_since 이후 주인 없는 이름으로 온 미확인 입금 (같은 금액만)."""
        async with self._lock:
            rows = await self._fetchall(
                "SELECT b.*, n.discord_id AS owner_id FROM bank_notifications b "
                "LEFT JOIN depositor_names n ON n.name_key = b.name_key "
                "WHERE b.status = 'unmatched' AND b.amount = ? "
                "AND (n.discord_id = ? OR (n.discord_id IS NULL AND b.received_at >= ?)) "
                "ORDER BY b.id DESC LIMIT ?",
                (amount, discord_id, unowned_since, limit),
            )
        return [BankNotification(**dict(r)) for r in rows]

    async def _get_notification(self, notification_id: int) -> BankNotification:
        row = await self._fetchone(
            "SELECT * FROM bank_notifications WHERE id = ?", (notification_id,)
        )
        return BankNotification(**dict(row))

    async def get_notification(self, notification_id: int) -> BankNotification | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT b.*, n.discord_id AS owner_id FROM bank_notifications b "
                "LEFT JOIN depositor_names n ON n.name_key = b.name_key WHERE b.id = ?",
                (notification_id,),
            )
        return BankNotification(**dict(row)) if row else None

    async def get_depositor_owner(self, name_key: str) -> int | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT discord_id FROM depositor_names WHERE name_key = ?", (name_key,)
            )
        return row["discord_id"] if row else None

    async def list_unmatched_notifications(self, limit: int = 15) -> list[BankNotification]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT b.*, n.discord_id AS owner_id FROM bank_notifications b "
                "LEFT JOIN depositor_names n ON n.name_key = b.name_key "
                "WHERE b.status = 'unmatched' ORDER BY b.id DESC LIMIT ?",
                (limit,),
            )
        return [BankNotification(**dict(r)) for r in rows]

    async def _close_notification(
        self,
        notification_id: int,
        status: str,
        admin_id: int,
        credited_user_id: int | None = None,
    ) -> BankNotification:
        async with self.db.execute(
            "UPDATE bank_notifications SET status = ?, credited_user_id = ?, "
            "resolved_by = ?, resolved_at = datetime('now') "
            "WHERE id = ? AND status = 'unmatched'",
            (status, credited_user_id, admin_id, notification_id),
        ) as cur:
            changed = cur.rowcount > 0
        if not changed:
            row = await self._fetchone(
                "SELECT status FROM bank_notifications WHERE id = ?", (notification_id,)
            )
            if row is None:
                raise UserFacingError(
                    f"#{notification_id} 입금 알림이 없어요. 번호를 다시 확인해 주세요.",
                    title="입금 알림을 찾을 수 없어요",
                    tone="warning",
                )
            raise UserFacingError(
                "다른 관리자가 먼저 처리했을 수 있어요.",
                title="이미 처리된 입금 알림이에요",
                tone="info",
                facts=[
                    ("입금 알림", f"#{notification_id}"),
                    ("상태", NOTIFICATION_STATUS.get(row["status"], row["status"])),
                ],
            )
        return await self._get_notification(notification_id)

    async def resolve_notification(
        self, notification_id: int, discord_id: int, admin_id: int,
        *, expected_revision: int | None = None,
    ) -> tuple[BankNotification, int]:
        async with self._transaction():
            current = await self._fetchone(
                "SELECT status FROM bank_notifications WHERE id = ?", (notification_id,)
            )
            if current is None or current["status"] != "unmatched":
                # 없거나 이미 처리된 알림이면 여기서 그 안내가 나간다. 쓰기는 일어나지 않는다
                await self._close_notification(notification_id, "resolved", admin_id)
            await self._check_credit_revision(expected_revision)
            notification = await self._close_notification(
                notification_id, "resolved", admin_id, credited_user_id=discord_id
            )
            new_balance = await self._credit_deposit(discord_id, notification.amount)
        return notification, new_balance

    async def debit_balance(
        self, discord_id: int, amount: int, reason: str, admin_id: int
    ) -> int:
        """수동 차감. 충전을 되돌리는 것이라 total_deposited도 같이 줄인다. 잔액보다 많이는 못 뺀다."""
        async with self._transaction():
            async with self.db.execute(
                "UPDATE users SET balance = balance - ?, total_deposited = total_deposited - ? "
                "WHERE discord_id = ? AND balance >= ?",
                (amount, amount, discord_id, amount),
            ) as cur:
                taken = cur.rowcount > 0
            row = await self._fetchone(
                "SELECT balance FROM users WHERE discord_id = ?", (discord_id,)
            )
            balance = row["balance"] if row else 0
            if not taken:
                raise UserFacingError(
                    "잔액보다 많이 뺄 수는 없어요. 금액을 다시 확인해 주세요.",
                    title="잔액이 모자라요",
                    tone="warning",
                    facts=[("빼려는 금액", f"{amount:,}원"), ("현재 잔액", f"**{balance:,}원**")],
                )
            await self.db.execute(
                "INSERT INTO balance_adjustments (discord_id, amount, reason, admin_id) "
                "VALUES (?, ?, ?, ?)",
                (discord_id, -amount, reason, admin_id),
            )
        return balance

    async def dismiss_notification(
        self, notification_id: int, admin_id: int
    ) -> BankNotification:
        async with self._transaction():
            return await self._close_notification(notification_id, "dismissed", admin_id)

    async def _spent_last_24h(self, discord_id: int) -> int:
        row = await self._fetchone(
            "SELECT COALESCE(SUM(krw_amount), 0) AS spent FROM transactions "
            "WHERE discord_id = ? AND status != 'failed' "
            "AND created_at >= datetime('now', '-1 day')",
            (discord_id,),
        )
        return row["spent"]

    async def _transfer_record(self, discord_id: int) -> tuple[str | None, int]:
        """(실패하지 않은 첫 송금 시각, 그런 송금의 합계)"""
        row = await self._fetchone(
            "SELECT MIN(created_at) AS first, COALESCE(SUM(krw_amount), 0) AS total "
            "FROM transactions WHERE discord_id = ? AND status != 'failed'",
            (discord_id,),
        )
        return row["first"], row["total"]

    async def _is_new_user(self, discord_id: int, days: int, min_total: int = 0) -> bool:
        """송금이 없거나, 첫 송금이 days일이 안 됐거나, 누적이 min_total 미만이면 신규."""
        first, total = await self._transfer_record(discord_id)
        if first is None or total < min_total:
            return True
        row = await self._fetchone(
            "SELECT ? > datetime('now', ?) AS new", (first, f"-{int(days)} days")
        )
        return bool(row["new"])

    async def _own_new_user_limit(self, discord_id: int) -> UserLimit | None:
        row = await self._fetchone(
            "SELECT * FROM user_limits WHERE discord_id = ?", (discord_id,)
        )
        return UserLimit(**dict(row)) if row else None

    async def _new_user_cap(
        self, discord_id: int, new_user_limit: int, new_user_days: int, daily_limit: int,
        new_user_min_total: int = 0,
    ) -> int:
        """이 유저에게 지금 걸리는 신규 한도(원). 없으면 0.

        관리자가 따로 정해 준 한도가 있으면 그걸 쓴다. 한도가 일반 한도(daily_limit) 이상이면
        신규 한도는 없는 셈이다. 한도 검사와 관리자 화면이 모두 이 함수만 본다.
        """
        if not (new_user_limit and new_user_days):
            return 0
        own = await self._own_new_user_limit(discord_id)
        if own is not None:
            new_user_limit = own.new_user_limit
        if daily_limit and new_user_limit >= daily_limit:
            return 0
        if not await self._is_new_user(discord_id, new_user_days, new_user_min_total):
            return 0
        return new_user_limit

    async def new_user_status(
        self, discord_id: int, new_user_limit: int, new_user_days: int, daily_limit: int,
        new_user_min_total: int = 0,
    ) -> NewUserStatus:
        async with self._lock:
            first, total = await self._transfer_record(discord_id)
            return NewUserStatus(
                cap=await self._new_user_cap(
                    discord_id, new_user_limit, new_user_days, daily_limit, new_user_min_total
                ),
                is_new=await self._is_new_user(discord_id, new_user_days, new_user_min_total),
                first_transfer_at=first,
                total_krw=total,
                spent_24h=await self._spent_last_24h(discord_id),
                own_limit=await self._own_new_user_limit(discord_id),
            )

    async def set_new_user_limit(self, discord_id: int, limit: int, admin_id: int) -> int | None:
        """신규 기간 동안 쓸 전용 한도를 정한다. 이전 전용 한도를 돌려준다 (없었으면 None)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"not a limit: {limit!r}")
        async with self._transaction():
            old = await self._own_new_user_limit(discord_id)
            await self.db.execute(
                "INSERT INTO user_limits (discord_id, new_user_limit, set_by) VALUES (?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET new_user_limit = excluded.new_user_limit, "
                "set_by = excluded.set_by, set_at = datetime('now')",
                (discord_id, limit, admin_id),
            )
        return old.new_user_limit if old else None

    async def clear_new_user_limit(self, discord_id: int) -> int | None:
        """전용 한도를 지운다. 지운 값을 돌려준다 (없었으면 None)."""
        async with self._transaction():
            old = await self._own_new_user_limit(discord_id)
            await self.db.execute("DELETE FROM user_limits WHERE discord_id = ?", (discord_id,))
        return old.new_user_limit if old else None

    async def _count_rejected(self, discord_id: int, minutes: int) -> int:
        """최근 minutes분 동안 유저 입력 탓으로 바이낸스가 거절한 출금 수.

        TransferService._reject가 그런 건만 'rejected (코드): …'로 저장한다.
        """
        # 거절된 시각은 updated_at이다. 송금이 줄을 서 있었을 수 있어 created_at으로 세면 안 된다
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM transactions WHERE discord_id = ? AND status = 'failed' "
            "AND error LIKE 'rejected (%' AND updated_at >= datetime('now', ?)",
            (discord_id, f"-{int(minutes)} minutes"),
        )
        return row["n"]

    async def count_rejected_transfers(self, discord_id: int, minutes: int) -> int:
        async with self._lock:
            return await self._count_rejected(discord_id, minutes)

    async def _check_transfer_limits(
        self, discord_id: int, krw_amount: int, max_per_transfer: int, daily_limit: int,
        new_user_limit: int = 0, new_user_days: int = 0, global_limit: int = 0,
        max_rejected: int = 0, rejected_window: int = 0, new_user_min_total: int = 0,
    ) -> None:
        # 멈춘 동안에는 견적 단계에서부터 막는다
        if await self._service_pause() is not None:
            raise ServicePausedError()
        if max_rejected and rejected_window:
            rejected = await self._count_rejected(discord_id, rejected_window)
            if rejected >= max_rejected:
                raise RejectedLimitError(rejected, max_rejected, rejected_window)
        if max_per_transfer and krw_amount > max_per_transfer:
            raise UserFacingError(
                "금액을 줄여서 다시 입력해 주세요.",
                title="1회 송금 한도를 넘었어요",
                tone="warning",
                facts=[("입력한 금액", f"{krw_amount:,}원"), ("1회 최대", f"{max_per_transfer:,}원")],
            )
        new_user_cap = await self._new_user_cap(
            discord_id, new_user_limit, new_user_days, daily_limit, new_user_min_total
        )
        if new_user_cap:
            spent = await self._spent_last_24h(discord_id)
            if spent + krw_amount > new_user_cap:
                raise UserFacingError(
                    "처음 이용하는 분은 안전을 위해 한도가 낮아요. 남은 한도만큼만 보낼 수 있어요.",
                    title="신규 이용자 24시간 한도를 넘었어요",
                    tone="warning",
                    facts=[
                        ("입력한 금액", f"{krw_amount:,}원"),
                        ("신규 이용자 한도", f"{new_user_cap:,}원"),
                        ("남은 한도", f"**{max(new_user_cap - spent, 0):,}원**"),
                    ],
                    hint=f"첫 송금 후 {new_user_days}일이 지나"
                    + (f"고 누적 {new_user_min_total:,}원 이상 보내" if new_user_min_total else "")
                    + "면 24시간 한도가 "
                    + (f"{daily_limit:,}원으로 늘어나요" if daily_limit else "없어져요"),
                )
        if daily_limit:
            spent = await self._spent_last_24h(discord_id)
            if spent + krw_amount > daily_limit:
                raise UserFacingError(
                    "남은 한도만큼만 보낼 수 있어요.",
                    title="24시간 송금 한도를 넘었어요",
                    tone="warning",
                    facts=[
                        ("입력한 금액", f"{krw_amount:,}원"),
                        ("24시간 한도", f"{daily_limit:,}원"),
                        ("남은 한도", f"**{max(daily_limit - spent, 0):,}원**"),
                    ],
                    hint="한도는 최근 24시간 동안 보낸 금액으로 계산해요",
                )
        if global_limit:
            row = await self._fetchone(
                "SELECT COALESCE(SUM(krw_amount), 0) AS spent FROM transactions "
                "WHERE status != 'failed' AND created_at >= datetime('now', '-1 day')"
            )
            if row["spent"] + krw_amount > global_limit:
                raise GlobalLimitError(row["spent"], global_limit)

    async def check_transfer_limits(
        self, discord_id: int, krw_amount: int, max_per_transfer: int, daily_limit: int,
        new_user_limit: int = 0, new_user_days: int = 0, global_limit: int = 0,
        max_rejected: int = 0, rejected_window: int = 0, new_user_min_total: int = 0,
    ) -> None:
        async with self._lock:
            await self._check_transfer_limits(
                discord_id, krw_amount, max_per_transfer, daily_limit,
                new_user_limit, new_user_days, global_limit, max_rejected, rejected_window,
                new_user_min_total,
            )

    async def create_pending_transaction(
        self,
        *,
        discord_id: int,
        coin: str,
        network: str,
        address: str,
        memo: str | None,
        coin_amount: str,
        krw_amount: int,
        fee_krw: int,
        exchange_rate: float,
        withdraw_order_id: str,
        max_per_transfer: int,
        daily_limit: int,
        new_user_limit: int = 0,
        new_user_days: int = 0,
        global_limit: int = 0,
        max_rejected: int = 0,
        rejected_window: int = 0,
        new_user_min_total: int = 0,
        market_krw: int | None = None,
    ) -> Transaction:
        """한도 확인, 잔액 차감, 출금 기록을 한 트랜잭션으로 처리한다."""
        async with self._transaction():
            await self._check_transfer_limits(
                discord_id, krw_amount, max_per_transfer, daily_limit,
                new_user_limit, new_user_days, global_limit, max_rejected, rejected_window,
                new_user_min_total,
            )
            async with self.db.execute(
                "UPDATE users SET balance = balance - ?, total_spent = total_spent + ? "
                "WHERE discord_id = ? AND balance >= ?",
                (krw_amount, krw_amount, discord_id, krw_amount),
            ) as cur:
                deducted = cur.rowcount > 0
            if not deducted:
                row = await self._fetchone(
                    "SELECT balance FROM users WHERE discord_id = ?", (discord_id,)
                )
                balance = row["balance"] if row else 0
                raise UserFacingError(
                    "보낼 금액이 잔액보다 많아요. 금액을 줄이거나 먼저 충전해 주세요.",
                    title="잔액이 부족해요",
                    tone="warning",
                    facts=[("보낼 금액", f"{krw_amount:,}원"), ("현재 잔액", f"**{balance:,}원**")],
                )
            async with self.db.execute(
                "INSERT INTO transactions "
                "(discord_id, coin, network, address, memo, coin_amount, krw_amount, "
                "fee_krw, exchange_rate, status, withdraw_order_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, datetime('now'))",
                (
                    discord_id, coin, network, address, memo, coin_amount,
                    krw_amount, fee_krw, exchange_rate, withdraw_order_id,
                ),
            ) as cur:
                tx_id = cur.lastrowid
            if market_krw is not None:
                await self.db.execute(
                    "INSERT INTO transfer_values (tx_id, market_krw) VALUES (?, ?)",
                    (tx_id, market_krw),
                )
            row = await self._fetchone("SELECT * FROM transactions WHERE id = ?", (tx_id,))
        return Transaction(**dict(row))

    async def profit_report(
        self, admins: Iterable[int], periods: dict[str, str | None],
    ) -> ProfitReport:
        """완료된 고객 송금의 기간별 이익.

        periods는 {이름: 시작 시각(UTC) 또는 None(전체)}. 관리자 본인 송금과 아직 끝나지 않은 송금은
        이익에 넣지 않고 따로 센다.
        """
        admins = sorted(set(admins))
        theirs = f"t.discord_id IN ({','.join('?' * len(admins))})" if admins else "0"
        report = ProfitReport(periods={})
        async with self._lock:
            for label, since in periods.items():
                row = await self._fetchone(
                    "SELECT COUNT(*) AS transfers, COALESCE(SUM(t.krw_amount), 0) AS krw, "
                    "COALESCE(SUM(t.fee_krw), 0) AS fees, "
                    "COALESCE(SUM(CASE WHEN v.tx_id IS NULL THEN t.fee_krw "
                    "ELSE t.krw_amount - v.market_krw END), 0) AS profit, "
                    "COUNT(v.tx_id) AS measured "
                    "FROM transactions t LEFT JOIN transfer_values v ON v.tx_id = t.id "
                    f"WHERE t.status = 'completed' AND NOT ({theirs}) "
                    "AND (? IS NULL OR t.created_at >= ?)",
                    (*admins, since, since),
                )
                report.periods[label] = ProfitTotals(**dict(row))
            row = await self._fetchone(
                "SELECT COUNT(*) AS n, COALESCE(SUM(t.krw_amount), 0) AS krw FROM transactions t "
                f"WHERE t.status = 'completed' AND ({theirs})",
                admins,
            )
            report.admin_transfers, report.admin_krw = row["n"], row["krw"]
            row = await self._fetchone(
                "SELECT COUNT(*) AS n, COALESCE(SUM(t.krw_amount), 0) AS krw FROM transactions t "
                f"WHERE t.status IN ({','.join('?' * len(_OPEN_TX_STATUSES))}) AND NOT ({theirs})",
                (*_OPEN_TX_STATUSES, *admins),
            )
            report.open_transfers, report.open_krw = row["n"], row["krw"]
        return report

    async def set_transaction_status(
        self,
        tx_id: int,
        status: str,
        *,
        binance_withdraw_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        """_TX_TRANSITIONS에 있는 전이만 적용한다. 안 되면 False."""
        sources = _TX_TRANSITIONS[status]
        placeholders = ",".join("?" * len(sources))
        async with self._transaction():
            async with self.db.execute(
                "UPDATE transactions SET status = ?, "
                "binance_withdraw_id = COALESCE(?, binance_withdraw_id), "
                "error = COALESCE(?, error), updated_at = datetime('now') "
                f"WHERE id = ? AND status IN ({placeholders})",
                (status, binance_withdraw_id, error, tx_id, *sources),
            ) as cur:
                return cur.rowcount > 0

    async def get_transaction(self, tx_id: int) -> Transaction | None:
        async with self._lock:
            row = await self._fetchone("SELECT * FROM transactions WHERE id = ?", (tx_id,))
        return Transaction(**dict(row)) if row else None

    async def set_transaction_txid(self, tx_id: int, txid: str) -> None:
        async with self._transaction():
            await self.db.execute(
                "UPDATE transactions SET txid = ?, updated_at = datetime('now') "
                "WHERE id = ? AND (txid IS NULL OR txid != ?)",
                (txid, tx_id, txid),
            )

    async def set_transaction_buy(self, tx_id: int, order_id: str, cost_usdt: str) -> None:
        async with self._transaction():
            await self.db.execute(
                "UPDATE transactions SET buy_order_id = ?, buy_cost_usdt = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (order_id, cost_usdt, tx_id),
            )

    async def fail_transaction(
        self, tx_id: int, error: str, *, from_statuses: tuple[str, ...] = _OPEN_TX_STATUSES
    ) -> Transaction | None:
        """출금을 실패 처리하고 환불한다. from_statuses 상태가 아니면(이미 끝났거나 그사이 진행됨) None."""
        placeholders = ",".join("?" * len(from_statuses))
        async with self._transaction():
            async with self.db.execute(
                "UPDATE transactions SET status = 'failed', error = ?, "
                "updated_at = datetime('now') "
                f"WHERE id = ? AND status IN ({placeholders})",
                (error, tx_id, *from_statuses),
            ) as cur:
                if cur.rowcount == 0:
                    return None
            row = await self._fetchone("SELECT * FROM transactions WHERE id = ?", (tx_id,))
            await self.db.execute(
                "UPDATE users SET balance = balance + ?, total_spent = total_spent - ? "
                "WHERE discord_id = ?",
                (row["krw_amount"], row["krw_amount"], row["discord_id"]),
            )
        return Transaction(**dict(row))

    async def get_open_transactions(
        self, pending_grace_seconds: int, submitted_track_days: int
    ) -> list[Transaction]:
        """바이낸스와 대조해야 하는 출금들. 방금 만든 pending은 아직 보내는 중이라 뺀다."""
        async with self._lock:
            rows = await self._fetchall(
                "SELECT * FROM transactions WHERE status = 'unknown' "
                "OR (status = 'pending' AND created_at < datetime('now', ?)) "
                "OR status = 'submitted' "
                "ORDER BY id",
                (f"-{int(pending_grace_seconds)} seconds",),
            )
        return [Transaction(**dict(r)) for r in rows]

    async def withdrawal_gate(self, tx_id: int, discord_id: int) -> tuple[str | None, bool, bool]:
        """출금 직전에 확인할 값(송금 상태, 서비스 중지, 이용 정지)을 한 번의 조회로 가져온다."""
        async with self._lock:
            row = await self._fetchone(
                "SELECT (SELECT status FROM transactions WHERE id = ? AND discord_id = ?) AS status, "
                "EXISTS(SELECT 1 FROM settings WHERE key = ?) AS paused, "
                "EXISTS(SELECT 1 FROM blocked_users WHERE discord_id = ?) AS blocked",
                (tx_id, discord_id, PAUSE_SETTING, discord_id),
            )
        return row["status"], bool(row["paused"]), bool(row["blocked"])

    async def get_user_transactions(
        self, discord_id: int, limit: int = 10
    ) -> list[Transaction]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT * FROM transactions WHERE discord_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (discord_id, limit),
            )
        return [Transaction(**dict(r)) for r in rows]

    async def get_user_stats(self, discord_id: int) -> dict:
        async with self._lock:
            user_row = await self._fetchone(
                "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
            )
            row = await self._fetchone(
                "SELECT COUNT(*) AS cnt FROM transactions "
                "WHERE discord_id = ? AND status != 'failed'",
                (discord_id,),
            )
        user = User(**dict(user_row)) if user_row else User(discord_id=discord_id)
        return {
            "balance": user.balance,
            "total_deposited": user.total_deposited,
            "total_spent": user.total_spent,
            "transaction_count": row["cnt"],
        }

    async def list_buyers(self) -> list[int]:
        """출금이 완료된 적이 있는 유저들. 접수만 된 건 아직 실패할 수 있어 세지 않는다."""
        async with self._lock:
            rows = await self._fetchall(
                "SELECT DISTINCT discord_id FROM transactions "
                "WHERE status = 'completed' ORDER BY discord_id"
            )
        return [r["discord_id"] for r in rows]

    async def get_user_adjustments(self, discord_id: int, limit: int = 5) -> list[dict]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT * FROM balance_adjustments WHERE discord_id = ? ORDER BY id DESC LIMIT ?",
                (discord_id, limit),
            )
        return [dict(r) for r in rows]

    async def ops_counts(self, deposit_timeout_minutes: int) -> dict[str, int]:
        async with self._transaction():
            await self._expire_deposits(deposit_timeout_minutes)
            row = await self._fetchone(
                "SELECT "
                "(SELECT COUNT(*) FROM deposits WHERE status = 'pending') AS pending_deposits, "
                "(SELECT COUNT(*) FROM bank_notifications WHERE status = 'unmatched') AS unmatched, "
                "(SELECT COUNT(*) FROM transactions WHERE status IN ('pending', 'submitted')) AS open_transfers, "
                "(SELECT COUNT(*) FROM transactions WHERE status = 'unknown') AS unknown_transfers"
            )
        return dict(row)

    async def activity_summary(self, hours: int = 24) -> dict[str, int]:
        since = {"s": f"-{int(hours)} hours"}
        async with self._lock:
            row = await self._fetchone(
                "SELECT "
                "(SELECT COUNT(*) FROM deposits WHERE status = 'completed' "
                " AND completed_at >= datetime('now', :s)) AS deposits, "
                "(SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'completed' "
                " AND completed_at >= datetime('now', :s)) AS deposit_krw, "
                "(SELECT COUNT(*) FROM bank_notifications WHERE status = 'resolved' "
                " AND resolved_at >= datetime('now', :s)) AS resolved, "
                "(SELECT COALESCE(SUM(amount), 0) FROM bank_notifications WHERE status = 'resolved' "
                " AND resolved_at >= datetime('now', :s)) AS resolved_krw, "
                "(SELECT COALESCE(SUM(amount), 0) FROM balance_adjustments "
                " WHERE amount > 0 AND created_at >= datetime('now', :s)) AS manual_krw, "
                "(SELECT COALESCE(-SUM(amount), 0) FROM balance_adjustments "
                " WHERE amount < 0 AND created_at >= datetime('now', :s)) AS debit_krw, "
                "(SELECT COUNT(*) FROM transactions WHERE status != 'failed' "
                " AND created_at >= datetime('now', :s)) AS transfers, "
                "(SELECT COALESCE(SUM(krw_amount), 0) FROM transactions WHERE status != 'failed' "
                " AND created_at >= datetime('now', :s)) AS transfer_krw, "
                "(SELECT COALESCE(SUM(fee_krw), 0) FROM transactions WHERE status != 'failed' "
                " AND created_at >= datetime('now', :s)) AS fee_krw, "
                "(SELECT COUNT(*) FROM transactions WHERE status = 'failed' "
                " AND created_at >= datetime('now', :s)) AS failed_transfers, "
                "(SELECT COUNT(*) FROM events WHERE level = 'error' "
                " AND created_at >= datetime('now', :s)) AS errors, "
                "(SELECT COUNT(*) FROM events WHERE level = 'warning' "
                " AND created_at >= datetime('now', :s)) AS warnings, "
                "(SELECT COALESCE(SUM(balance), 0) FROM users) AS user_balances",
                since,
            )
        return dict(row)

    async def add_event(
        self,
        kind: str,
        level: str,
        message: str,
        *,
        user_id: int | None = None,
        ref_type: str | None = None,
        ref_id: int | None = None,
        details: str | None = None,
    ) -> Event:
        async with self._transaction():
            async with self.db.execute(
                "INSERT INTO events (kind, level, message, user_id, ref_type, ref_id, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, level, message, user_id, ref_type, ref_id, details),
            ) as cur:
                event_id = cur.lastrowid
            if level in ("warning", "error"):
                await self.db.execute(
                    "INSERT INTO event_deliveries (event_id) VALUES (?)", (event_id,)
                )
            row = await self._fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
        return Event(**dict(row))

    async def ensure_event_delivery(self, event_id: int) -> None:
        # 전송 기록 없이 로그만 남아 있던 경고도 재전송 대상에 넣는다
        async with self._transaction():
            await self.db.execute(
                "INSERT OR IGNORE INTO event_deliveries (event_id) "
                "SELECT id FROM events WHERE id = ? AND level IN ('warning', 'error')", (event_id,)
            )

    async def pending_event_deliveries(self, limit: int = 100) -> list[Event]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT e.* FROM event_deliveries d JOIN events e ON e.id = d.event_id "
                "WHERE d.delivered_at IS NULL AND d.next_attempt_at <= datetime('now') "
                "ORDER BY d.next_attempt_at, d.event_id LIMIT ?", (limit,)
            )
        return [Event(**dict(row)) for row in rows]

    async def finish_event_delivery(self, event_id: int, *, delivered: bool) -> None:
        async with self._transaction():
            if delivered:
                await self.db.execute(
                    "UPDATE event_deliveries SET delivered_at = datetime('now') WHERE event_id = ?",
                    (event_id,),
                )
            else:
                await self.db.execute(
                    "UPDATE event_deliveries SET attempts = attempts + 1, "
                    "next_attempt_at = datetime('now', '+' || MIN(300, 2 * (1 << MIN(attempts, 8))) "
                    "|| ' seconds') WHERE event_id = ? AND delivered_at IS NULL", (event_id,),
                )

    async def get_event(self, event_id: int) -> Event | None:
        async with self._lock:
            row = await self._fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
        return Event(**dict(row)) if row else None

    async def find_event(self, kind: str, ref_type: str, ref_id: int) -> Event | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT * FROM events WHERE kind = ? AND ref_type = ? AND ref_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (kind, ref_type, ref_id),
            )
        return Event(**dict(row)) if row else None

    async def list_events(
        self,
        *,
        levels: tuple[str, ...] | None = None,
        kind_prefix: str | None = None,
        user_id: int | None = None,
        ref: tuple[str, int] | None = None,
        before_id: int | None = None,
        limit: int = 10,
    ) -> list[Event]:
        """최근 것부터. 다음 쪽은 마지막 id를 before_id로 넘긴다."""
        query = "SELECT * FROM events WHERE 1 = 1"
        params: list = []
        if levels:
            query += f" AND level IN ({','.join('?' * len(levels))})"
            params += levels
        if kind_prefix:
            # 접두어 속의 _ 와 % 는 와일드카드가 아니라 글자 그대로 찾는다
            query += " AND kind LIKE ? ESCAPE '\\'"
            escaped = kind_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(escaped + "%")
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if ref is not None:
            query += " AND ref_type = ? AND ref_id = ?"
            params += ref
        if before_id is not None:
            query += " AND id < ?"
            params.append(before_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            rows = await self._fetchall(query, params)
        return [Event(**dict(r)) for r in rows]

    async def prune_events(self, keep_days: int) -> int:
        """keep_days보다 오래된 일반 기록을 지운다. 경고와 오류는 남긴다."""
        async with self._transaction():
            async with self.db.execute(
                "DELETE FROM events WHERE level IN ('info', 'success') "
                "AND created_at < datetime('now', ?)",
                (f"-{int(keep_days)} days",),
            ) as cur:
                return cur.rowcount

    async def get_settings(self) -> dict[str, str]:
        async with self._lock:
            rows = await self._fetchall("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    async def set_setting(self, key: str, value: str | None) -> None:
        async with self._transaction():
            if value is None:
                await self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                await self.db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    async def _service_pause(self) -> dict | None:
        row = await self._fetchone("SELECT value FROM settings WHERE key = ?", (PAUSE_SETTING,))
        if row is None:
            return None
        try:
            pause = json.loads(row["value"])
        except ValueError:
            pause = None
        # 값이 깨져 있어도 멈춘 것으로 본다. 깨진 값 때문에 서비스가 다시 열리면 안 된다
        return pause if isinstance(pause, dict) else {"reason": str(row["value"])}

    async def get_service_pause(self) -> dict | None:
        """멈춘 상태면 {reason, by, since}, 아니면 None."""
        async with self._lock:
            return await self._service_pause()

    async def pause_service(self, reason: str, by: int) -> bool:
        """resume_service까지 새 충전 요청과 송금을 막는다. 재시작해도 유지된다. 이미 멈춰 있었으면 False."""
        since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        value = json.dumps({"reason": reason, "by": by, "since": since}, ensure_ascii=False)
        async with self._transaction():
            if await self._service_pause() is not None:
                return False
            await self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)", (PAUSE_SETTING, value)
            )
        return True

    async def resume_service(self) -> dict | None:
        """중지를 푼다. 풀린 중지 정보를 돌려준다 (멈춰 있지 않았으면 None)."""
        async with self._transaction():
            pause = await self._service_pause()
            if pause is not None:
                await self.db.execute("DELETE FROM settings WHERE key = ?", (PAUSE_SETTING,))
        return pause

    async def save_wallet(
        self,
        discord_id: int,
        label: str,
        coin: str,
        network: str,
        address: str,
        memo: str | None = None,
    ) -> Wallet:
        async with self._transaction():
            await self._ensure_user(discord_id)
            async with self.db.execute(
                "INSERT OR REPLACE INTO wallets (discord_id, label, coin, network, address, memo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (discord_id, label, coin, network, address, memo),
            ) as cur:
                wallet_id = cur.lastrowid
        return Wallet(
            id=wallet_id,
            discord_id=discord_id,
            label=label,
            coin=coin,
            network=network,
            address=address,
            memo=memo,
        )

    async def get_wallets(
        self, discord_id: int, coin: str | None = None, network: str | None = None
    ) -> list[Wallet]:
        query = "SELECT * FROM wallets WHERE discord_id = ?"
        params: list = [discord_id]
        if coin:
            query += " AND coin = ?"
            params.append(coin)
        if network:
            query += " AND network = ?"
            params.append(network)
        query += " ORDER BY created_at DESC, id DESC"
        async with self._lock:
            rows = await self._fetchall(query, params)
        return [Wallet(**dict(r)) for r in rows]

    async def delete_wallet(self, wallet_id: int, discord_id: int) -> bool:
        async with self._transaction():
            async with self.db.execute(
                "DELETE FROM wallets WHERE id = ? AND discord_id = ?",
                (wallet_id, discord_id),
            ) as cur:
                return cur.rowcount > 0

    async def add_panel(self, channel_id: int, message_id: int) -> None:
        async with self._transaction():
            await self.db.execute(
                "INSERT OR IGNORE INTO panels (message_id, channel_id) VALUES (?, ?)",
                (message_id, channel_id),
            )

    async def list_panels(self) -> list[Panel]:
        async with self._lock:
            rows = await self._fetchall("SELECT * FROM panels ORDER BY created_at")
        return [Panel(**dict(r)) for r in rows]

    async def remove_panel(self, message_id: int) -> None:
        async with self._transaction():
            await self.db.execute("DELETE FROM panels WHERE message_id = ?", (message_id,))
