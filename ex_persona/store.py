"""平台数据层：SQLite 连接与用户/会话/人格/模型配置/桥接的读写。

所有多租户数据以 ``user_id`` 为隔离边界；跨用户查询仅用于管理员场景。
"""

import json
import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_lock = threading.RLock()
_initialized = False

log = logging.getLogger("ex_persona.store")

# 到期清零流水的固定标识，供统计与前端区分展示。
EXPIRE_REASON = "积分到期清零"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password_hash TEXT,
    password_salt TEXT,
    wechat_openid TEXT UNIQUE,
    wechat_unionid TEXT,
    nickname TEXT,
    avatar TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    target_name TEXT,
    dir TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'empty',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_configs (
    user_id INTEGER PRIMARY KEY,
    api_key_encrypted TEXT,
    base_url TEXT,
    model TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wechat_bindings (
    user_id INTEGER PRIMARY KEY,
    persona_id INTEGER,
    bridge_token TEXT UNIQUE NOT NULL,
    home_dir TEXT,
    phase TEXT NOT NULL DEFAULT 'idle',
    bot_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_states (
    state TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_persona ON chat_turns(persona_id, id);
CREATE TABLE IF NOT EXISTS stickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    name TEXT,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stickers_persona ON stickers(persona_id);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    contact_key TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    last_turn_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(persona_id, contact_key)
);
CREATE INDEX IF NOT EXISTS idx_contacts_persona ON contacts(persona_id, contact_key);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    contact_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(persona_id, contact_key, id);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, id);
CREATE TABLE IF NOT EXISTS proactive_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proactive_log_persona ON proactive_log(persona_id, id);
CREATE TABLE IF NOT EXISTS farewells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL UNIQUE,
    contact TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_farewells_user ON farewells(user_id, persona_id);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    persona_id INTEGER,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, id);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    media TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS send_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_send_events_user ON send_events(user_id, created_at);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    read_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, read_at, id);
CREATE TABLE IF NOT EXISTS recovery_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    handled_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_recovery_status ON recovery_requests(status, id);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, id);
CREATE TABLE IF NOT EXISTS platform_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_platform_usage_user ON platform_usage(user_id, created_at);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    api_key_encrypted TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com/v1',
    model TEXT NOT NULL DEFAULT 'deepseek-chat',
    enabled INTEGER NOT NULL DEFAULT 0,
    per_turn_cost INTEGER NOT NULL DEFAULT 1,
    new_user_gift INTEGER NOT NULL DEFAULT 100,
    default_credit_days INTEGER NOT NULL DEFAULT 30,
    distill_ticket_price INTEGER NOT NULL DEFAULT 60,
    distill_ticket_gift INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    credits INTEGER NOT NULL,
    coins INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER NOT NULL DEFAULT 0,
    badge TEXT NOT NULL DEFAULT '',
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    validity_days INTEGER NOT NULL DEFAULT 30,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user ON credit_ledger(user_id, id);
CREATE TABLE IF NOT EXISTS coin_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_ledger_user ON coin_ledger(user_id, id);
CREATE TABLE IF NOT EXISTS redemption_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    coins INTEGER NOT NULL,
    batch TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unused',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    redeemed_by INTEGER,
    redeemed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_redemption_codes_status ON redemption_codes(status, id);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, scope, key)
);
CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    sticker_id INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moments_persona ON moments(persona_id, id);
CREATE TABLE IF NOT EXISTS moment_likes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(moment_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_moment_likes_moment ON moment_likes(moment_id);
CREATE TABLE IF NOT EXISTS moment_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    reply TEXT NOT NULL DEFAULT '',
    reply_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moment_comments_moment ON moment_comments(moment_id, id);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'gift',
    package_id INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    reminded_at TEXT NOT NULL DEFAULT '',
    expired_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_batches_user ON credit_batches(user_id, expires_at, id);
CREATE INDEX IF NOT EXISTS idx_credit_batches_live ON credit_batches(remaining, expires_at);
CREATE TABLE IF NOT EXISTS distill_ticket_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_distill_ticket_ledger_user
    ON distill_ticket_ledger(user_id, id);
"""

_PERSONA_COLUMNS = {
    "tag": "TEXT NOT NULL DEFAULT ''",
    "settings": "TEXT NOT NULL DEFAULT ''",
    "avatar": "TEXT NOT NULL DEFAULT ''",
    "last_contact": "TEXT NOT NULL DEFAULT ''",
}

_MIGRATIONS: dict[str, dict[str, str]] = {
    "personas": dict(_PERSONA_COLUMNS),
    "chat_turns": {"contact": "TEXT NOT NULL DEFAULT ''"},
    "model_configs": {
        "key_status": "TEXT NOT NULL DEFAULT ''",
        "key_error": "TEXT NOT NULL DEFAULT ''",
        "key_checked_at": "TEXT NOT NULL DEFAULT ''",
    },
    "tasks": {
        "result": "TEXT NOT NULL DEFAULT ''",
        "client_id": "TEXT NOT NULL DEFAULT ''",
        "error_kind": "TEXT NOT NULL DEFAULT ''",
    },
    "sessions": {"pending_totp": "INTEGER NOT NULL DEFAULT 0"},
    "users": {
        "credits": "INTEGER NOT NULL DEFAULT 0",
        "coins": "INTEGER NOT NULL DEFAULT 0",
        "distill_tickets": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "totp_secret": "TEXT NOT NULL DEFAULT ''",
        "totp_enabled": "INTEGER NOT NULL DEFAULT 0",
    },
    "credit_packages": {
        "coins": "INTEGER NOT NULL DEFAULT 0",
        "validity_days": "INTEGER NOT NULL DEFAULT 30",
    },
    "platform_config": {
        "default_credit_days": "INTEGER NOT NULL DEFAULT 30",
        "distill_ticket_price": "INTEGER NOT NULL DEFAULT 60",
        "distill_ticket_gift": "INTEGER NOT NULL DEFAULT 0",
    },
    "proactive_log": {"kind": "TEXT NOT NULL DEFAULT ''"},
}

# 积分与蒸馏券的有效期档位（固定天数）。
CREDIT_VALIDITY_DAYS = (30, 90, 365)
CREDIT_VALIDITY_LABELS = {30: "月度", 90: "季度", 365: "年度"}
DEFAULT_CREDIT_DAYS = 30

DEFAULT_PACKAGES = (
    ("体验包", 100, 10, "", 1, 30),
    ("标准包", 1000, 90, "推荐", 2, 90),
    ("尊享包", 5000, 400, "超值", 3, 365),
)


class InsufficientCredits(RuntimeError):
    """积分不足。``balance`` 为当前可用积分。"""

    def __init__(self, balance: int) -> None:
        super().__init__("积分不足")
        self.balance = balance


class InsufficientCoins(RuntimeError):
    """念念币不足。``balance`` 为当前可用念念币。"""

    def __init__(self, balance: int) -> None:
        super().__init__("念念币不足")
        self.balance = balance


class InsufficientDistillTickets(RuntimeError):
    """蒸馏券不足。``balance`` 为当前可用蒸馏券数量。"""

    def __init__(self, balance: int) -> None:
        super().__init__("蒸馏券不足")
        self.balance = balance


class InvalidValidityDays(ValueError):
    """有效期天数不合法（仅支持 30 / 90 / 365）。"""

    def __init__(self, days: object) -> None:
        super().__init__("有效期仅支持 30/90/365 天")
        self.days = days


class RedemptionError(RuntimeError):
    """兑换码无效或已被使用。"""

    def __init__(self, message: str = "兑换码无效") -> None:
        super().__init__(message)


def db_path() -> Path:
    override = os.getenv("PERSONA_DB_PATH")
    if override:
        return Path(override)
    return Path(os.getenv("PERSONA_DATA_DIR", "data")) / "platform.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    global _initialized
    with _lock:
        with connect() as conn:
            conn.executescript(SCHEMA)
            added_columns: set[tuple[str, str]] = set()
            for table, columns in _MIGRATIONS.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                        added_columns.add((table, column))
            # 依赖迁移列的索引必须在补列之后创建，否则老库启动时会报 no such column。
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_client ON tasks(user_id, kind, client_id)"
            )
            row = conn.execute("SELECT COUNT(*) AS n FROM credit_packages").fetchone()
            if int(row["n"]) == 0:
                now = utcnow()
                conn.executemany(
                    "INSERT INTO credit_packages (name, credits, coins, price_cents, badge, sort,"
                    " active, validity_days, created_at, updated_at)"
                    " VALUES (?, ?, ?, 0, ?, ?, 1, ?, ?, ?)",
                    [(name, credits, coins, badge, sort, days, now, now)
                     for name, credits, coins, badge, sort, days in DEFAULT_PACKAGES],
                )
            # 存量套餐（新货币模型前创建）仅在本次新增 coins 列时补齐定价一次，
            # 避免管理员把套餐改为免费后又被启动逻辑重置。
            if ("credit_packages", "coins") in added_columns:
                conn.executemany(
                    "UPDATE credit_packages SET coins = ? WHERE name = ? AND coins <= 0",
                    [(coins, name)
                     for name, _credits, coins, _badge, _sort, _days in DEFAULT_PACKAGES],
                )
            # 存量套餐补齐有效期档位：仅在本次新增 validity_days 列时执行一次，
            # 且只覆盖仍是默认值 30 的同名行，避免覆盖管理员的自定义设置。
            if ("credit_packages", "validity_days") in added_columns:
                conn.executemany(
                    "UPDATE credit_packages SET validity_days = ?"
                    " WHERE name = ? AND validity_days = ?",
                    [(days, name, DEFAULT_CREDIT_DAYS)
                     for name, _credits, _coins, _badge, _sort, days in DEFAULT_PACKAGES],
                )
            _migrate_credit_expiry(conn)
        _initialized = True


def _migrate_credit_expiry(conn: sqlite3.Connection) -> None:
    """一次性升级到「积分按批次、可到期清零」机制：清空历史余额（幂等）。

    升级前积分是永久余额，没有批次与到期概念。用户已确认升级时清空历史余额，
    因此这里把存量余额归零并留下一条说明流水，用 ``schema_meta`` 标记保证只执行一次。
    """
    if _meta_get(conn, "credit_expiry_migrated") == "1":
        return
    rows = conn.execute("SELECT id, credits FROM users WHERE credits > 0").fetchall()
    now = utcnow()
    for row in rows:
        balance = int(row["credits"])
        conn.execute("UPDATE users SET credits = 0 WHERE id = ?", (int(row["id"]),))
        conn.execute(
            "INSERT INTO credit_ledger (user_id, delta, balance_after, reason, actor,"
            " ref, created_at) VALUES (?, ?, 0, ?, 'system', ?, ?)",
            (int(row["id"]), -balance, "历史余额清空（有效期机制上线）",
             "credit-expiry-migration", now),
        )
    _meta_set(conn, "credit_expiry_migrated", "1")


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (str(key),)).fetchone()
    return str(row["value"]) if row is not None else ""


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (str(key), str(value), utcnow()),
    )


def _ensure() -> None:
    if not _initialized:
        init_db()


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _claim_idempotency(conn: sqlite3.Connection, user_id: int, scope: str, key: str) -> bool:
    """在当前事务内登记幂等键；首次返回 True，重复返回 False。

    调用方必须在同一个 ``connect()`` 事务里先 claim 再执行副作用，才能保证
    并发/重放下副作用只发生一次。
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys (user_id, scope, key, created_at)"
        " VALUES (?, ?, ?, ?)",
        (int(user_id), str(scope), str(key), utcnow()),
    )
    return cursor.rowcount == 1


# --------------------------------------------------------------------------- #
# 用户
# --------------------------------------------------------------------------- #

def create_user(
    username: str | None,
    password_hash: str | None = None,
    password_salt: str | None = None,
    nickname: str | None = None,
    wechat_openid: str | None = None,
    wechat_unionid: str | None = None,
    avatar: str | None = None,
    role: str = "user",
) -> dict:
    _ensure()
    with _lock:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, password_salt, wechat_openid,"
                " wechat_unionid, nickname, avatar, role, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, password_hash, password_salt, wechat_openid, wechat_unionid,
                 nickname, avatar, role, utcnow()),
            )
            new_id = cursor.lastrowid
    return get_user(new_id)  # type: ignore[arg-type]


def get_user(user_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_username(username: str) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def get_user_by_openid(openid: str) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM users WHERE wechat_openid = ?", (openid,)).fetchone())


def list_users() -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def admin_insights() -> dict:
    """运营看板聚合：新增、活跃、留存、付费与内容量。"""
    _ensure()
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week = (now - timedelta(days=7)).isoformat()
    month = (now - timedelta(days=30)).isoformat()
    with connect() as conn:
        def scalar(sql: str, params: tuple = ()) -> int:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        return {
            "users": {
                "total": scalar("SELECT COUNT(*) FROM users"),
                "new_today": scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (day,)),
                "new_week": scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (week,)),
                "new_month": scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (month,)),
            },
            "active": {
                "today": scalar(
                    "SELECT COUNT(DISTINCT user_id) FROM chat_turns WHERE created_at >= ?", (day,)
                ),
                "week": scalar(
                    "SELECT COUNT(DISTINCT user_id) FROM chat_turns WHERE created_at >= ?", (week,)
                ),
                "month": scalar(
                    "SELECT COUNT(DISTINCT user_id) FROM chat_turns WHERE created_at >= ?", (month,)
                ),
                "returning_week": scalar(
                    "SELECT COUNT(*) FROM (SELECT user_id FROM chat_turns WHERE created_at >= ?"
                    " GROUP BY user_id HAVING COUNT(DISTINCT substr(created_at, 1, 10)) >= 2)",
                    (week,),
                ),
            },
            "content": {
                "personas": scalar("SELECT COUNT(*) FROM personas"),
                "personas_active": scalar("SELECT COUNT(DISTINCT persona_id) FROM chat_turns"),
                "turns_total": scalar("SELECT COUNT(*) FROM chat_turns"),
                "turns_today": scalar("SELECT COUNT(*) FROM chat_turns WHERE created_at >= ?", (day,)),
                "turns_week": scalar("SELECT COUNT(*) FROM chat_turns WHERE created_at >= ?", (week,)),
                "bound_users": scalar(
                    "SELECT COUNT(*) FROM wechat_bindings WHERE phase IN ('logged-in', 'running')"
                ),
            },
            "revenue": {
                "coins_spent": scalar(
                    "SELECT COALESCE(-SUM(delta), 0) FROM coin_ledger WHERE delta < 0"
                ),
                "coins_spent_week": scalar(
                    "SELECT COALESCE(-SUM(delta), 0) FROM coin_ledger WHERE delta < 0"
                    " AND created_at >= ?",
                    (week,),
                ),
                "packages_purchased": scalar("SELECT COUNT(*) FROM coin_ledger WHERE delta < 0"),
                "codes_redeemed": scalar(
                    "SELECT COUNT(*) FROM redemption_codes WHERE status = 'used'"
                ),
            },
        }


def set_user_username(user_id: int, username: str, password_hash: str, password_salt: str) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE users SET username = ?, password_hash = ?, password_salt = ? WHERE id = ?",
            (username, password_hash, password_salt, user_id),
        )


def set_user_role(user_id: int, role: str) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role or "user", user_id))


def set_user_status(user_id: int, status: str) -> None:
    """启用/停用账号。停用只改状态并注销会话，数据与账本全部保留。"""
    _ensure()
    with _lock, connect() as conn:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status or "active", user_id))
        if status != "active":
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(user_id: int) -> None:
    """软删账号：标记为 deleted 并注销会话，保留历史与账本以备对账/恢复。"""
    _ensure()
    with _lock, connect() as conn:
        conn.execute("UPDATE users SET status = 'deleted' WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# 用户主动注销时需要清理的内容表；账本（credit_ledger/coin_ledger）保留用于对账。
_USER_CONTENT_TABLES = (
    "sessions",
    "outbox",
    "send_events",
    "alerts",
    "reports",
    "platform_usage",
    "tasks",
    "proactive_log",
    "feedback",
    "memories",
    "contacts",
    "chat_turns",
    "stickers",
    "wechat_bindings",
    "model_configs",
    "personas",
    "farewells",
)


def purge_user_content(user_id: int) -> None:
    """清除用户的人格、会话、记忆等个人内容，保留账号行与账本。"""
    _ensure()
    delete_moments_for_user(user_id)
    with _lock, connect() as conn:
        for table in _USER_CONTENT_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))


def delete_user_sessions(user_id: int, keep_token: str | None = None) -> None:
    """注销该用户的所有会话；传入 keep_token 时保留当前这一个。"""
    _ensure()
    with _lock, connect() as conn:
        if keep_token:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token != ?", (user_id, keep_token)
            )
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #

def create_session(user_id: int, token: str, days: int = 7, pending_totp: bool = False) -> str:
    _ensure()
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at, pending_totp)"
            " VALUES (?, ?, ?, ?, ?)",
            (token, user_id, utcnow(), expires, 1 if pending_totp else 0),
        )
    return expires


def get_user_by_session(token: str | None) -> dict | None:
    if not token:
        return None
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at AS _expires, s.pending_totp AS _pending FROM sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    user = dict(row)
    expires = user.pop("_expires", None)
    user["totp_pending"] = bool(user.pop("_pending", 0))
    if expires and expires < utcnow():
        delete_session(token)
        return None
    return user


def mark_session_verified(token: str | None) -> None:
    """完成二次验证后，把会话从「待验证」转为正常会话。"""
    if not token:
        return
    _ensure()
    with _lock, connect() as conn:
        conn.execute("UPDATE sessions SET pending_totp = 0 WHERE token = ?", (token,))


def set_user_totp(user_id: int, secret: str, enabled: bool) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE users SET totp_secret = ?, totp_enabled = ? WHERE id = ?",
            (secret or "", 1 if enabled else 0, user_id),
        )


def delete_session(token: str | None) -> None:
    if not token:
        return
    _ensure()
    with _lock, connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (utcnow(),))


def purge_expired_login_states() -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute("DELETE FROM login_states WHERE expires_at < ?", (utcnow(),))


# --------------------------------------------------------------------------- #
# 登录 state（微信扫码预留）
# --------------------------------------------------------------------------- #

def create_login_state(state: str, ttl_minutes: int = 10) -> None:
    _ensure()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO login_states (state, created_at, expires_at) VALUES (?, ?, ?)",
            (state, utcnow(), expires),
        )


def consume_login_state(state: str) -> bool:
    _ensure()
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM login_states WHERE state = ?", (state,)
        ).fetchone()
        conn.execute("DELETE FROM login_states WHERE state = ?", (state,))
    return bool(row) and row["expires_at"] >= utcnow()


# --------------------------------------------------------------------------- #
# 人格
# --------------------------------------------------------------------------- #

def list_personas(user_id: int) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personas WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_persona(user_id: int, persona_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM personas WHERE id = ? AND user_id = ?", (persona_id, user_id)
        ).fetchone())


def get_active_persona(user_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM personas WHERE user_id = ? AND is_active = 1 ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone())


def create_persona(
    user_id: int,
    name: str,
    directory: str,
    target_name: str | None = None,
    tag: str = "",
    settings: str = "",
    avatar: str = "",
) -> dict:
    _ensure()
    now = utcnow()
    with _lock:
        with connect() as conn:
            has_active = conn.execute(
                "SELECT COUNT(*) AS n FROM personas WHERE user_id = ?", (user_id,)
            ).fetchone()["n"]
            is_active = 1 if not has_active else 0
            cursor = conn.execute(
                "INSERT INTO personas (user_id, name, target_name, dir, is_active, status,"
                " tag, settings, avatar, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'empty', ?, ?, ?, ?, ?)",
                (user_id, name, target_name or name, directory, is_active, tag, settings,
                 avatar, now, now),
            )
            new_id = cursor.lastrowid
    return get_persona(user_id, new_id)  # type: ignore[arg-type]


def activate_persona(user_id: int, persona_id: int) -> dict | None:
    _ensure()
    with _lock, connect() as conn:
        owned = conn.execute(
            "SELECT id FROM personas WHERE id = ? AND user_id = ?", (persona_id, user_id)
        ).fetchone()
        if owned is None:
            return None
        conn.execute("UPDATE personas SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE personas SET is_active = 1, updated_at = ? WHERE id = ?", (utcnow(), persona_id)
        )
    return get_persona(user_id, persona_id)


def update_persona(user_id: int, persona_id: int, **fields) -> None:
    allowed = {
        "name", "target_name", "status", "dir", "tag", "settings", "avatar",
        "last_contact", "is_active",
    }
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if not updates:
        return
    _ensure()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [utcnow(), persona_id, user_id]
    with _lock, connect() as conn:
        conn.execute(
            f"UPDATE personas SET {assignments}, updated_at = ? WHERE id = ? AND user_id = ?",
            values,
        )


# 删除人格时需要一并清理的从表；按 (user_id, persona_id) 精确定位，避免残留孤儿数据。
_PERSONA_CHILD_TABLES = (
    "chat_turns",
    "stickers",
    "contacts",
    "memories",
    "feedback",
    "proactive_log",
    "tasks",
    "farewells",
)


def delete_persona(user_id: int, persona_id: int) -> None:
    _ensure()
    delete_moments_for_persona(user_id, persona_id)
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT is_active FROM personas WHERE id = ? AND user_id = ?",
            (persona_id, user_id),
        ).fetchone()
        was_active = bool(row and row["is_active"])
        for table in _PERSONA_CHILD_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE persona_id = ? AND user_id = ?",
                (persona_id, user_id),
            )
        conn.execute(
            "UPDATE wechat_bindings SET persona_id = NULL WHERE persona_id = ? AND user_id = ?",
            (persona_id, user_id),
        )
        conn.execute("DELETE FROM personas WHERE id = ? AND user_id = ?", (persona_id, user_id))
        # 删掉的是当前激活人格时，顺位激活另一个，避免用户剩下人格却没有可用分身。
        if was_active:
            other = conn.execute(
                "SELECT id FROM personas WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
            ).fetchone()
            if other is not None:
                conn.execute(
                    "UPDATE personas SET is_active = 1, updated_at = ? WHERE id = ?",
                    (utcnow(), other["id"]),
                )


# --------------------------------------------------------------------------- #
# 模型配置
# --------------------------------------------------------------------------- #

def set_model_config(user_id: int, api_key_encrypted: str, base_url: str, model: str) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO model_configs (user_id, api_key_encrypted, base_url, model, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET api_key_encrypted = excluded.api_key_encrypted,"
            " base_url = excluded.base_url, model = excluded.model, updated_at = excluded.updated_at,"
            " key_status = '', key_error = '', key_checked_at = ''",
            (user_id, api_key_encrypted, base_url, model, utcnow()),
        )


def set_model_key_status(user_id: int, status: str, error: str = "") -> None:
    """记录 Key 的最近一次健康状态（ok/invalid），供前端提示用户更新。"""
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE model_configs SET key_status = ?, key_error = ?, key_checked_at = ?"
            " WHERE user_id = ?",
            (status, (error or "")[:300], utcnow(), user_id),
        )


def get_model_config(user_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM model_configs WHERE user_id = ?", (user_id,)
        ).fetchone())


# --------------------------------------------------------------------------- #
# 微信桥接
# --------------------------------------------------------------------------- #

def upsert_wechat_binding(
    user_id: int,
    bridge_token: str,
    home_dir: str,
    persona_id: int | None = None,
    phase: str = "idle",
    bot_id: str | None = None,
) -> dict:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO wechat_bindings (user_id, persona_id, bridge_token, home_dir, phase,"
            " bot_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET persona_id = excluded.persona_id,"
            " home_dir = excluded.home_dir, phase = excluded.phase,"
            " bot_id = COALESCE(excluded.bot_id, wechat_bindings.bot_id),"
            " updated_at = excluded.updated_at",
            (user_id, persona_id, bridge_token, home_dir, phase, bot_id, utcnow()),
        )
    return get_wechat_binding(user_id)  # type: ignore[return-value]


def get_wechat_binding(user_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM wechat_bindings WHERE user_id = ?", (user_id,)
        ).fetchone())


def get_binding_by_token(token: str) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM wechat_bindings WHERE bridge_token = ?", (token,)
        ).fetchone())


def list_wechat_bindings() -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM wechat_bindings").fetchall()
    return [dict(row) for row in rows]


def set_binding_phase(user_id: int, phase: str, bot_id: str | None = None) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE wechat_bindings SET phase = ?, bot_id = COALESCE(?, bot_id), updated_at = ?"
            " WHERE user_id = ?",
            (phase, bot_id, utcnow(), user_id),
        )


def set_binding_persona(user_id: int, persona_id: int) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE wechat_bindings SET persona_id = ?, updated_at = ? WHERE user_id = ?",
            (persona_id, utcnow(), user_id),
        )


def set_binding_token(user_id: int, bridge_token: str) -> None:
    """轮换桥接令牌；旧令牌立即失效。"""
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE wechat_bindings SET bridge_token = ?, updated_at = ? WHERE user_id = ?",
            (bridge_token, utcnow(), user_id),
        )


# --------------------------------------------------------------------------- #
# 对话轮次（人格上下文记忆）
# --------------------------------------------------------------------------- #

def add_turn(user_id: int, persona_id: int, role: str, content: str, contact: str = "") -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO chat_turns (user_id, persona_id, role, content, contact, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, persona_id, role, content, contact or "", utcnow()),
        )


def list_turns(
    user_id: int, persona_id: int, limit: int = 20, contact: str | None = None
) -> list[dict]:
    _ensure()
    query = ("SELECT role, content, created_at FROM chat_turns"
             " WHERE persona_id = ? AND user_id = ?")
    params: list = [persona_id, user_id]
    if contact is not None:
        query += " AND contact = ?"
        params.append(contact)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in reversed(rows)]


def count_turns(user_id: int, persona_id: int, contact: str | None = None) -> int:
    _ensure()
    query = "SELECT COUNT(*) AS n FROM chat_turns WHERE persona_id = ? AND user_id = ?"
    params: list = [persona_id, user_id]
    if contact is not None:
        query += " AND contact = ?"
        params.append(contact)
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row["n"])


def clear_turns(user_id: int, persona_id: int, contact: str | None = None) -> None:
    _ensure()
    query = "DELETE FROM chat_turns WHERE persona_id = ? AND user_id = ?"
    params: list = [persona_id, user_id]
    if contact is not None:
        query += " AND contact = ?"
        params.append(contact)
    with _lock, connect() as conn:
        conn.execute(query, params)


# --------------------------------------------------------------------------- #
# 联系人（按聊天对象隔离）
# --------------------------------------------------------------------------- #

def touch_contact(
    user_id: int, persona_id: int, contact_key: str, display_name: str = ""
) -> dict:
    """创建或刷新联系人，返回联系人记录。"""
    _ensure()
    now = utcnow()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO contacts (user_id, persona_id, contact_key, display_name,"
            " last_turn_id, created_at, last_seen_at) VALUES (?, ?, ?, ?, 0, ?, ?)"
            " ON CONFLICT(persona_id, contact_key) DO UPDATE SET last_seen_at = excluded.last_seen_at,"
            " display_name = CASE WHEN excluded.display_name = '' THEN contacts.display_name"
            " ELSE excluded.display_name END",
            (user_id, persona_id, contact_key, display_name or "", now, now),
        )
    return get_contact(user_id, persona_id, contact_key)  # type: ignore[return-value]


def get_contact(user_id: int, persona_id: int, contact_key: str) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM contacts WHERE persona_id = ? AND user_id = ? AND contact_key = ?",
            (persona_id, user_id, contact_key),
        ).fetchone())


def list_contacts(user_id: int, persona_id: int) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE persona_id = ? AND user_id = ?"
            " ORDER BY last_seen_at DESC",
            (persona_id, user_id),
        ).fetchall()
    contacts = []
    for row in rows:
        item = dict(row)
        item["memory_count"] = count_memories(user_id, persona_id, item["contact_key"])
        item["turn_count"] = count_turns(user_id, persona_id, item["contact_key"])
        contacts.append(item)
    return contacts


def set_contact_name(user_id: int, persona_id: int, contact_key: str, display_name: str) -> bool:
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "UPDATE contacts SET display_name = ? WHERE persona_id = ? AND user_id = ?"
            " AND contact_key = ?",
            (display_name, persona_id, user_id, contact_key),
        )
        return cursor.rowcount > 0


def set_contact_last_turn(
    user_id: int, persona_id: int, contact_key: str, last_turn_id: int
) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE contacts SET last_turn_id = ? WHERE persona_id = ? AND user_id = ?"
            " AND contact_key = ?",
            (int(last_turn_id), persona_id, user_id, contact_key),
        )


def list_turns_after(user_id: int, persona_id: int, contact: str, after_id: int) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content FROM chat_turns WHERE persona_id = ? AND user_id = ?"
            " AND contact = ? AND id > ? ORDER BY id",
            (persona_id, user_id, contact, int(after_id)),
        ).fetchall()
    return [dict(row) for row in rows]


def last_turn_id(user_id: int, persona_id: int, contact: str) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS n FROM chat_turns WHERE persona_id = ? AND user_id = ?"
            " AND contact = ?",
            (persona_id, user_id, contact),
        ).fetchone()
    return int(row["n"] or 0)


# --------------------------------------------------------------------------- #
# 长期记忆
# --------------------------------------------------------------------------- #

MEMORY_KINDS = {"fact", "preference", "event", "relation"}


def add_memory(
    user_id: int, persona_id: int, contact_key: str, kind: str, content: str
) -> dict:
    _ensure()
    kind = kind if kind in MEMORY_KINDS else "fact"
    now = utcnow()
    with _lock:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO memories (user_id, persona_id, contact_key, kind, content,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, persona_id, contact_key or "", kind, content, now, now),
            )
            new_id = cursor.lastrowid
    return get_memory(user_id, new_id)  # type: ignore[arg-type]


def get_memory(user_id: int, memory_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)
        ).fetchone())


def list_memories(
    user_id: int, persona_id: int, contact_key: str | None = None, limit: int = 500
) -> list[dict]:
    _ensure()
    query = "SELECT * FROM memories WHERE persona_id = ? AND user_id = ?"
    params: list = [persona_id, user_id]
    if contact_key is not None:
        query += " AND contact_key = ?"
        params.append(contact_key)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in reversed(rows)]


def count_memories(user_id: int, persona_id: int, contact_key: str | None = None) -> int:
    _ensure()
    query = "SELECT COUNT(*) AS n FROM memories WHERE persona_id = ? AND user_id = ?"
    params: list = [persona_id, user_id]
    if contact_key is not None:
        query += " AND contact_key = ?"
        params.append(contact_key)
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row["n"])


def delete_memory(user_id: int, persona_id: int, memory_id: int) -> bool:
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ? AND persona_id = ?",
            (memory_id, user_id, persona_id),
        )
        return cursor.rowcount > 0


def update_memory(
    user_id: int, persona_id: int, memory_id: int, content: str, kind: str | None = None
) -> bool:
    """修正一条记忆的内容（以及可选的类型）；返回是否命中。"""
    _ensure()
    content = (content or "").strip()
    if not content:
        return False
    now = utcnow()
    with _lock, connect() as conn:
        if kind is not None:
            kind = kind if kind in MEMORY_KINDS else "fact"
            cursor = conn.execute(
                "UPDATE memories SET content = ?, kind = ?, updated_at = ?"
                " WHERE id = ? AND user_id = ? AND persona_id = ?",
                (content, kind, now, memory_id, user_id, persona_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE memories SET content = ?, updated_at = ?"
                " WHERE id = ? AND user_id = ? AND persona_id = ?",
                (content, now, memory_id, user_id, persona_id),
            )
        return cursor.rowcount > 0


def clear_memories(user_id: int, persona_id: int, contact_key: str | None = None) -> int:
    _ensure()
    query = "DELETE FROM memories WHERE persona_id = ? AND user_id = ?"
    params: list = [persona_id, user_id]
    if contact_key is not None:
        query += " AND contact_key = ?"
        params.append(contact_key)
    with _lock, connect() as conn:
        return conn.execute(query, params).rowcount


# --------------------------------------------------------------------------- #
# 问题反馈
# --------------------------------------------------------------------------- #

def add_feedback(user_id: int, persona_id: int, content: str, contact: str = "") -> dict:
    _ensure()
    now = utcnow()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "INSERT INTO feedback (user_id, persona_id, content, contact, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, persona_id, content, contact, now),
        )
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def list_feedback(user_id: int, limit: int = 50) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# 表情包
# --------------------------------------------------------------------------- #

def add_sticker(user_id: int, persona_id: int, name: str, path: str) -> dict:
    _ensure()
    with _lock:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO stickers (user_id, persona_id, name, path, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (user_id, persona_id, name, path, utcnow()),
            )
            new_id = cursor.lastrowid
    return get_sticker(user_id, new_id)  # type: ignore[arg-type]


def list_stickers(user_id: int, persona_id: int) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM stickers WHERE persona_id = ? AND user_id = ? ORDER BY id DESC",
            (persona_id, user_id),
        ).fetchall()
    return [dict(row) for row in rows]


def get_sticker(user_id: int, sticker_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM stickers WHERE id = ? AND user_id = ?", (sticker_id, user_id)
        ).fetchone())


def get_sticker_by_id(sticker_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM stickers WHERE id = ?", (sticker_id,)
        ).fetchone())


def delete_sticker(user_id: int, persona_id: int, sticker_id: int) -> dict | None:
    _ensure()
    sticker = get_sticker(user_id, sticker_id)
    if sticker is None or int(sticker.get("persona_id") or 0) != int(persona_id):
        return None
    with _lock, connect() as conn:
        cursor = conn.execute(
            "DELETE FROM stickers WHERE id = ? AND user_id = ? AND persona_id = ?",
            (sticker_id, user_id, persona_id),
        )
    return sticker if cursor.rowcount > 0 else None


def add_moment(
    user_id: int, persona_id: int, content: str, sticker_id: int = 0, source: str = "auto"
) -> dict:
    _ensure()
    with _lock:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO moments (user_id, persona_id, content, sticker_id, source,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, persona_id, content, int(sticker_id or 0), source or "auto", utcnow()),
            )
            new_id = cursor.lastrowid
    return get_moment(user_id, new_id)  # type: ignore[arg-type]


def get_moment(user_id: int, moment_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM moments WHERE id = ? AND user_id = ?", (moment_id, user_id)
        ).fetchone())


def list_moments(
    user_id: int, persona_id: int, limit: int = 20, before_id: int = 0
) -> list[dict]:
    """按 id 倒序返回某人格的动态，附带点赞数、评论数与当前用户是否已赞。"""
    _ensure()
    limit = min(max(int(limit or 20), 1), 50)
    query = (
        "SELECT m.*,"
        " (SELECT COUNT(*) FROM moment_likes l WHERE l.moment_id = m.id) AS like_count,"
        " (SELECT COUNT(*) FROM moment_comments c WHERE c.moment_id = m.id) AS comment_count,"
        " EXISTS(SELECT 1 FROM moment_likes l WHERE l.moment_id = m.id AND l.user_id = ?)"
        " AS liked"
        " FROM moments m WHERE m.persona_id = ? AND m.user_id = ?"
    )
    params: list = [user_id, persona_id, user_id]
    if before_id and int(before_id) > 0:
        query += " AND m.id < ?"
        params.append(int(before_id))
    query += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def count_moments_since(persona_id: int, since_iso: str) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM moments WHERE persona_id = ? AND created_at >= ?",
            (persona_id, since_iso),
        ).fetchone()
    return int(row["n"])


def last_moment_at(persona_id: int) -> str:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM moments WHERE persona_id = ? ORDER BY id DESC LIMIT 1",
            (persona_id,),
        ).fetchone()
    return row["created_at"] if row else ""


def delete_moment(user_id: int, moment_id: int) -> bool:
    """删除动态并级联清除其点赞与评论；未命中返回 False。"""
    _ensure()
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT id FROM moments WHERE id = ? AND user_id = ?", (moment_id, user_id)
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM moment_likes WHERE moment_id = ?", (moment_id,))
        conn.execute("DELETE FROM moment_comments WHERE moment_id = ?", (moment_id,))
        conn.execute(
            "DELETE FROM moments WHERE id = ? AND user_id = ?", (moment_id, user_id)
        )
    return True


def set_moment_like(moment_id: int, user_id: int, liked: bool) -> bool:
    """点赞或取消点赞；点赞为幂等操作，返回操作后的点赞状态。"""
    _ensure()
    with _lock, connect() as conn:
        if liked:
            conn.execute(
                "INSERT OR IGNORE INTO moment_likes (moment_id, user_id, created_at)"
                " VALUES (?, ?, ?)",
                (moment_id, user_id, utcnow()),
            )
        else:
            conn.execute(
                "DELETE FROM moment_likes WHERE moment_id = ? AND user_id = ?",
                (moment_id, user_id),
            )
    return bool(liked)


def count_moment_likes(moment_id: int) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM moment_likes WHERE moment_id = ?", (moment_id,)
        ).fetchone()
    return int(row["n"])


def list_moment_comments(moment_id: int) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM moment_comments WHERE moment_id = ? ORDER BY id", (moment_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_moment_comment(comment_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM moment_comments WHERE id = ?", (comment_id,)
        ).fetchone())


def add_moment_comment(moment_id: int, user_id: int, content: str) -> dict:
    _ensure()
    now = utcnow()
    with _lock:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO moment_comments (moment_id, user_id, content, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?)",
                (moment_id, user_id, content, now, now),
            )
            new_id = cursor.lastrowid
    return get_moment_comment(new_id)  # type: ignore[arg-type]


def set_moment_comment_reply(comment_id: int, reply: str, status: str) -> bool:
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "UPDATE moment_comments SET reply = ?, reply_status = ?, updated_at = ?"
            " WHERE id = ?",
            (reply or "", status or "done", utcnow(), comment_id),
        )
        return cursor.rowcount > 0


def delete_moments_for_persona(user_id: int, persona_id: int) -> None:
    """删除某人格的全部动态及其点赞与评论（人格删除/用户清空内容时调用）。"""
    _ensure()
    with _lock, connect() as conn:
        for table in ("moment_likes", "moment_comments"):
            conn.execute(
                f"DELETE FROM {table} WHERE moment_id IN"
                " (SELECT id FROM moments WHERE user_id = ? AND persona_id = ?)",
                (user_id, persona_id),
            )
        conn.execute(
            "DELETE FROM moments WHERE user_id = ? AND persona_id = ?", (user_id, persona_id)
        )


def delete_moments_for_user(user_id: int) -> None:
    """删除某用户的全部动态及其点赞与评论（账号注销清空内容时调用）。"""
    _ensure()
    with _lock, connect() as conn:
        for table in ("moment_likes", "moment_comments"):
            conn.execute(
                f"DELETE FROM {table} WHERE moment_id IN"
                " (SELECT id FROM moments WHERE user_id = ?)",
                (user_id,),
            )
        conn.execute("DELETE FROM moments WHERE user_id = ?", (user_id,))


def list_proactive_personas() -> list[dict]:
    """返回开启了主动消息的人格及其用户，供调度器使用。"""
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.*, u.username AS username FROM personas p"
            " JOIN users u ON u.id = p.user_id WHERE p.status = 'ready' ORDER BY p.id"
        ).fetchall()
    return [dict(row) for row in rows]


def add_proactive_log(
    user_id: int, persona_id: int, contact: str, content: str, kind: str = ""
) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO proactive_log (user_id, persona_id, contact, content, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, persona_id, contact or "", content or "", kind or "", utcnow()),
        )


def last_proactive_at(persona_id: int, kind: str | None = None) -> str:
    _ensure()
    query = "SELECT created_at FROM proactive_log WHERE persona_id = ?"
    params: list = [persona_id]
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY id DESC LIMIT 1"
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return row["created_at"] if row else ""


def count_proactive_since(persona_id: int, since_iso: str, kind: str | None = None) -> int:
    _ensure()
    query = "SELECT COUNT(*) AS n FROM proactive_log WHERE persona_id = ? AND created_at >= ?"
    params: list = [persona_id, since_iso]
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row["n"])


def list_proactive_log(user_id: int, persona_id: int, contact: str = "", limit: int = 50) -> list[dict]:
    _ensure()
    query = "SELECT * FROM proactive_log WHERE user_id = ? AND persona_id = ?"
    params: list = [user_id, persona_id]
    if contact:
        query += " AND contact = ?"
        params.append(contact)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(int(limit), 1))
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def last_turn_at(user_id: int, persona_id: int, contact: str = "", role: str = "user") -> str:
    """最近一条指定角色消息的时间，用于判断对方沉默了多久。"""
    _ensure()
    query = (
        "SELECT created_at FROM chat_turns WHERE user_id = ? AND persona_id = ? AND role = ?"
    )
    params: list = [user_id, persona_id, role]
    if contact:
        query += " AND contact = ?"
        params.append(contact)
    query += " ORDER BY id DESC LIMIT 1"
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return row["created_at"] if row else ""


# --------------------------------------------------------------------------- #
# 好好告别与回忆时间线
# --------------------------------------------------------------------------- #


def save_farewell(user_id: int, persona_id: int, contact: str, content: str) -> dict:
    """保存一封告别信；同一人格重复告别时覆盖旧内容。"""
    _ensure()
    now = utcnow()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO farewells (user_id, persona_id, contact, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(persona_id) DO UPDATE SET user_id = excluded.user_id,"
            " contact = excluded.contact, content = excluded.content,"
            " created_at = excluded.created_at",
            (user_id, persona_id, contact or "", content or "", now),
        )
        row = conn.execute(
            "SELECT * FROM farewells WHERE persona_id = ? AND user_id = ?", (persona_id, user_id)
        ).fetchone()
    return dict(row) if row else {}


def get_farewell(user_id: int, persona_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM farewells WHERE persona_id = ? AND user_id = ?", (persona_id, user_id)
        ).fetchone()
    return dict(row) if row else None


def timeline(user_id: int, persona_id: int, contact: str = "", limit: int = 400) -> dict:
    """聚合一个人的回忆时间线：逐日对话、长期记忆与主动消息。"""
    _ensure()
    where = "user_id = ? AND persona_id = ?"
    params: list = [user_id, persona_id]
    if contact:
        where += " AND contact = ?"
        params.append(contact)
    with connect() as conn:
        day_rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, role, COUNT(*) AS n,"
            f" MIN(created_at) AS first_at FROM chat_turns WHERE {where}"
            " GROUP BY day, role ORDER BY day, role",
            params,
        ).fetchall()
        total_row = conn.execute(
            f"SELECT COUNT(*) AS total, MIN(created_at) AS first_at, MAX(created_at) AS last_at"
            f" FROM chat_turns WHERE {where}",
            params,
        ).fetchone()
    memories = list_memories(user_id, persona_id, contact or None, limit=limit)
    proactive = list_proactive_log(user_id, persona_id, contact, limit=limit)
    return {
        "contact": contact,
        "total": int(total_row["total"] or 0) if total_row else 0,
        "first_at": (total_row["first_at"] if total_row else "") or "",
        "last_at": (total_row["last_at"] if total_row else "") or "",
        "days": [dict(row) for row in day_rows],
        "memories": [dict(item) for item in memories],
        "proactive": [dict(item) for item in proactive],
    }


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #

def create_task(
    task_id: str, user_id: int, persona_id: int | None, kind: str, client_id: str = ""
) -> dict:
    _ensure()
    now = utcnow()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, user_id, persona_id, kind, status, progress, message,"
            " error, result, client_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'running', 0, '', '', '', ?, ?, ?)",
            (task_id, user_id, persona_id, kind, client_id, now, now),
        )
    return get_task(task_id) or {}


def find_task_by_client(user_id: int, kind: str, client_id: str) -> dict | None:
    """按客户端幂等键查找最近一次任务，用于避免重复提交重复建人格。"""
    if not client_id:
        return None
    _ensure()
    with connect() as conn:
        return _decode_task(
            conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND kind = ? AND client_id = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (user_id, kind, client_id),
            ).fetchone()
        )


def _decode_task(row: sqlite3.Row | None) -> dict | None:
    task = _row(row)
    if task is None:
        return None
    raw = task.get("result") or ""
    if raw:
        try:
            task["result"] = json.loads(raw)
        except (ValueError, TypeError):
            task["result"] = None
    else:
        task["result"] = None
    return task


def get_task(task_id: str) -> dict | None:
    _ensure()
    with connect() as conn:
        return _decode_task(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )


def list_tasks(user_id: int, limit: int = 20) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(int(limit), 1)),
        ).fetchall()
    return [_decode_task(row) or {} for row in rows]


def update_task(
    task_id: str,
    *,
    progress: int | None = None,
    message: str | None = None,
    status: str | None = None,
    error: str | None = None,
    error_kind: str | None = None,
    result: dict | None = None,
) -> None:
    _ensure()
    fields: list[str] = ["updated_at = ?"]
    params: list = [utcnow()]
    if progress is not None:
        fields.append("progress = ?")
        params.append(max(0, min(int(progress), 100)))
    if message is not None:
        fields.append("message = ?")
        params.append(message)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    if error_kind is not None:
        fields.append("error_kind = ?")
        params.append(error_kind)
    if result is not None:
        fields.append("result = ?")
        params.append(json.dumps(result, ensure_ascii=False))
    params.append(task_id)
    with _lock, connect() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params)


def prune_tasks(days: int = 7) -> int:
    """清理超过保留期的已完成任务，返回清理条数。"""
    _ensure()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(int(days), 1))).isoformat()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "DELETE FROM tasks WHERE status != 'running' AND updated_at < ?", (cutoff,)
        )
    return cursor.rowcount


def fail_stale_tasks(reason: str = "服务重启，任务已中断，请重试") -> int:
    """把重启后残留的 running 任务标记为失败。

    任务跑在进程内的守护线程里，服务一重启线程就没了，状态会永远停在 running，
    前端会一直转圈。启动时统一收尾。
    """
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET status = 'error', message = '失败', error = ?, updated_at = ?"
            " WHERE status = 'running'",
            (reason, utcnow()),
        )
    return cursor.rowcount


# --------------------------------------------------------------------------- #
# 出站队列
# --------------------------------------------------------------------------- #

def add_outbox(
    user_id: int, recipient: str, text: str = "", media: str = "", delay_seconds: float = 0
) -> int:
    _ensure()
    now = utcnow()
    delay = max(float(delay_seconds or 0), 0.0)
    if delay:
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    else:
        next_at = now
    with _lock, connect() as conn:
        cursor = conn.execute(
            "INSERT INTO outbox (user_id, recipient, text, media, status, attempts,"
            " last_error, next_attempt_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', 0, '', ?, ?, ?)",
            (user_id, recipient, text or "", media or "", next_at, now, now),
        )
        return int(cursor.lastrowid or 0)


def list_due_outbox(now_iso: str, limit: int = 10) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE status = 'pending' AND next_attempt_at <= ?"
            " ORDER BY id LIMIT ?",
            (now_iso, max(int(limit), 1)),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_outbox_sent(outbox_id: int) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE outbox SET status = 'sent', last_error = '', updated_at = ? WHERE id = ?",
            (utcnow(), outbox_id),
        )


def mark_outbox_retry(outbox_id: int, error: str, next_attempt_at: str, attempts: int) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE outbox SET status = 'pending', attempts = ?, last_error = ?,"
            " next_attempt_at = ?, updated_at = ? WHERE id = ?",
            (attempts, error or "", next_attempt_at, utcnow(), outbox_id),
        )


def mark_outbox_failed(outbox_id: int, error: str) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE outbox SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
            (error or "", utcnow(), outbox_id),
        )


def count_outbox_sent_since(user_id: int, since_iso: str) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE user_id = ? AND status = 'sent'"
            " AND updated_at >= ?",
            (user_id, since_iso),
        ).fetchone()
    return int(row["n"])


def outbox_stats(limit: int = 500) -> dict:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"
        ).fetchall()
        failed = conn.execute(
            "SELECT id, user_id, last_error, attempts, updated_at FROM outbox"
            " WHERE status = 'failed' ORDER BY id DESC LIMIT ?",
            (max(int(limit), 1),),
        ).fetchall()
    return {
        "counts": {row["status"]: int(row["n"]) for row in rows},
        "recent_failures": [dict(row) for row in failed],
    }


# --------------------------------------------------------------------------- #
# 发送计数与站内提醒（防封）
# --------------------------------------------------------------------------- #

# 发送事件的轻量清理节流：每次进程内最多每小时清理一次。
_send_prune_at = 0.0


def add_send_event(user_id: int) -> None:
    """记录一次成功发出的消息，用于每日/每分钟配额统计。"""
    _ensure()
    global _send_prune_at
    now = datetime.now(timezone.utc).timestamp()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO send_events (user_id, created_at) VALUES (?, ?)",
            (user_id, utcnow()),
        )
        if now - _send_prune_at > 3600:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            conn.execute("DELETE FROM send_events WHERE created_at < ?", (cutoff,))
            _send_prune_at = now


def count_send_events_since(user_id: int, since_iso: str) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM send_events WHERE user_id = ? AND created_at >= ?",
            (user_id, since_iso),
        ).fetchone()
    return int(row["n"])


def add_alert(user_id: int, kind: str, message: str) -> int:
    """写入一条站内提醒（掉线、发送失败、触发配额等）。"""
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "INSERT INTO alerts (user_id, kind, message, read_at, created_at)"
            " VALUES (?, ?, ?, '', ?)",
            (user_id, kind or "info", message or "", utcnow()),
        )
        return int(cursor.lastrowid or 0)


def list_alerts(user_id: int, limit: int = 20, unread_only: bool = False) -> list[dict]:
    _ensure()
    query = "SELECT * FROM alerts WHERE user_id = ?"
    params: list = [user_id]
    if unread_only:
        query += " AND read_at = ''"
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(int(limit), 1))
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def count_unread_alerts(user_id: int) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE user_id = ? AND read_at = ''",
            (user_id,),
        ).fetchone()
    return int(row["n"])


def mark_alerts_read(user_id: int) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE alerts SET read_at = ? WHERE user_id = ? AND read_at = ''",
            (utcnow(), user_id),
        )


# --------------------------------------------------------------------------- #
# 人工找回密码请求
# --------------------------------------------------------------------------- #


def add_recovery_request(
    username: str, contact: str = "", note: str = "", user_id: int | None = None
) -> int:
    """记录一条找回密码申请；即使没有匹配账号也落库，避免暴露账号是否存在。"""
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "INSERT INTO recovery_requests (user_id, username, contact, note, status, created_at,"
            " handled_at) VALUES (?, ?, ?, ?, 'open', ?, '')",
            (user_id, (username or "").strip()[:64], (contact or "").strip()[:128],
             (note or "").strip()[:500], utcnow()),
        )
        return int(cursor.lastrowid or 0)


def list_recovery_requests(status: str = "open", limit: int = 50) -> list[dict]:
    _ensure()
    query = "SELECT * FROM recovery_requests"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(int(limit), 1))
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def count_open_recovery_requests() -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM recovery_requests WHERE status = 'open'"
        ).fetchone()
    return int(row["n"])


def set_recovery_status(request_id: int, status: str) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE recovery_requests SET status = ?, handled_at = ? WHERE id = ?",
            (status or "open", utcnow(), int(request_id)),
        )


# --------------------------------------------------------------------------- #
# 举报与平台模型用量
# --------------------------------------------------------------------------- #

def add_report(user_id: int, category: str, detail: str, persona_id: int = 0) -> dict:
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "INSERT INTO reports (user_id, persona_id, category, detail, status, created_at)"
            " VALUES (?, ?, ?, ?, 'open', ?)",
            (user_id, int(persona_id or 0), category or "", detail or "", utcnow()),
        )
        report_id = int(cursor.lastrowid or 0)
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return dict(row) if row else {}


def list_reports(status: str = "", limit: int = 100) -> list[dict]:
    _ensure()
    query = (
        "SELECT reports.*, users.username AS username FROM reports"
        " LEFT JOIN users ON users.id = reports.user_id"
    )
    params: list = []
    if status:
        query += " WHERE reports.status = ?"
        params.append(status)
    query += " ORDER BY reports.id DESC LIMIT ?"
    params.append(max(int(limit), 1))
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def set_report_status(report_id: int, status: str) -> None:
    _ensure()
    with _lock, connect() as conn:
        conn.execute("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))


def count_reports(status: str = "open") -> int:
    _ensure()
    query = "SELECT COUNT(*) AS n FROM reports"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row["n"])


def add_audit(
    actor_id: int, actor: str, action: str, target: str = "", detail: str = ""
) -> int:
    """记录一次管理员敏感操作，便于事后审计。"""
    _ensure()
    with _lock, connect() as conn:
        cursor = conn.execute(
            "INSERT INTO admin_audit (actor_id, actor, action, target, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (int(actor_id or 0), actor or "", action or "", target or "", detail or "", utcnow()),
        )
        return int(cursor.lastrowid or 0)


def list_audit(limit: int = 100) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (max(int(limit), 1),)
        ).fetchall()
    return [dict(row) for row in rows]


def add_platform_call(user_id: int) -> None:
    """记录一次平台内置模型的调用。"""
    _ensure()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO platform_usage (user_id, created_at) VALUES (?, ?)",
            (user_id, utcnow()),
        )


def count_platform_calls_since(user_id: int, since_iso: str) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM platform_usage WHERE user_id = ? AND created_at >= ?",
            (user_id, since_iso),
        ).fetchone()
    return int(row["n"])


def count_personas_for_user(user_id: int) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM personas WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["n"])


def platform_usage_stats(since_iso: str = "") -> dict:
    _ensure()
    query = (
        "SELECT user_id, COUNT(*) AS n FROM platform_usage"
        + (" WHERE created_at >= ?" if since_iso else "")
        + " GROUP BY user_id ORDER BY n DESC LIMIT 50"
    )
    with connect() as conn:
        rows = conn.execute(query, (since_iso,) if since_iso else ()).fetchall()
    return {"by_user": [dict(row) for row in rows]}


def count_users() -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def count_personas() -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM personas").fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------- #
# 平台模型配置
# --------------------------------------------------------------------------- #

def get_platform_config_row() -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM platform_config WHERE id = 1").fetchone())


def set_platform_config(
    api_key_encrypted: str,
    base_url: str,
    model: str,
    enabled: bool,
    per_turn_cost: int,
    new_user_gift: int,
    default_credit_days: int | None = None,
    distill_ticket_price: int | None = None,
    distill_ticket_gift: int | None = None,
) -> dict:
    _ensure()
    current = get_platform_config_row() or {}
    days = normalize_validity_days(
        default_credit_days if default_credit_days is not None
        else current.get("default_credit_days")
    )
    ticket_price = max(int(
        distill_ticket_price if distill_ticket_price is not None
        else current.get("distill_ticket_price", 60) or 0
    ), 0)
    ticket_gift = max(int(
        distill_ticket_gift if distill_ticket_gift is not None
        else current.get("distill_ticket_gift", 0) or 0
    ), 0)
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO platform_config (id, api_key_encrypted, base_url, model, enabled,"
            " per_turn_cost, new_user_gift, default_credit_days, distill_ticket_price,"
            " distill_ticket_gift, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET api_key_encrypted = excluded.api_key_encrypted,"
            " base_url = excluded.base_url, model = excluded.model, enabled = excluded.enabled,"
            " per_turn_cost = excluded.per_turn_cost, new_user_gift = excluded.new_user_gift,"
            " default_credit_days = excluded.default_credit_days,"
            " distill_ticket_price = excluded.distill_ticket_price,"
            " distill_ticket_gift = excluded.distill_ticket_gift,"
            " updated_at = excluded.updated_at",
            (api_key_encrypted, base_url, model, 1 if enabled else 0,
             int(per_turn_cost), int(new_user_gift), days, ticket_price, ticket_gift, utcnow()),
        )
    return get_platform_config_row() or {}


# --------------------------------------------------------------------------- #
# 积分
# --------------------------------------------------------------------------- #

def get_credits(user_id: int) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row["credits"]) if row is not None else 0


def normalize_validity_days(days: int | None) -> int:
    """校验有效期档位，仅接受 30 / 90 / 365；``None`` 回落到默认 30 天。"""
    value = DEFAULT_CREDIT_DAYS if days is None else int(days)
    if value not in CREDIT_VALIDITY_DAYS:
        raise InvalidValidityDays(days)
    return value


def _expires_at(days: int, now: str | None = None) -> str:
    base = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    return (base + timedelta(days=int(days))).isoformat()


def _live_batch_total(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(remaining), 0) AS n FROM credit_batches"
        " WHERE user_id = ? AND remaining > 0 AND expired_at = ''",
        (int(user_id),),
    ).fetchone()
    return int(row["n"])


def _consume_batches(conn: sqlite3.Connection, user_id: int, amount: int) -> int:
    """按到期时间最早优先扣减批次，返回实际扣减量。"""
    remaining = int(amount)
    if remaining <= 0:
        return 0
    rows = conn.execute(
        "SELECT id, remaining FROM credit_batches"
        " WHERE user_id = ? AND remaining > 0 AND expired_at = ''"
        " ORDER BY expires_at, id",
        (int(user_id),),
    ).fetchall()
    consumed = 0
    for row in rows:
        if remaining <= 0:
            break
        take = min(int(row["remaining"]), remaining)
        if take <= 0:
            continue
        conn.execute(
            "UPDATE credit_batches SET remaining = remaining - ? WHERE id = ?",
            (take, int(row["id"])),
        )
        remaining -= take
        consumed += take
    return consumed


def grant_credits(
    user_id: int,
    delta: int,
    reason: str = "",
    actor: str = "",
    ref: str = "",
    idem: str = "",
    expires_days: int | None = None,
    source: str = "gift",
    package_id: int | None = None,
) -> dict:
    """在单事务内调整用户积分并写入流水，余额不足时抛出 InsufficientCredits。

    ``delta > 0`` 时按 ``expires_days``（缺省取默认档位）新建一条带到期时间的积分批次；
    ``delta < 0`` 时按最早到期优先消耗批次额度。``idem`` 非空时启用幂等：同一
    (user_id, scope="credits.grant", idem) 只会生效一次。
    """
    _ensure()
    delta = int(delta)
    with _lock, connect() as conn:
        row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError("用户不存在")
        if idem and not _claim_idempotency(conn, user_id, "credits.grant", idem):
            return {"balance": int(row["credits"]), "delta": 0, "duplicate": True}
        if delta > 0:
            days = normalize_validity_days(expires_days)
            now = utcnow()
            conn.execute(
                "INSERT INTO credit_batches (user_id, amount, remaining, source, package_id,"
                " reason, actor, ref, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, delta, delta, str(source or "gift"), package_id, reason, actor, ref,
                 _expires_at(days, now), now),
            )
        elif delta < 0:
            needed = -delta
            available = int(row["credits"])
            live = _live_batch_total(conn, user_id)
            if available < needed or live < needed:
                raise InsufficientCredits(available)
            _consume_batches(conn, user_id, needed)
        # 相对增减 + 非负约束放在同一条 UPDATE 内，避免跨进程读改写丢更新。
        cursor = conn.execute(
            "UPDATE users SET credits = credits + ? WHERE id = ? AND credits + ? >= 0",
            (delta, user_id, delta),
        )
        if cursor.rowcount != 1:
            raise InsufficientCredits(int(row["credits"]))
        new_balance = int(
            conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()["credits"]
        )
        if delta:
            conn.execute(
                "INSERT INTO credit_ledger (user_id, delta, balance_after, reason, actor,"
                " ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, delta, new_balance, reason, actor, ref, utcnow()),
            )
    return {"balance": new_balance, "delta": delta, "duplicate": False}


def deduct_credits(user_id: int, amount: int, reason: str = "", ref: str = "") -> dict:
    return grant_credits(user_id, -abs(int(amount)), reason, "", ref)


def credits_summary(user_id: int) -> dict:
    _ensure()
    now = utcnow()
    soon = _expires_at(3, now)  # 72 小时提醒窗口
    with connect() as conn:
        user = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
        rows = conn.execute(
            "SELECT delta, reason FROM credit_ledger WHERE user_id = ?", (user_id,)
        ).fetchall()
        batch = conn.execute(
            "SELECT COALESCE(SUM(remaining), 0) AS soon_total FROM credit_batches"
            " WHERE user_id = ? AND remaining > 0 AND expired_at = '' AND expires_at <= ?",
            (int(user_id), soon),
        ).fetchone()
        upcoming = conn.execute(
            "SELECT MIN(expires_at) AS next_expiry FROM credit_batches"
            " WHERE user_id = ? AND remaining > 0 AND expired_at = ''",
            (int(user_id),),
        ).fetchone()
        live = conn.execute(
            "SELECT COALESCE(SUM(remaining), 0) AS n FROM credit_batches"
            " WHERE user_id = ? AND remaining > 0 AND expired_at = ''",
            (int(user_id),),
        ).fetchone()
    balance = int(user["credits"]) if user is not None else 0
    expired = sum(-int(item["delta"]) for item in rows
                  if int(item["delta"]) < 0 and item["reason"] == EXPIRE_REASON)
    used = sum(-int(item["delta"]) for item in rows
               if int(item["delta"]) < 0 and item["reason"] != EXPIRE_REASON)
    granted = sum(int(item["delta"]) for item in rows if int(item["delta"]) > 0)
    # 以“累计获得”为分母计算剩余比例：管理员扣减与聊天消耗都会降低剩余比例，
    # 同时兼容历史数据（无流水但账户有余额）的情况。
    total = max(granted, used + expired + balance)
    remaining = (balance / total) if total > 0 else 0.0
    ratio = (1 - remaining) if total > 0 else 0.0
    return {
        "balance": balance,
        "used": used,
        "granted": granted,
        "expired": expired,
        "ratio": round(ratio, 4),
        "remaining_ratio": round(remaining, 4),
        "expiring_soon": int(batch["soon_total"] or 0),
        "next_expiry": upcoming["next_expiry"] or "",
        "batch_balance": int(live["n"] or 0),
    }


def expire_credit_batches(now: str | None = None) -> dict:
    """清零所有已到期的批次剩余额度，并写入到期流水。

    以批次为准回收 ``users.credits``；任何异常都跳过该批次并记录日志，绝不产生负余额。
    """
    _ensure()
    moment = now or utcnow()
    expired_batches = 0
    expired_credits = 0
    skipped = 0
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, remaining FROM credit_batches"
            " WHERE remaining > 0 AND expired_at = '' AND expires_at <= ? ORDER BY expires_at, id",
            (moment,),
        ).fetchall()
        for row in rows:
            remaining = int(row["remaining"])
            user_id = int(row["user_id"])
            cursor = conn.execute(
                "UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?",
                (remaining, user_id, remaining),
            )
            if cursor.rowcount != 1:
                skipped += 1
                log.warning(
                    "credit batch mismatch",
                    extra={"event": "credit.batch.mismatch", "user_id": user_id,
                           "batch_id": int(row["id"]), "remaining": remaining},
                )
                continue
            conn.execute(
                "UPDATE credit_batches SET remaining = 0, expired_at = ? WHERE id = ?",
                (moment, int(row["id"])),
            )
            balance = int(
                conn.execute(
                    "SELECT credits FROM users WHERE id = ?", (user_id,)
                ).fetchone()["credits"]
            )
            conn.execute(
                "INSERT INTO credit_ledger (user_id, delta, balance_after, reason, actor,"
                " ref, created_at) VALUES (?, ?, ?, ?, 'system', ?, ?)",
                (user_id, -remaining, balance, EXPIRE_REASON,
                 f"batch:{int(row['id'])}", moment),
            )
            expired_batches += 1
            expired_credits += remaining
    return {"expired_batches": expired_batches, "expired_credits": expired_credits,
            "skipped": skipped}


def remind_expiring_batches(now: str | None = None, window_hours: int = 72,
                            notify=None) -> dict:
    """对 72 小时内将到期且尚未提醒的批次发送一次站内提醒。"""
    _ensure()
    moment = now or utcnow()
    limit = _expires_at(0, moment)
    base = datetime.fromisoformat(moment)
    until = (base + timedelta(hours=int(window_hours))).isoformat()
    sent = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, remaining, expires_at FROM credit_batches"
            " WHERE remaining > 0 AND expired_at = '' AND reminded_at = ''"
            " AND expires_at > ? AND expires_at <= ? ORDER BY expires_at, id",
            (limit, until),
        ).fetchall()
    sender = notify or add_alert
    for row in rows:
        message = (
            f"你有 {int(row['remaining'])} 积分将于 {str(row['expires_at'])[:10]} 到期，"
            "到期未使用的部分会自动清零。"
        )
        try:
            sender(int(row["user_id"]), "credit_expiry", message)
        except Exception as error:  # noqa: BLE001 - 单条提醒失败不影响其他批次
            log.warning(
                "credit expiry remind failed",
                extra={"event": "credit.expire.remind_error", "user_id": int(row["user_id"]),
                       "batch_id": int(row["id"]), "error": str(error)},
            )
            continue
        with _lock, connect() as conn:
            conn.execute(
                "UPDATE credit_batches SET reminded_at = ? WHERE id = ? AND reminded_at = ''",
                (moment, int(row["id"])),
            )
        sent += 1
    return {"sent": sent, "due": len(rows)}


def list_credit_batches(user_id: int, limit: int = 20, live_only: bool = False) -> list[dict]:
    _ensure()
    query = "SELECT * FROM credit_batches WHERE user_id = ?"
    if live_only:
        query += " AND remaining > 0 AND expired_at = ''"
    query += " ORDER BY expires_at, id LIMIT ?"
    with connect() as conn:
        rows = conn.execute(query, (int(user_id), max(int(limit), 1))).fetchall()
    return [dict(row) for row in rows]


def list_credit_ledger(user_id: int, limit: int = 20) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, delta, balance_after, reason, actor, ref, created_at"
            " FROM credit_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def list_credit_packages(active_only: bool = True) -> list[dict]:
    _ensure()
    query = "SELECT * FROM credit_packages"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY sort, id"
    with connect() as conn:
        rows = conn.execute(query).fetchall()
    return [dict(row) for row in rows]


def get_credit_package(package_id: int) -> dict | None:
    _ensure()
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM credit_packages WHERE id = ?", (package_id,)
        ).fetchone())


def upsert_credit_package(
    package_id: int | None,
    name: str,
    credits: int,
    price_cents: int,
    badge: str,
    sort: int,
    active: bool,
    coins: int = 0,
    validity_days: int | None = None,
) -> dict:
    _ensure()
    now = utcnow()
    credits = max(int(credits), 0)
    days = normalize_validity_days(validity_days) if validity_days is not None else None
    with _lock, connect() as conn:
        if package_id:
            if days is None:
                conn.execute(
                    "UPDATE credit_packages SET name = ?, credits = ?, coins = ?, price_cents = ?,"
                    " badge = ?, sort = ?, active = ?, updated_at = ? WHERE id = ?",
                    (name, credits, max(int(coins), 0), int(price_cents), badge, int(sort),
                     1 if active else 0, now, int(package_id)),
                )
            else:
                conn.execute(
                    "UPDATE credit_packages SET name = ?, credits = ?, coins = ?, price_cents = ?,"
                    " badge = ?, sort = ?, active = ?, validity_days = ?, updated_at = ?"
                    " WHERE id = ?",
                    (name, credits, max(int(coins), 0), int(price_cents), badge, int(sort),
                     1 if active else 0, days, now, int(package_id)),
                )
            if conn.execute(
                "SELECT id FROM credit_packages WHERE id = ?", (int(package_id),)
            ).fetchone() is None:
                raise KeyError("套餐不存在")
            row_id = int(package_id)
        else:
            cursor = conn.execute(
                "INSERT INTO credit_packages (name, credits, coins, price_cents, badge, sort,"
                " active, validity_days, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, credits, max(int(coins), 0), int(price_cents), badge, int(sort),
                 1 if active else 0, days if days is not None else DEFAULT_CREDIT_DAYS, now, now),
            )
            row_id = int(cursor.lastrowid)
    return get_credit_package(row_id) or {}


def credits_totals() -> dict:
    _ensure()
    with connect() as conn:
        held = conn.execute("SELECT COALESCE(SUM(credits), 0) AS n FROM users").fetchone()
        used = conn.execute(
            "SELECT COALESCE(SUM(-delta), 0) AS n FROM credit_ledger"
            " WHERE delta < 0 AND reason != ?", (EXPIRE_REASON,)
        ).fetchone()
        granted = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS n FROM credit_ledger WHERE delta > 0"
        ).fetchone()
        expired = conn.execute(
            "SELECT COALESCE(SUM(-delta), 0) AS n FROM credit_ledger"
            " WHERE delta < 0 AND reason = ?", (EXPIRE_REASON,)
        ).fetchone()
    return {"held": int(held["n"]), "used": int(used["n"]), "granted": int(granted["n"]),
            "expired": int(expired["n"])}


# --------------------------------------------------------------------------- #
# 念念币 / 兑换码
# --------------------------------------------------------------------------- #

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_code() -> str:
    group = lambda: "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))  # noqa: E731
    return "NIAN-" + "-".join(group() for _ in range(3))


def get_coins(user_id: int) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row["coins"]) if row is not None else 0


def grant_coins(
    user_id: int,
    delta: int,
    reason: str = "",
    actor: str = "",
    ref: str = "",
    idem: str = "",
) -> dict:
    """在单事务内调整用户念念币并写入流水，余额不足时抛出 InsufficientCoins。

    ``idem`` 非空时启用幂等：同一 (user_id, scope="coins.grant", idem) 只会
    生效一次，重复调用返回当前余额且不产生新的流水。
    """
    _ensure()
    delta = int(delta)
    with _lock, connect() as conn:
        row = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError("用户不存在")
        if idem and not _claim_idempotency(conn, user_id, "coins.grant", idem):
            return {"balance": int(row["coins"]), "delta": 0, "duplicate": True}
        cursor = conn.execute(
            "UPDATE users SET coins = coins + ? WHERE id = ? AND coins + ? >= 0",
            (delta, user_id, delta),
        )
        if cursor.rowcount != 1:
            raise InsufficientCoins(int(row["coins"]))
        new_balance = int(
            conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()["coins"]
        )
        if delta:
            conn.execute(
                "INSERT INTO coin_ledger (user_id, delta, balance_after, reason, actor,"
                " ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, delta, new_balance, reason, actor, ref, utcnow()),
            )
    return {"balance": new_balance, "delta": delta, "duplicate": False}


def coins_summary(user_id: int) -> dict:
    _ensure()
    with connect() as conn:
        user = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
        rows = conn.execute(
            "SELECT delta FROM coin_ledger WHERE user_id = ?", (user_id,)
        ).fetchall()
    balance = int(user["coins"]) if user is not None else 0
    used = sum(-int(item["delta"]) for item in rows if int(item["delta"]) < 0)
    granted = sum(int(item["delta"]) for item in rows if int(item["delta"]) > 0)
    return {"balance": balance, "used": used, "granted": granted}


def list_coin_ledger(user_id: int, limit: int = 20) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, delta, balance_after, reason, actor, ref, created_at"
            " FROM coin_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def coins_totals() -> dict:
    _ensure()
    with connect() as conn:
        held = conn.execute("SELECT COALESCE(SUM(coins), 0) AS n FROM users").fetchone()
        granted = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS n FROM coin_ledger WHERE delta > 0"
        ).fetchone()
        spent = conn.execute(
            "SELECT COALESCE(SUM(-delta), 0) AS n FROM coin_ledger WHERE delta < 0"
        ).fetchone()
    return {"held": int(held["n"]), "granted": int(granted["n"]), "spent": int(spent["n"])}


# --------------------------------------------------------------------------- #
# 蒸馏券
# --------------------------------------------------------------------------- #

TICKET_BUY_REASON = "蒸馏券购买"
TICKET_RESERVE_REASON = "蒸馏预扣"
TICKET_REFUND_REASON = "蒸馏失败退还"


def distill_ticket_price() -> int:
    """单张蒸馏券的念念币价格（后台可配，默认 60）。"""
    from . import config as _config
    return max(int(_config.load_platform_config().distill_ticket_price), 0)


def get_distill_tickets(user_id: int) -> int:
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
    return int(row["distill_tickets"]) if row is not None else 0


def grant_distill_tickets(
    user_id: int,
    delta: int,
    reason: str = "",
    actor: str = "",
    ref: str = "",
    idem: str = "",
) -> dict:
    """在单事务内调整用户蒸馏券并写入流水，余额不足时抛出 InsufficientDistillTickets。"""
    _ensure()
    delta = int(delta)
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            raise KeyError("用户不存在")
        if idem and not _claim_idempotency(conn, int(user_id), "distill.grant", idem):
            return {"balance": int(row["distill_tickets"]), "delta": 0, "duplicate": True}
        cursor = conn.execute(
            "UPDATE users SET distill_tickets = distill_tickets + ?"
            " WHERE id = ? AND distill_tickets + ? >= 0",
            (delta, int(user_id), delta),
        )
        if cursor.rowcount != 1:
            raise InsufficientDistillTickets(int(row["distill_tickets"]))
        new_balance = int(
            conn.execute(
                "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()["distill_tickets"]
        )
        if delta:
            conn.execute(
                "INSERT INTO distill_ticket_ledger (user_id, delta, balance_after, reason,"
                " actor, ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(user_id), delta, new_balance, reason, actor, ref, utcnow()),
            )
    return {"balance": new_balance, "delta": delta, "duplicate": False}


def purchase_distill_ticket(user_id: int, quantity: int = 1, idem: str = "") -> dict:
    """用念念币购买蒸馏券（单事务扣币 + 发券 + 双流水）。"""
    _ensure()
    quantity = max(int(quantity), 1)
    price = distill_ticket_price()
    total = price * quantity
    with _lock, connect() as conn:
        user = conn.execute(
            "SELECT coins, distill_tickets FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if user is None:
            raise KeyError("用户不存在")
        if idem and not _claim_idempotency(
            conn, int(user_id), "distill.purchase", idem
        ):
            return {"coins": int(user["coins"]), "tickets": int(user["distill_tickets"]),
                    "quantity": 0, "spent": 0, "duplicate": True}
        if total > 0:
            cursor = conn.execute(
                "UPDATE users SET coins = coins - ?, distill_tickets = distill_tickets + ?"
                " WHERE id = ? AND coins >= ?",
                (total, quantity, int(user_id), total),
            )
        else:
            cursor = conn.execute(
                "UPDATE users SET distill_tickets = distill_tickets + ? WHERE id = ?",
                (quantity, int(user_id)),
            )
        if cursor.rowcount != 1:
            raise InsufficientCoins(int(user["coins"]))
        fresh = conn.execute(
            "SELECT coins, distill_tickets FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        new_coins = int(fresh["coins"])
        new_tickets = int(fresh["distill_tickets"])
        if total > 0:
            conn.execute(
                "INSERT INTO coin_ledger (user_id, delta, balance_after, reason, actor,"
                " ref, created_at) VALUES (?, ?, ?, ?, '', 'distill-ticket', ?)",
                (int(user_id), -total, new_coins, f"购买蒸馏券 x{quantity}", utcnow()),
            )
        conn.execute(
            "INSERT INTO distill_ticket_ledger (user_id, delta, balance_after, reason,"
            " actor, ref, created_at) VALUES (?, ?, ?, ?, '', 'distill-ticket', ?)",
            (int(user_id), quantity, new_tickets, TICKET_BUY_REASON, utcnow()),
        )
    return {"coins": new_coins, "tickets": new_tickets, "quantity": quantity,
            "spent": total, "duplicate": False}


def reserve_distill_ticket(user_id: int, task_id: str) -> dict:
    """发起蒸馏时预扣 1 张蒸馏券；无券抛出 InsufficientDistillTickets。"""
    _ensure()
    ref = f"task:{str(task_id)}"
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            raise KeyError("用户不存在")
        cursor = conn.execute(
            "UPDATE users SET distill_tickets = distill_tickets - 1"
            " WHERE id = ? AND distill_tickets > 0",
            (int(user_id),),
        )
        if cursor.rowcount != 1:
            raise InsufficientDistillTickets(int(row["distill_tickets"]))
        new_balance = int(
            conn.execute(
                "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()["distill_tickets"]
        )
        conn.execute(
            "INSERT INTO distill_ticket_ledger (user_id, delta, balance_after, reason,"
            " actor, ref, created_at) VALUES (?, -1, ?, ?, 'system', ?, ?)",
            (int(user_id), new_balance, TICKET_RESERVE_REASON, ref, utcnow()),
        )
    return {"balance": new_balance, "reserved": 1}


def _outstanding_reservation(conn: sqlite3.Connection, user_id: int, task_id: str) -> bool:
    ref = f"task:{str(task_id)}"
    row = conn.execute(
        "SELECT"
        " (SELECT COUNT(*) FROM distill_ticket_ledger WHERE user_id = ? AND ref = ?"
        "   AND reason = ?) AS reserved,"
        " (SELECT COUNT(*) FROM distill_ticket_ledger WHERE user_id = ? AND ref = ?"
        "   AND delta > 0) AS refunded",
        (int(user_id), ref, TICKET_RESERVE_REASON, int(user_id), ref),
    ).fetchone()
    return int(row["reserved"]) > int(row["refunded"])


def refund_distill_ticket(user_id: int, task_id: str,
                          reason: str = TICKET_REFUND_REASON) -> dict:
    """蒸馏失败/取消时退还预扣的蒸馏券；同一任务最多退一次。"""
    _ensure()
    with _lock, connect() as conn:
        if not _outstanding_reservation(conn, int(user_id), str(task_id)):
            row = conn.execute(
                "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()
            return {"balance": int(row["distill_tickets"]) if row else 0,
                    "delta": 0, "duplicate": True}
        if not _claim_idempotency(conn, int(user_id), "distill.refund", str(task_id)):
            row = conn.execute(
                "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()
            return {"balance": int(row["distill_tickets"]) if row else 0,
                    "delta": 0, "duplicate": True}
        conn.execute(
            "UPDATE users SET distill_tickets = distill_tickets + 1 WHERE id = ?",
            (int(user_id),),
        )
        new_balance = int(
            conn.execute(
                "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()["distill_tickets"]
        )
        conn.execute(
            "INSERT INTO distill_ticket_ledger (user_id, delta, balance_after, reason,"
            " actor, ref, created_at) VALUES (?, 1, ?, ?, 'system', ?, ?)",
            (int(user_id), new_balance, reason, f"task:{str(task_id)}", utcnow()),
        )
    return {"balance": new_balance, "delta": 1, "duplicate": False}


def distill_tickets_summary(user_id: int) -> dict:
    _ensure()
    with connect() as conn:
        user = conn.execute(
            "SELECT distill_tickets FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        rows = conn.execute(
            "SELECT delta, reason FROM distill_ticket_ledger WHERE user_id = ?",
            (int(user_id),),
        ).fetchall()
    balance = int(user["distill_tickets"]) if user is not None else 0
    purchased = sum(int(item["delta"]) for item in rows
                    if int(item["delta"]) > 0 and item["reason"] == TICKET_BUY_REASON)
    reserved = sum(1 for item in rows if item["reason"] == TICKET_RESERVE_REASON)
    refunded = sum(1 for item in rows if item["reason"] == TICKET_REFUND_REASON)
    return {"balance": balance, "purchased": purchased,
            "consumed": max(reserved - refunded, 0), "price": distill_ticket_price()}


def list_distill_ticket_ledger(user_id: int, limit: int = 20) -> list[dict]:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, delta, balance_after, reason, actor, ref, created_at"
            " FROM distill_ticket_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (int(user_id), max(int(limit), 1)),
        ).fetchall()
    return [dict(row) for row in rows]


def reconcile_distill_tickets() -> dict:
    """启动对账：为中断的蒸馏任务退还预扣券，避免券被永久占用。"""
    _ensure()
    refunded = 0
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id FROM tasks"
            " WHERE kind IN ('distill', 'redistill')"
            " AND status IN ('queued', 'pending', 'running')"
        ).fetchall()
        for row in rows:
            user_id = int(row["user_id"])
            task_id = str(row["id"])
            if not _outstanding_reservation(conn, user_id, task_id):
                continue
            if not _claim_idempotency(conn, user_id, "distill.refund", task_id):
                continue
            conn.execute(
                "UPDATE users SET distill_tickets = distill_tickets + 1 WHERE id = ?",
                (user_id,),
            )
            new_balance = int(
                conn.execute(
                    "SELECT distill_tickets FROM users WHERE id = ?", (user_id,)
                ).fetchone()["distill_tickets"]
            )
            now = utcnow()
            conn.execute(
                "INSERT INTO distill_ticket_ledger (user_id, delta, balance_after, reason,"
                " actor, ref, created_at) VALUES (?, 1, ?, ?, 'system', ?, ?)",
                (user_id, new_balance, "蒸馏中断退还", f"task:{task_id}", now),
            )
            conn.execute(
                "UPDATE tasks SET status = 'error', error = ?, error_kind = 'interrupted',"
                " updated_at = ? WHERE id = ?",
                ("服务重启导致蒸馏中断，蒸馏券已退还", now, task_id),
            )
            refunded += 1
    return {"refunded": refunded}


def purchase_package(user_id: int, package_id: int, idem: str = "") -> dict:
    """用念念币购买套餐：扣念念币并发放聊天额度，单事务完成。

    ``idem`` 非空时启用幂等：同一 (user_id, 套餐, idem) 只会扣费/发放一次，
    重复调用返回当前余额，避免网络重试或重复点击造成双扣。
    """
    _ensure()
    now = utcnow()
    with _lock, connect() as conn:
        package = conn.execute(
            "SELECT * FROM credit_packages WHERE id = ? AND active = 1", (int(package_id),)
        ).fetchone()
        if package is None:
            raise KeyError("套餐不存在或已下架")
        user = conn.execute("SELECT coins, credits FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise KeyError("用户不存在")
        if idem and not _claim_idempotency(
            conn, user_id, "package.purchase", f"{int(package_id)}:{idem}"
        ):
            return {
                "coins": int(user["coins"]),
                "credits": int(user["credits"]),
                "package": dict(package),
                "spent": 0,
                "duplicate": True,
            }
        price = max(int(package["coins"]), 0)
        gain = max(int(package["credits"]), 0)
        days = normalize_validity_days(package["validity_days"])
        cursor = conn.execute(
            "UPDATE users SET coins = coins - ?, credits = credits + ?"
            " WHERE id = ? AND coins >= ?",
            (price, gain, user_id, price),
        )
        if cursor.rowcount != 1:
            raise InsufficientCoins(int(user["coins"]))
        fresh = conn.execute(
            "SELECT coins, credits FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        new_coins = int(fresh["coins"])
        new_credits = int(fresh["credits"])
        if gain:
            conn.execute(
                "INSERT INTO credit_batches (user_id, amount, remaining, source, package_id,"
                " reason, actor, ref, expires_at, created_at) VALUES (?, ?, ?, 'package', ?,"
                " ?, '', ?, ?, ?)",
                (user_id, gain, gain, int(package_id), f"套餐到账：{package['name']}",
                 f"package:{package_id}", _expires_at(days, now), now),
            )
        if price:
            conn.execute(
                "INSERT INTO coin_ledger (user_id, delta, balance_after, reason, actor,"
                " ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, -price, new_coins, f"购买套餐：{package['name']}", "", f"package:{package_id}", now),
            )
        conn.execute(
            "INSERT INTO credit_ledger (user_id, delta, balance_after, reason, actor,"
            " ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, gain, new_credits, f"套餐到账：{package['name']}", "", f"package:{package_id}", now),
        )
    return {
        "coins": new_coins,
        "credits": new_credits,
        "package": dict(package),
        "spent": price,
        "duplicate": False,
    }


def create_redemption_codes(
    count: int,
    coins: int,
    batch: str = "",
    note: str = "",
    actor: str = "",
) -> list[dict]:
    """批量生成兑换码，兑换码是念念币的发放凭证。"""
    _ensure()
    count = max(1, min(int(count), 200))
    coins = max(int(coins), 1)
    now = utcnow()
    created: list[dict] = []
    with _lock, connect() as conn:
        existing = {row["code"] for row in conn.execute("SELECT code FROM redemption_codes")}
        for _ in range(count):
            code = _new_code()
            while code in existing:
                code = _new_code()
            existing.add(code)
            cursor = conn.execute(
                "INSERT INTO redemption_codes (code, coins, batch, note, status, created_by,"
                " created_at) VALUES (?, ?, ?, ?, 'unused', ?, ?)",
                (code, coins, batch, note, actor, now),
            )
            created.append({"id": int(cursor.lastrowid), "code": code, "coins": coins,
                            "batch": batch, "status": "unused"})
    return created


def list_redemption_codes(limit: int = 200, status: str = "") -> list[dict]:
    _ensure()
    query = (
        "SELECT c.id, c.code, c.coins, c.batch, c.note, c.status, c.created_by,"
        " c.created_at, c.redeemed_by, c.redeemed_at, u.username AS redeemed_username"
        " FROM redemption_codes c LEFT JOIN users u ON u.id = c.redeemed_by"
    )
    params: list = []
    if status:
        query += " WHERE c.status = ?"
        params.append(status)
    query += " ORDER BY c.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def redemption_stats() -> dict:
    _ensure()
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(coins), 0) AS coins"
            " FROM redemption_codes GROUP BY status"
        ).fetchall()
    stats = {"unused": 0, "used": 0, "unused_coins": 0, "used_coins": 0}
    for row in rows:
        status = row["status"]
        stats[status] = int(row["n"])
        stats[status + "_coins"] = int(row["coins"])
    return stats


def redeem_code(user_id: int, code: str) -> dict:
    """用户兑换：校验兑换码并直接发放念念币。"""
    _ensure()
    normalized = (code or "").strip().upper()
    if not normalized:
        raise RedemptionError("请输入兑换码")
    now = utcnow()
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT * FROM redemption_codes WHERE code = ?", (normalized,)
        ).fetchone()
        if row is None:
            raise RedemptionError("兑换码无效")
        if row["status"] != "unused":
            raise RedemptionError("兑换码已被使用")
        user = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise KeyError("用户不存在")
        amount = int(row["coins"])
        # 条件更新保证兑换码只能被消费一次（并发下第二个请求 rowcount 为 0）。
        cursor = conn.execute(
            "UPDATE redemption_codes SET status = 'used', redeemed_by = ?, redeemed_at = ?"
            " WHERE id = ? AND status = 'unused'",
            (user_id, now, row["id"]),
        )
        if cursor.rowcount != 1:
            raise RedemptionError("兑换码已被使用")
        conn.execute("UPDATE users SET coins = coins + ? WHERE id = ?", (amount, user_id))
        new_coins = int(
            conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()["coins"]
        )
        conn.execute(
            "INSERT INTO coin_ledger (user_id, delta, balance_after, reason, actor,"
            " ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, amount, new_coins, "兑换码兑换", "", normalized, now),
        )
    return {"coins": new_coins, "amount": amount, "code": normalized}

