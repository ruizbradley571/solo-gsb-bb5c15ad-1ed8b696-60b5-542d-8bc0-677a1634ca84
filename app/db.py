"""SQLite 连接与建表。数据库路径由环境变量 DATABASE_PATH 指定，默认 ./rates.db。"""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no   TEXT NOT NULL,          -- 合同号
    rate          REAL NOT NULL,          -- 费率值
    valid_from    TEXT NOT NULL,          -- 业务生效起点（含），UTC ISO 字符串
    valid_to      TEXT,                   -- 业务生效终点（不含），NULL 表示无限期
    recorded_at   TEXT NOT NULL,          -- 系统记录时间（认知时间起点，含）
    superseded_at TEXT,                   -- 被修订时间（认知时间终点，不含），NULL 表示当前认知
    version       INTEGER NOT NULL,       -- 同一合同内单调递增的版本号
    reason        TEXT                    -- 录入/修订说明
);
CREATE INDEX IF NOT EXISTS idx_rate_versions_contract ON rate_versions (contract_no);

-- 版本血缘边：方向为后继版本 -> 祖先版本（录入时在同一事务写入，只追加、不修改）。
-- supersedes：新费率 -> 本次修订被取代的每个旧版本；
-- retained_from：补偿片段 -> 切出该片段的来源旧版本。
-- 没有任何出边的版本（含功能上线前的历史行）视为血缘根。
CREATE TABLE IF NOT EXISTS rate_version_edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_rate_id INTEGER NOT NULL,        -- 后继版本（新费率或补偿片段）
    to_rate_id   INTEGER NOT NULL,        -- 祖先版本（被取代版本或补偿片段来源版本）
    relation     TEXT NOT NULL,           -- supersedes / retained_from
    recorded_at  TEXT NOT NULL,           -- 边建立时间（= 修订的 recorded_at，审计用）
    UNIQUE (from_rate_id, to_rate_id, relation),
    CHECK (relation IN ('supersedes', 'retained_from')),
    FOREIGN KEY (from_rate_id) REFERENCES rate_versions (id),
    FOREIGN KEY (to_rate_id) REFERENCES rate_versions (id)
);
CREATE INDEX IF NOT EXISTS idx_rate_version_edges_from ON rate_version_edges (from_rate_id);

-- 结算期费率锁的锁头：一次锁定请求的窗口、认知时点与原因，只追加、不修改、不删除。
CREATE TABLE IF NOT EXISTS rate_locks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no TEXT NOT NULL,          -- 合同号
    valid_from  TEXT NOT NULL,          -- 锁定窗口起点（含），UTC ISO 字符串
    valid_to    TEXT NOT NULL,          -- 锁定窗口终点（不含）
    known_at    TEXT NOT NULL,          -- 锁定判定使用的认知时点
    reason      TEXT,                   -- 锁定原因
    created_at  TEXT NOT NULL           -- 锁定落库时间（系统时间）
);
CREATE INDEX IF NOT EXISTS idx_rate_locks_contract ON rate_locks (contract_no);

-- 锁定片段：锁窗口按覆盖切分规则得到的连续 covered 片段及其 basis 版本，
-- 同一锁的片段连续铺满整个锁窗口；与锁头在同一事务写入。
CREATE TABLE IF NOT EXISTS rate_lock_segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lock_id    INTEGER NOT NULL,        -- 所属锁头
    valid_from TEXT NOT NULL,           -- 片段起点（含）
    valid_to   TEXT NOT NULL,           -- 片段终点（不含）
    rate_id    INTEGER NOT NULL,        -- 该片段锁定的依据费率版本
    FOREIGN KEY (lock_id) REFERENCES rate_locks (id),
    FOREIGN KEY (rate_id) REFERENCES rate_versions (id)
);
CREATE INDEX IF NOT EXISTS idx_rate_lock_segments_lock ON rate_lock_segments (lock_id);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", "rates.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """建表（幂等），应用启动时自动调用。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
