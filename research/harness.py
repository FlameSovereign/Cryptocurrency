"""策略研究台：同一套数据、同一套成本假设，把策略在一篮子标的 × 周期上跑一遍。

只用本地缓存（.cache/），不联网；缓存用 `python -m research.harness --download` 生成。

    python -m research.harness sma_cross                          # 默认参数，全篮子
    python -m research.harness rsi --params '{"low": 25}' --tf 4h
    python -m research.harness donchian --split 0.6               # 前 60% 样本内 / 后 40% 样本外
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass

from bot.backtest import Result, run_backtest
from bot.candles import Candle, now_ms
from bot.data import fetch_fear_greed, fetch_history
from bot.engine import Risk
from bot.strategies import FearGreed, get

EXCHANGE = "gate"
FEE = 0.002  # Gate 现货 VIP0；有 GT 抵扣 / VIP 会更低
CRYPTO = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "GT/USDT"]
STOCKS = ["SPYX/USDT", "NVDAX/USDT", "TSLAX/USDT", "QQQX/USDT"]  # xStocks 股票代币，2025-07 上线
UNIVERSE = CRYPTO + STOCKS
TIMEFRAMES = ["1h", "4h", "1d"]
# 按 2026-10 盘口实测（300 USDT 市价单冲击 + 一半价差）往保守取整
SLIPPAGE = {"GT/USDT": 0.002, **{s: 0.001 for s in STOCKS}}
DEFAULT_SLIPPAGE = 0.0005
BUDGET = 300.0
MIN_COST = 3.0


@dataclass
class Row:
    strategy: str
    symbol: str
    timeframe: str
    segment: str  # full / in / out
    r: Result

    def as_dict(self) -> dict:
        d = self.r.summary()
        d.update(segment=self.segment, beat_hodl=beats(self.r))
        return d


_cache: dict = {}


def beats(r: Result) -> bool:
    """跑赢囤币（留一点浮点误差余量，囤币自己不算赢自己）。"""
    return r.total_return > r.hodl_return + 1e-6


def candles(symbol: str, timeframe: str) -> list[Candle]:
    key = (symbol, timeframe)
    if key not in _cache:
        _cache[key] = fetch_history(EXCHANGE, symbol, timeframe, 0, offline=True)
    return _cache[key]


def fear_greed() -> FearGreed:
    if "fng" not in _cache:
        _cache["fng"] = fetch_fear_greed(0, offline=True)
    return _cache["fng"]


def backtest(strategy: str, symbol: str, timeframe: str, *, params=None, risk: Risk = Risk(), size: float = 1.0,
             fee: float = FEE, start_ms: int | None = None, end_ms: int | None = None) -> Result:
    strat = get(strategy)
    c = candles(symbol, timeframe)
    if end_ms is not None:
        c = [x for x in c if x.ts < end_ms]
    return run_backtest(strat, c, timeframe=timeframe, symbol=symbol, params=params, budget=BUDGET, size=size,
                        risk=risk, fee=fee, slippage=SLIPPAGE.get(symbol, DEFAULT_SLIPPAGE), min_cost=MIN_COST,
                        fear_greed=fear_greed() if "fear_greed" in strat.needs else None, start_ms=start_ms)


def split_ts(symbol: str, timeframe: str, frac: float) -> int:
    c = candles(symbol, timeframe)
    return c[int(len(c) * frac)].ts


def evaluate(strategy: str, *, params=None, risk: Risk = Risk(), size: float = 1.0, symbols=None, timeframes=None,
             split: float | None = None, fee: float = FEE) -> list[Row]:
    """split=None：全样本；split=0.6：分别给出样本内（前 60%）和样本外（后 40%）。"""
    rows = []
    for tf in timeframes or TIMEFRAMES:
        for sym in symbols or UNIVERSE:
            try:
                if split is None:
                    rows.append(Row(strategy, sym, tf, "full", backtest(strategy, sym, tf, params=params, risk=risk, size=size, fee=fee)))
                else:
                    cut = split_ts(sym, tf, split)
                    rows.append(Row(strategy, sym, tf, "in", backtest(strategy, sym, tf, params=params, risk=risk, size=size, fee=fee, end_ms=cut)))
                    rows.append(Row(strategy, sym, tf, "out", backtest(strategy, sym, tf, params=params, risk=risk, size=size, fee=fee, start_ms=cut)))
            except (ValueError, FileNotFoundError) as e:
                print(f"跳过 {strategy} {sym} {tf}: {e}", file=sys.stderr)
    return rows


def monkey_band(symbol: str, timeframe: str, seeds: int = 30, hold: int = 24, **kw) -> tuple[float, float]:
    """30 只猴子的收益中位数和 90 分位：一个策略连 p90 猴子都打不过，就别说自己有 alpha。"""
    rets = sorted(backtest("monkey", symbol, timeframe, params={"seed": s, "hold": hold}, **kw).total_return for s in range(seeds))
    return statistics.median(rets), rets[int(0.9 * (len(rets) - 1))]


def table(rows: list[Row]) -> str:
    out = ["| 策略 | 标的 | 周期 | 段 | 收益 | 囤币 | 最大回撤 | 囤币回撤 | 夏普 | 成交 | 胜率 | 持仓 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        r = x.r
        wr = "-" if r.win_rate is None else f"{r.win_rate:.0%}"
        out.append(f"| {x.strategy} | {x.symbol} | {x.timeframe} | {x.segment} | {r.total_return:+.1%} | {r.hodl_return:+.1%} "
                   f"| {r.max_drawdown:.0%} | {r.hodl_max_drawdown:.0%} | {r.sharpe:.2f} | {r.fills} | {wr} | {r.exposure:.0%} |")
    return "\n".join(out)


def scoreboard(rows: list[Row]) -> str:
    """按 (段, 周期) 汇总：中位数收益、赢囤币的比例、中位数回撤、中位数夏普。"""
    groups: dict = {}
    for x in rows:
        groups.setdefault((x.segment, x.timeframe), []).append(x.r)
    out = ["| 段 | 周期 | 标的数 | 收益中位数 | 囤币中位数 | 赢囤币 | 回撤中位数 | 夏普中位数 |", "|---|---|---|---|---|---|---|---|"]
    for (seg, tf), rs in sorted(groups.items()):
        med = statistics.median
        beat = sum(beats(r) for r in rs)
        out.append(f"| {seg} | {tf} | {len(rs)} | {med(r.total_return for r in rs):+.1%} | {med(r.hodl_return for r in rs):+.1%} "
                   f"| {beat}/{len(rs)} | {med(r.max_drawdown for r in rs):.0%} | {med(r.sharpe for r in rs):.2f} |")
    return "\n".join(out)


def download() -> None:
    from bot.data import make_exchange

    ex = make_exchange(EXCHANGE)
    for tf in TIMEFRAMES:
        for sym in UNIVERSE:
            c = fetch_history(ex, sym, tf, 0)
            print(f"{tf} {sym}: {len(c)} 根")
    print("恐惧贪婪指数:", len(fetch_fear_greed(0, use_cache=True)), "天")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy", nargs="?")
    ap.add_argument("--params", default="{}")
    ap.add_argument("--risk", default="{}")
    ap.add_argument("--size", type=float, default=1.0)
    ap.add_argument("--tf", help="逗号分隔，默认 1h,4h,1d")
    ap.add_argument("--symbols", help="逗号分隔，默认全篮子")
    ap.add_argument("--split", type=float)
    ap.add_argument("--fee", type=float, default=FEE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--download", action="store_true", help="下载/更新本地缓存")
    a = ap.parse_args(argv)
    if a.download:
        download()
        return 0
    if not a.strategy:
        ap.error("需要策略名")
    rows = evaluate(a.strategy, params=json.loads(a.params), risk=Risk(**json.loads(a.risk)), size=a.size,
                    symbols=a.symbols.split(",") if a.symbols else None, timeframes=a.tf.split(",") if a.tf else None,
                    split=a.split, fee=a.fee)
    if a.json:
        print(json.dumps([x.as_dict() for x in rows], ensure_ascii=False))
    else:
        print(table(rows))
        print()
        print(scoreboard(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
