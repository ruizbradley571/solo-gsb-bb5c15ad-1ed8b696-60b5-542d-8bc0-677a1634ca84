"""时间工具：所有时间统一规范化为 UTC ISO-8601 字符串存储。

固定微秒精度（timespec="microseconds"）保证字符串等宽，
SQLite 中直接按字典序比较即等价于时间先后比较。
"""

from datetime import datetime, timezone


def to_utc(dt: datetime) -> datetime:
    """将 datetime 归一到 UTC；naive 时间按 UTC 解释。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """归一化为等宽 UTC ISO 字符串，可安全做字典序比较。"""
    return to_utc(dt).isoformat(timespec="microseconds")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def parse(s: str) -> datetime:
    """解析 iso() 产出的等宽 UTC ISO 字符串。"""
    return datetime.fromisoformat(s)
