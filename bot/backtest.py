"""回测：用和实盘完全相同的 decide / plan_order 逻辑在历史 K 线上跑一遍。

规则：
- 第 i 根 K 线收盘时做决策，第 i+1 根开盘价成交（不偷看未来）
- 手续费 + 滑点照扣，最小下单金额照卡
- 风控（止损/止盈/熔断）按每根 K 线收盘价检查（实盘是每次运行用最新价检查，会更灵敏一些）

命令行：
    python -m bot.backtest --strategy sma_cross --symbol BTC/USDT --timeframe 4h --days 730
    python -m bot.backtest --bot btc-trend          # 直接回测 config.toml 里的某个机器人
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .candles import YEAR_MS, Candle, now_ms, timeframe_ms
from .engine import Risk, apply_events, decide, plan_order, simulate_fill
from .strategies import Context, FearGreed, Strategy, get
from .wallet import Wallet


@dataclass
class Result:
    strategy: str
    params: dict
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    bars: int
    final_equity: float
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    fills: int
    round_trips: int
    win_rate: float | None
    exposure: float  # 持仓时间占比（有仓位的 K 线占比）
    fees_paid: float
    hodl_return: float
    hodl_max_drawdown: float
    avg_exposure: float = 0.0  # 平均仓位（每根 K 线 币值/净值 的均值；网格这类部分仓位的策略看这个）
    curve: list[tuple[int, float]] = field(default_factory=list, repr=False)
    trades: list[dict] = field(default_factory=list, repr=False)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("curve")
        d.pop("trades")
        return d


def max_drawdown(values) -> float:
    peak, mdd = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def sharpe(values, bars_per_year: float) -> float:
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return m / sd * math.sqrt(bars_per_year) if sd > 0 else 0.0


def run_backtest(
    strategy: Strategy | str,
    candles: list[Candle],
    *,
    timeframe: str,
    symbol: str = "",
    params: dict | None = None,
    budget: float = 100.0,
    size: float = 1.0,
    risk: Risk = Risk(),
    fee: float = 0.002,
    slippage: float = 0.0005,
    threshold: float = 0.02,
    min_cost: float = 3.0,
    fear_greed: FearGreed | None = None,
    start_ms: int | None = None,
) -> Result:
    strat = get(strategy) if isinstance(strategy, str) else strategy
    params = strat.resolve(params)
    look = strat.lookback(params)
    tf = timeframe_ms(timeframe)
    if "fear_greed" in strat.needs and fear_greed is None:
        raise ValueError(f"策略 {strat.name} 需要恐惧贪婪指数数据（fear_greed=...）")

    first = look - 1
    if start_ms is not None:
        first = max(first, next((i for i, c in enumerate(candles) if c.ts >= start_ms), len(candles)))
    if first >= len(candles) - 1:
        raise ValueError(f"K 线不够：{strat.name} 需要至少 {look + 1} 根，只有 {len(candles)} 根")

    wallet = Wallet.new(budget)
    curve: list[tuple[int, float]] = [(candles[first].ts + tf, budget)]
    trades: list[dict] = []
    wins = round_trips = in_bars = 0
    exposure_sum = 0.0

    for i in range(first, len(candles) - 1):
        bar, nxt = candles[i], candles[i + 1]
        price = bar.close
        wallet.mark(price)
        ctx = Context(timeframe=timeframe, asof_ms=bar.ts + tf, symbol=symbol, fear_greed=fear_greed)
        d = decide(strat, params, candles[i - look + 1 : i + 1], wallet, price, size=size, risk=risk, ctx=ctx)
        apply_events(wallet, d)
        plan = plan_order(d.target, wallet, price, threshold=threshold, min_cost=min_cost)
        if plan:
            fill = simulate_fill(plan, nxt.open, fee=fee, slippage=slippage, ts=nxt.ts)
            pnl = wallet.apply(fill, flat_below=min_cost)
            trades.append({"ts": nxt.ts, "side": fill.side, "price": fill.price, "base": fill.base,
                           "quote": fill.quote, "event": d.event, "reason": d.reason})
            if pnl is not None:
                round_trips += 1
                wins += pnl > 0
        if wallet.in_position:
            in_bars += 1
        exposure_sum += wallet.exposure(nxt.close)
        curve.append((nxt.ts + tf, wallet.equity(nxt.close)))

    values = [v for _, v in curve]
    span = curve[-1][0] - curve[0][0]
    final = values[-1]
    total = final / budget - 1
    cagr = (final / budget) ** (YEAR_MS / span) - 1 if span > 0 and final > 0 else -1.0
    entry = candles[first + 1].open * (1 + slippage)
    hodl_curve = [c.close / entry * (1 - fee) for c in candles[first + 1 :]]
    return Result(
        strategy=strat.name,
        params=params,
        symbol=symbol,
        timeframe=timeframe,
        start_ms=curve[0][0],
        end_ms=curve[-1][0],
        bars=len(curve) - 1,
        final_equity=final,
        total_return=total,
        cagr=cagr,
        max_drawdown=max_drawdown(values),
        sharpe=sharpe(values, YEAR_MS / tf),
        fills=wallet.fills,
        round_trips=round_trips,
        win_rate=wins / round_trips if round_trips else None,
        exposure=in_bars / max(1, len(curve) - 1),
        fees_paid=wallet.fees_paid,
        hodl_return=hodl_curve[-1] - 1,
        hodl_max_drawdown=max_drawdown(hodl_curve),
        avg_exposure=exposure_sum / max(1, len(curve) - 1),
        curve=curve,
        trades=trades,
    )


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def format_result(r: Result) -> str:
    wr = "-" if r.win_rate is None else f"{r.win_rate:.0%}"
    return (
        f"{r.strategy} {r.params} | {r.symbol} {r.timeframe} | {_day(r.start_ms)} → {_day(r.end_ms)}\n"
        f"  收益 {r.total_return:+.1%}（年化 {r.cagr:+.1%}）  最大回撤 {r.max_drawdown:.1%}  夏普 {r.sharpe:.2f}\n"
        f"  成交 {r.fills} 笔 / {r.round_trips} 轮，胜率 {wr}，持仓时间 {r.exposure:.0%}，手续费 {r.fees_paid:.2f}\n"
        f"  对比囤币：{r.hodl_return:+.1%}（最大回撤 {r.hodl_max_drawdown:.1%}）"
    )


def main(argv: list[str] | None = None) -> int:
    from .config import load_config
    from .data import fetch_fear_greed, fetch_history

    ap = argparse.ArgumentParser(description="回测一个策略")
    ap.add_argument("--bot", help="直接回测 config.toml 里的机器人")
    ap.add_argument("--strategy")
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--params", default="{}", help='JSON，例如 \'{"fast": 20, "slow": 60}\'')
    ap.add_argument("--risk", default="{}", help='JSON，例如 \'{"stop_loss": 0.1}\'')
    ap.add_argument("--size", type=float, default=1.0)
    ap.add_argument("--days", type=int, default=365 * 3)
    ap.add_argument("--fee", type=float)
    ap.add_argument("--slippage", type=float)
    ap.add_argument("--offline", action="store_true", help="只用本地缓存，不联网")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.bot:
        bot = next((b for b in cfg.bots if b.name == args.bot), None)
        if bot is None:
            ap.error(f"config.toml 里没有机器人 {args.bot}")
        if bot.is_rotation:  # 轮动机器人有自己的多币回测
            from . import rotation

            return rotation.main(["--bot", bot.name, "--days", str(args.days)] + (["--offline"] if args.offline else [])
                                 + (["--json"] if args.json else []))
        strategy, symbol, timeframe, params = bot.strategy, bot.symbol, bot.timeframe, bot.params
        size, risk = bot.size, bot.risk
        slippage = bot.slippage if bot.slippage is not None else cfg.slippage
    else:
        if not args.strategy:
            ap.error("需要 --strategy 或 --bot")
        strategy, symbol, timeframe = args.strategy, args.symbol.upper(), args.timeframe
        params, size, risk = json.loads(args.params), args.size, Risk(**json.loads(args.risk))
        slippage = cfg.slippage
    fee = cfg.fee if args.fee is None else args.fee
    slippage = slippage if args.slippage is None else args.slippage

    strat = get(strategy)
    params = strat.resolve(params)
    look = strat.lookback(params)
    tf = timeframe_ms(timeframe)
    start = now_ms() - args.days * 86_400_000
    candles = fetch_history(cfg.exchange, symbol, timeframe, start - look * tf, offline=args.offline)
    fng = fetch_fear_greed(0, use_cache=True, offline=args.offline) if "fear_greed" in strat.needs else None
    r = run_backtest(strat, candles, timeframe=timeframe, symbol=symbol, params=params, size=size, risk=risk,
                     fee=fee, slippage=slippage, threshold=cfg.rebalance_threshold, fear_greed=fng, start_ms=start)
    print(json.dumps(r.summary(), ensure_ascii=False) if args.json else format_result(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
