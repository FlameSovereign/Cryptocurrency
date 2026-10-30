"""测试工具：造 K 线、假交易所。"""
from __future__ import annotations

import math

from bot.candles import Candle, timeframe_ms

T0 = 1_700_000_000_000 - 1_700_000_000_000 % 86_400_000  # 某个 UTC 0 点


def make_candles(closes, tf: str = "1h", t0: int = T0, spread: float = 0.002) -> list[Candle]:
    step = timeframe_ms(tf)
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) * (1 + spread)
        lo = min(o, c) * (1 - spread)
        out.append(Candle(t0 + i * step, float(o), float(hi), float(lo), float(c), 1.0))
        prev = c
    return out


def wave(n: int, base: float = 100.0, amp: float = 20.0, period: int = 50, drift: float = 0.0) -> list[float]:
    return [base * (1 + drift) ** i + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


class FakeExchange:
    """够 run.py / brokers 用的最小 ccxt 替身。"""

    id = "gate"

    def __init__(self, candles, *, last=None, min_cost=3.0):
        self.candles = candles
        self.last = last if last is not None else candles[-1].close
        self.markets = {"BTC/USDT": self._market(min_cost)}

    @staticmethod
    def _market(min_cost):
        return {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "spot": True,
                "limits": {"cost": {"min": min_cost}, "amount": {"min": 1e-6}}, "precision": {"amount": 1e-6}}

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        rows = [list(c) for c in self.candles if since is None or c.ts >= since]
        return rows[:limit]

    def fetch_ticker(self, symbol):
        return {"last": self.last, "close": self.last}
