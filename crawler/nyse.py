"""美股（NYSE）交易时段，按纽约时间算，自动处理夏令时。

盘前 04:00-09:30，盘中 09:30-16:00，盘后 16:00-20:00；其余时间、周末、全天休市的节假日 = 休市。
提前收盘日（感恩节次日、平安夜等）盘中到 13:00，盘后到 17:00。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # Windows 自建 runner 没装 tzdata 时退回手算夏令时
    NY = None

STATES = {"pre": "盘前", "regular": "盘中", "post": "盘后", "closed": "休市"}

# NYSE 官方公布的全天休市日和提前收盘日（2025-12-23 公告，覆盖 2026-2028）。
# 过期没更新也不会出错，只是节假日会被当成普通交易日。
HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04",
    "2028-11-23", "2028-12-25",
))
EARLY_CLOSE = frozenset(date.fromisoformat(d) for d in (
    "2026-11-27", "2026-12-24", "2027-11-26", "2028-07-03", "2028-11-24",
))


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7, weeks=n - 1)


def _us_dst(utc: datetime) -> bool:
    """美国夏令时：3 月第 2 个周日 2:00 到 11 月第 1 个周日 2:00（当地时间）。"""
    y = utc.year
    start = datetime.combine(_nth_sunday(y, 3, 2), time(7), timezone.utc)  # 2:00 EST = 7:00 UTC
    end = datetime.combine(_nth_sunday(y, 11, 1), time(6), timezone.utc)  # 2:00 EDT = 6:00 UTC
    return start <= utc < end


def new_york(at: datetime) -> datetime:
    """任意带时区的时间 → 纽约当地时间。"""
    if NY is not None:
        return at.astimezone(NY)
    utc = at.astimezone(timezone.utc)
    return (utc + timedelta(hours=-4 if _us_dst(utc) else -5)).replace(tzinfo=None)


def market_state(at: datetime) -> str:
    """'pre' / 'regular' / 'post' / 'closed'。"""
    t = new_york(at)
    d = t.date()
    if t.weekday() >= 5 or d in HOLIDAYS:
        return "closed"
    close, late = (time(13), time(17)) if d in EARLY_CLOSE else (time(16), time(20))
    now = t.time()
    if time(4) <= now < time(9, 30):
        return "pre"
    if time(9, 30) <= now < close:
        return "regular"
    if close <= now < late:
        return "post"
    return "closed"
