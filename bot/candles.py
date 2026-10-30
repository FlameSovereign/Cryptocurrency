"""K 线结构和周期换算。"""
from __future__ import annotations

import re
import time
from typing import Iterable, NamedTuple, Sequence


class Candle(NamedTuple):
    ts: int  # 开盘时间，UTC 毫秒
    open: float
    high: float
    low: float
    close: float
    volume: float


_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
YEAR_MS = int(365.25 * 86_400_000)


def timeframe_ms(tf: str) -> int:
    """'4h' -> 14_400_000。"""
    m = re.fullmatch(r"(\d+)([mhdw])", tf.strip())
    if not m:
        raise ValueError(f"看不懂的周期 {tf!r}，请用 15m / 1h / 4h / 1d 这种写法")
    return int(m.group(1)) * _UNIT_MS[m.group(2)]


def now_ms() -> int:
    return int(time.time() * 1000)


def from_ccxt(rows: Iterable[Sequence]) -> list[Candle]:
    """ccxt 的 [ts, o, h, l, c, v] 列表 -> Candle，顺手排序去重。"""
    out = {}
    for r in rows:
        if r is None or r[4] is None:
            continue
        out[int(r[0])] = Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0.0))
    return [out[k] for k in sorted(out)]


def merge(*series: Sequence[Candle]) -> list[Candle]:
    """合并多段 K 线，同一时间戳以后出现的为准。"""
    out = {}
    for s in series:
        for c in s:
            out[c.ts] = c
    return [out[k] for k in sorted(out)]


def drop_unclosed(candles: Sequence[Candle], tf: str, at_ms: int | None = None) -> list[Candle]:
    """去掉还没收盘的 K 线。策略只看已收盘数据，否则信号会在一根 K 线内反复横跳。"""
    span = timeframe_ms(tf)
    t = now_ms() if at_ms is None else at_ms
    return [c for c in candles if c.ts + span <= t]
