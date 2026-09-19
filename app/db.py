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

-- 结算期费率锁（v1.5 起）：锁定窗口 [valid_from, valid_to) 必须在 known_at 认知时点
-- 全部 covered，按覆盖率切分得到的每个连续片段落一行，并记录该片段当时唯一可见的
-- 费率版本（basis_rate_id）。只追加、不修改、不删除；存在 gap/conflict 时整批不落库。
-- POST /rates 的拟议区间与任一锁定片段相交即被拒绝（RATE_WINDOW_LOCKED）。
CREATE TABLE IF NOT EXISTS rate_locks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no   TEXT NOT NULL,          -- 合同号
    valid_from    TEXT NOT NULL,          -- 锁定片段起点（含），UTC ISO 字符串
    valid_to      TEXT NOT NULL,          -- 锁定片段终点（不含），UTC ISO 字符串
    known_at      TEXT NOT NULL,          -- 锁定所依据的认知时点
    basis_rate_id INTEGER NOT NULL,       -- 该片段在 known_at 时点唯一可见的费率版本
    reason        TEXT,                   -- 锁定原因
    created_at    TEXT NOT NULL,          -- 锁记录写入时间（审计用）
    FOREIGN KEY (basis_rate_id) REFERENCES rate_versions (id)
);
CREATE INDEX IF NOT EXISTS idx_rate_locks_contract
    ON rate_locks (contract_no, valid_from, valid_to);
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
