"""纯 Python 技术指标。

约定：输入是数值列表（或 Candle 列表），输出与输入等长，数据不够的位置填 None。
实盘和回测都把同样长度的窗口喂进来，所以 EMA/RSI 这类有“记忆”的指标两边结果一致。
"""
from __future__ import annotations

import math
from collections import deque
from typing import Sequence

from .candles import Candle

Series = list  # list[float | None]


def _check(n: int) -> None:
    if n < 1:
        raise ValueError(f"周期必须 >= 1，收到 {n}")


def sma(values: Sequence[float], n: int) -> Series:
    _check(n)
    out: Series = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    """指数均线，用前 n 个值的 SMA 做种子。"""
    _check(n)
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(values: Sequence[float], n: int = 14) -> Series:
    """Wilder RSI。"""
    _check(n)
    out: Series = [None] * len(values)
    if len(values) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    ag, al = gain / n, loss / n
    out[n] = _rsi_from(ag, al)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = _rsi_from(ag, al)
    return out


def true_range(candles: Sequence[Candle]) -> list[float]:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(candles: Sequence[Candle], n: int = 14) -> Series:
    """Wilder ATR，用前 n 根 TR 的均值做种子。"""
    _check(n)
    tr = true_range(candles)
    out: Series = [None] * len(tr)
    if len(tr) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def _rolling_extreme(values: Sequence[float], n: int, better) -> Series:
    _check(n)
    out: Series = [None] * len(values)
    q: deque[int] = deque()
    for i, v in enumerate(values):
        while q and not better(values[q[-1]], v):
            q.pop()
        q.append(i)
        if q[0] <= i - n:
            q.popleft()
        if i >= n - 1:
            out[i] = values[q[0]]
    return out


def highest(values: Sequence[float], n: int) -> Series:
    """含当前值在内的最近 n 个值的最大值。"""
    return _rolling_extreme(values, n, lambda kept, new: kept > new)


def lowest(values: Sequence[float], n: int) -> Series:
    return _rolling_extreme(values, n, lambda kept, new: kept < new)


def stdev(values: Sequence[float], n: int) -> Series:
    """滚动总体标准差。"""
    _check(n)
    out: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1 : i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(max(sum((x - m) ** 2 for x in w) / n, 0.0))
    return out


def bollinger(values: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[Series, Series, Series]:
    """布林带：(中轨, 上轨, 下轨)。"""
    mid = sma(values, n)
    sd = stdev(values, n)
    up = [m + k * s if m is not None else None for m, s in zip(mid, sd)]
    lo = [m - k * s if m is not None else None for m, s in zip(mid, sd)]
    return mid, up, lo


def roc(values: Sequence[float], n: int) -> Series:
    """n 期涨跌幅：values[i] / values[i-n] - 1。"""
    _check(n)
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        if values[i - n]:
            out[i] = values[i] / values[i - n] - 1.0
    return out


def closes(candles: Sequence[Candle]) -> list[float]:
    return [c.close for c in candles]
