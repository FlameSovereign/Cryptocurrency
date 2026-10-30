"""轮动梭哈：一篮子币里只拿最强的那一个（“每天梭哈最强的那个币”）。

横截面动量（cross-sectional momentum）：每到决策时点（机器人的周期，一般 1d），给篮子里每个币
算过去 N 根 K 线的涨幅、排个名，现金全梭进第一名；第一名自己都没涨（绝对动量过滤），就拿现金。
换车要交两笔手续费（卖 + 买，Gate VIP0 合计约 0.4%，再加滑点），所以新第一名得比手里这个
强出一截（margin）才换，差不多的时候拿着不动。

为什么不进 strategies 注册表：单币策略回答“这个币拿几成”，轮动回答“拿哪个币”，函数签名不同。
这里有自己的小注册表 ROTATIONS，config.py 单独校验，run.py 走 rotation_tick。

轮动函数约定（规矩和单币策略一样）：

    fn(windows, holding, params, ctx) -> (目标交易对 | None, "说明")

- windows：{交易对: 已收盘 K 线（旧 → 新）}，每个恰好 warmup(params) 根、最后一根时间相同；
  历史不够的币（刚上市）不在里面
- holding：当前持有的交易对，None = 拿着现金。滞后只能靠它，不许藏状态
- 返回 None = 拿现金；同样输入必须同样输出（GitHub Actions 会重跑、漏跑）

记账复用同一个 Wallet：wallet.holding = 手里的交易对，wallet.qty = 这个币的数量。
回测和实盘共用 decide() + rebalance()，保证“回测里赚的钱”和“实盘里的逻辑”是同一套。

命令行回测：
    python -m bot.rotation --bot rotation --days 730          # config.toml 里的轮动机器人
    python -m bot.rotation --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframe 1d --params '{"lookback": 30}'
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .backtest import Result, _sharpe, format_result, max_drawdown
from .candles import YEAR_MS, Candle, timeframe_ms
from .engine import OrderPlan, Risk, plan_order, simulate_fill
from .strategies import Context, Strategy
from .wallet import Fill, Wallet

SCORES = ("ret", "sharpe")
ROTATIONS: dict[str, Strategy] = {}


def _register(name: str, *, title: str, school: str, summary: str, defaults: Mapping[str, object],
              warmup: Callable[[Mapping[str, object]], int]):
    def deco(fn):
        if name in ROTATIONS:
            raise ValueError(f"轮动策略重名: {name}")
        ROTATIONS[name] = Strategy(name, title, school, summary, dict(defaults), warmup, fn)
        return fn

    return deco


def get(name: str) -> Strategy:
    try:
        return ROTATIONS[name]
    except KeyError:
        raise KeyError(f"没有叫 {name!r} 的轮动策略，可选: {', '.join(sorted(ROTATIONS))}") from None


def is_rotation(name: str) -> bool:
    return name in ROTATIONS


def resolve(name: str, params: Mapping[str, object] | None) -> dict:
    """合并默认参数并校验；写错参数名、score 写错都直接报错。"""
    p = get(name).resolve(params)
    if "score" in p and p["score"] not in SCORES:
        raise ValueError(f"轮动策略 {name} 的 score 只能是 {' / '.join(SCORES)}，收到 {p['score']!r}")
    return p


def base(symbol: str) -> str:
    return symbol.split("/")[0]


# ---------------- 动量 ----------------

def momentum(candles: Sequence[Candle], n: int, score: str = "ret") -> tuple[float, float] | None:
    """(排名分, 近 n 根涨幅)，数据不够或价格异常返回 None。

    score="ret"：排名分就是涨幅
    score="sharpe"：排名分 = 对数涨幅 / (单根对数收益的波动 × √n)，涨得越稳分越高，暴拉暴砸的妖币被打折
    """
    if n < 1 or len(candles) < n + 1:
        return None
    w = candles[-1 - n :]
    if any(c.close <= 0 for c in w):
        return None
    ret = w[-1].close / w[0].close - 1.0
    if score == "ret":
        return ret, ret
    lr = [math.log(b.close / a.close) for a, b in zip(w, w[1:])]
    m = sum(lr) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in lr) / n)
    return sum(lr) / (max(sd, 1e-9) * math.sqrt(n)), ret


def align(windows: Mapping[str, Sequence[Candle]]) -> tuple[dict, list[str]]:
    """只留最后一根 K 线是最新那根的币；落后的（停牌、接口延迟）这次不参与排名。返回 (保留的, 掉队的)。"""
    windows = {s: w for s, w in windows.items() if w}
    if not windows:
        return {}, []
    last = max(w[-1].ts for w in windows.values())
    keep = {s: w for s, w in windows.items() if w[-1].ts == last}
    return keep, [s for s in windows if s not in keep]


# ---------------- 轮动策略 ----------------

@_register(
    "rotation",
    title="轮动梭哈",
    school="动量派",
    summary="篮子里近 N 根涨得最猛的币全仓梭哈；第一名涨幅没过 min_ret 就拿现金，手里的跌破 exit_ret 才下车；"
            "新第一名要比手里的强出 margin 才换车，少交手续费。",
    defaults={"lookback": 30, "margin": 0.10, "score": "ret", "min_ret": 0.0, "exit_ret": 0.0},
    warmup=lambda p: int(p["lookback"]) + 1,
)
def rotation(windows, holding, params, ctx):
    n = max(1, int(params["lookback"]))
    margin, floor = float(params["margin"]), float(params["min_ret"])
    exit_line = min(float(params["exit_ret"]), floor)  # 下车线比上车线还高会刚上车就下车，压回上车线
    board = {s: m for s, c in windows.items() if (m := momentum(c, n, str(params["score"]))) is not None}
    if not board:
        return None, "篮子里没有能算动量的币，拿现金"
    leader = max(board, key=lambda s: board[s][0])  # 同分取篮子里排前面的，结果固定
    lead_score, lead_ret = board[leader]
    tail = f"（近 {n} 根）"
    if holding in board:
        hold_score, hold_ret = board[holding]
        if hold_ret > exit_line:  # 手里的还没跌破下车线
            if leader != holding and lead_score > hold_score + margin and lead_ret > floor:
                return leader, f"{base(leader)} {lead_ret:+.1%} 比手里的 {base(holding)} {hold_ret:+.1%} 强出一截{tail}，换车！"
            if leader != holding:
                return holding, f"{base(leader)} {lead_ret:+.1%} 略强于 {base(holding)} {hold_ret:+.1%}{tail}，没强出门槛，拿住"
            return holding, f"{base(holding)} {hold_ret:+.1%} 还是第一{tail}，拿住"
        if leader != holding and lead_ret > floor:
            return leader, f"{base(holding)} {hold_ret:+.1%} 动量没了，换成第一名 {base(leader)} {lead_ret:+.1%}{tail}"
        return None, f"最强的 {base(leader)} 也只有 {lead_ret:+.1%}{tail}，全篮子没动量，跑！"
    if lead_ret > floor:
        return leader, f"第一名 {base(leader)} {lead_ret:+.1%}{tail}，梭！"
    return None, f"最强的 {base(leader)} 也只有 {lead_ret:+.1%}{tail}，拿现金观望"


@_register(
    "rotation_monkey",
    title="轮动猴子",
    school="对照组",
    summary="每隔 hold 根 K 线从篮子里随机挑一个币梭哈（p_cash 的概率拿现金）。打不过它的轮动 = 玄学。",
    defaults={"hold": 10, "p_cash": 0.3, "seed": 42},
    warmup=lambda p: 1,
)
def rotation_monkey(windows, holding, params, ctx):
    hold = max(1, int(params["hold"]))
    last = max(w[-1].ts for w in windows.values())
    # 用 (种子, 时间段) 做随机种子：同一时间段结果固定，满足幂等
    rng = random.Random(f"{params['seed']}:{last // (timeframe_ms(ctx.timeframe) * hold)}")
    if rng.random() < float(params["p_cash"]):
        return None, "猴子说：拿现金！"
    pick = rng.choice(sorted(windows))
    return pick, f"猴子说：梭 {base(pick)}！"


# ---------------- 决策 & 调仓（回测、模拟盘、实盘共用） ----------------

@dataclass(frozen=True)
class RotationDecision:
    target: str | None  # 要持有的交易对；None = 现金
    event: str | None  # kill_switch / halted；正常信号为 None
    reason: str


def decide(rot: Strategy, params: dict, windows: Mapping[str, Sequence[Candle]], wallet: Wallet, price: float | None,
           *, risk: Risk = Risk(), ctx: Context) -> RotationDecision:
    """风控 + 轮动信号 -> 该拿哪个币。不碰钱包。price = 手里那个币的最新价（空仓时不用）。"""
    if wallet.halted:
        return RotationDecision(None, "halted", "已熔断，罢工中（删掉 state 里这个机器人的文件可复活）")
    if wallet.holding is not None and price is None:
        raise ValueError(f"手里拿着 {wallet.holding} 却没有它的价格")
    equity = wallet.cash + (wallet.qty * price if wallet.holding is not None else 0.0)
    if risk.kill_switch > 0 and equity <= wallet.budget * (1 - risk.kill_switch):
        return RotationDecision(None, "kill_switch", f"净值 {equity:.2f} 跌破熔断线，清仓停机")
    if not windows:
        return RotationDecision(None, None, "篮子里没有一个币有足够的已收盘 K 线，拿现金")
    result, note = rot.fn(windows, wallet.holding, params, ctx), ""
    if isinstance(result, tuple):
        result, note = result[0], str(result[1])
    if result is not None and result not in windows:
        raise ValueError(f"轮动策略 {rot.name} 选了不在候选里的 {result!r}")
    return RotationDecision(result, None, note)


def flatten(wallet: Wallet) -> None:
    """清掉卖剩的零头，回到纯现金（零头留在交易所账户里，不再算这个机器人的）。"""
    wallet.qty = 0.0
    wallet.holding = wallet.entry_price = wallet.high_water = wallet.entry_equity = None


def rebalance(
    wallet: Wallet,
    target: str | None,
    prices: Mapping[str, float],
    *,
    size: float = 1.0,
    threshold: float = 0.02,
    min_cost: Callable[[str, float], float],
    fill: Callable[[str, OrderPlan, float], Fill | None],
    on_fill: Callable[[str, Fill, float | None], None] | None = None,
) -> list[str]:
    """按目标调仓：先把手里不该拿的币全卖掉，卖干净了再把现金梭进目标。返回备注。

    prices：{交易对: 下单参考价}，至少要有手里的币和目标币
    min_cost(symbol, price)：交易所最小下单额
    fill(symbol, plan, price) -> Fill | None：真去成交（实盘下单 / 模拟撮合）；None = 这次没成交
    on_fill(symbol, fill, 平仓盈亏)：记完账以后马上调用（实盘用来第一时间存盘）
    """
    notes: list[str] = []
    held = wallet.holding
    if held is None and wallet.qty > 0:
        raise ValueError("钱包里有币却没记是哪个交易对（wallet.holding 为空）")

    if held is not None and held != target:
        px = prices[held]
        mc = min_cost(held, px)
        plan = plan_order(0.0, wallet, px, threshold=threshold, min_cost=mc)
        got = pnl = None
        if plan:
            got = fill(held, plan, px)
            if got is None:
                return notes  # 没卖掉就别买，下次再来
            pnl = wallet.apply(got)
        left = wallet.qty * px
        if left >= mc:
            notes.append(f"{held} 还剩 {wallet.qty:.8g}（约 {left:.2f}）没卖完，这次先不换")
        else:
            if left >= wallet.dust_value():
                notes.append(f"{held} 剩 {wallet.qty:.8g}（约 {left:.2f}）低于最小下单额卖不掉，记作损耗")
            flatten(wallet)
        if got is not None and on_fill:
            on_fill(held, got, pnl)
        if wallet.holding is not None:
            return notes

    if target is None:
        return notes
    px = prices[target]
    plan = plan_order(size, wallet, px, threshold=threshold, min_cost=min_cost(target, px))
    if plan is None:
        return notes
    got = fill(target, plan, px)
    if got is None:
        return notes
    wallet.holding = target
    pnl = wallet.apply(got)
    if on_fill:
        on_fill(target, got, pnl)
    return notes


# ---------------- 回测 ----------------

def run_backtest(
    rot: Strategy | str,
    data: Mapping[str, Sequence[Candle]],
    *,
    timeframe: str,
    params: dict | None = None,
    budget: float = 100.0,
    size: float = 1.0,
    risk: Risk = Risk(),
    fee: float = 0.002,
    slippage: float | Mapping[str, float] = 0.0005,
    threshold: float = 0.02,
    min_cost: float = 3.0,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> Result:
    """多币轮动回测：第 i 根收盘决策，第 i+1 根开盘成交，换车时先卖后买。

    data：{交易对: K 线}，按时间戳对齐；某个币历史不够 warmup 就先不进候选。
    slippage：一个数，或者 {交易对: 滑点}（缺的用 0.0005）。
    hodl_return = 首个决策点已有足够历史的币等权买入、一直拿着（同样扣一次手续费 + 滑点）。
    """
    rot = get(rot) if isinstance(rot, str) else rot
    params = resolve(rot.name, params) if rot.name in ROTATIONS else rot.resolve(params)
    if risk.stop_loss or risk.take_profit or risk.trailing_stop:
        raise ValueError("轮动目前只支持 risk.kill_switch")
    look = rot.lookback(params)
    tf = timeframe_ms(timeframe)
    symbols = list(data)
    series = {s: [c for c in data[s] if end_ms is None or c.ts < end_ms] for s in symbols}
    index = {s: {c.ts: i for i, c in enumerate(cs)} for s, cs in series.items()}
    timeline = sorted({c.ts for cs in series.values() for c in cs})
    slip = (lambda s: float(slippage.get(s, 0.0005))) if isinstance(slippage, Mapping) else (lambda s: float(slippage))

    def ready(s: str, t: int) -> bool:
        return index[s].get(t, -1) >= look - 1

    first = next((k for k, t in enumerate(timeline)
                  if (start_ms is None or t >= start_ms) and any(ready(s, t) for s in symbols)), len(timeline))
    if first >= len(timeline) - 1:
        raise ValueError(f"K 线不够：{rot.name} 每个币至少要 {look + 1} 根")

    wallet = Wallet.new(budget)
    curve: list[tuple[int, float]] = [(timeline[first] + tf, budget)]
    trades: list[dict] = []
    stats = {"wins": 0, "round_trips": 0, "in_bars": 0}
    last_close: dict[str, float] = {}

    def close_at(s: str, t: int) -> None:
        i = index[s].get(t)
        if i is not None:
            last_close[s] = series[s][i].close

    for k in range(first, len(timeline) - 1):
        t, nxt = timeline[k], timeline[k + 1]
        for s in symbols:
            close_at(s, t)
        pool = {s: series[s][index[s][t] - look + 1 : index[s][t] + 1] for s in symbols if ready(s, t)}
        held = wallet.holding
        price = last_close.get(held) if held else None
        if held:
            wallet.mark(price)
        if held is None or held in pool:  # 持仓币这根缺数据就不动（实盘也是这样）
            d = decide(rot, params, pool, wallet, price, risk=risk, ctx=Context(timeframe=timeframe, asof_ms=t + tf))
            if d.event == "kill_switch":
                wallet.halted = True
            prices = {s: w[-1].close for s, w in pool.items()}

            def fill(sym, plan, px, nxt=nxt):
                j = index[sym].get(nxt)
                if j is None:
                    return None
                return simulate_fill(plan, series[sym][j].open, fee=fee, slippage=slip(sym), ts=nxt)

            def on_fill(sym, f, pnl, d=d):
                trades.append({"ts": f.ts, "symbol": sym, "side": f.side, "price": f.price, "base": f.base,
                               "quote": f.quote, "event": d.event, "reason": d.reason})
                if pnl is not None:
                    stats["round_trips"] += 1
                    stats["wins"] += pnl > 0

            rebalance(wallet, d.target, prices, size=size, threshold=threshold,
                      min_cost=lambda s, p: min_cost, fill=fill, on_fill=on_fill)
        for s in symbols:
            close_at(s, nxt)
        if wallet.holding is not None:
            stats["in_bars"] += 1
        curve.append((nxt + tf, wallet.cash + (wallet.qty * last_close[wallet.holding] if wallet.holding else 0.0)))

    # 对照：首个决策点已经能参与排名的币，各自下一根开盘等权买入拿到底
    t0 = timeline[first]
    entry, hold_px = {}, {}
    for s in symbols:
        j = bisect.bisect_right([c.ts for c in series[s]], t0)
        if ready(s, t0) and j < len(series[s]):
            entry[s] = series[s][j].open * (1 + slip(s))
    hodl_curve = []
    for t in timeline[first + 1 :]:
        for s in entry:
            i = index[s].get(t)
            if i is not None:
                hold_px[s] = series[s][i].close
        held_now = [s for s in entry if s in hold_px]
        hodl_curve.append(sum(hold_px[s] / entry[s] for s in held_now) / len(held_now) * (1 - fee) if held_now else 1.0)

    values = [v for _, v in curve]
    span = curve[-1][0] - curve[0][0]
    final = values[-1]
    return Result(
        strategy=rot.name,
        params=params,
        symbol="+".join(base(s) for s in symbols),
        timeframe=timeframe,
        start_ms=curve[0][0],
        end_ms=curve[-1][0],
        bars=len(curve) - 1,
        final_equity=final,
        total_return=final / budget - 1,
        cagr=(final / budget) ** (YEAR_MS / span) - 1 if span > 0 and final > 0 else -1.0,
        max_drawdown=max_drawdown(values),
        sharpe=_sharpe(values, YEAR_MS / tf),
        fills=wallet.fills,
        round_trips=stats["round_trips"],
        win_rate=stats["wins"] / stats["round_trips"] if stats["round_trips"] else None,
        exposure=stats["in_bars"] / max(1, len(curve) - 1),
        fees_paid=wallet.fees_paid,
        hodl_return=hodl_curve[-1] - 1,
        hodl_max_drawdown=max_drawdown(hodl_curve),
        curve=curve,
        trades=trades,
    )


def main(argv: list[str] | None = None) -> int:
    from .candles import now_ms
    from .config import load_config
    from .data import fetch_history

    ap = argparse.ArgumentParser(description="回测轮动策略")
    ap.add_argument("--bot", help="直接回测 config.toml 里的轮动机器人")
    ap.add_argument("--strategy", default="rotation")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,DOGE/USDT,GT/USDT")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--params", default="{}", help='JSON，例如 \'{"lookback": 30, "margin": 0.1}\'')
    ap.add_argument("--kill-switch", type=float, default=0.0)
    ap.add_argument("--days", type=int, default=365 * 3)
    ap.add_argument("--split", type=float, help="比如 0.6：前 60%% 样本内 / 后 40%% 样本外分开报")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存，不联网")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.bot:
        bot = next((b for b in cfg.bots if b.name == args.bot), None)
        if bot is None or not is_rotation(bot.strategy):
            ap.error(f"config.toml 里没有轮动机器人 {args.bot}")
        name, symbols, timeframe, params, size, risk = bot.strategy, list(bot.symbols), bot.timeframe, bot.params, bot.size, bot.risk
        slippage = bot.slippage if bot.slippage is not None else cfg.slippage
    else:
        name, symbols, timeframe = args.strategy, [s.strip().upper() for s in args.symbols.split(",") if s.strip()], args.timeframe
        params, size, risk, slippage = json.loads(args.params), 1.0, Risk(kill_switch=args.kill_switch), cfg.slippage
    rot = get(name)
    params = resolve(name, params)
    tf = timeframe_ms(timeframe)
    start = now_ms() - args.days * 86_400_000
    since = start - rot.lookback(params) * tf
    data = {s: fetch_history(cfg.exchange, s, timeframe, since, offline=args.offline) for s in symbols}
    kw = dict(timeframe=timeframe, params=params, size=size, risk=risk, fee=cfg.fee, slippage=slippage,
              threshold=cfg.rebalance_threshold)
    runs = [("", dict(start_ms=start))]
    if args.split:
        bars = sorted({c.ts for cs in data.values() for c in cs if c.ts >= start})
        cut = bars[int(len(bars) * args.split)]
        runs = [("样本内 ", dict(start_ms=start, end_ms=cut)), ("样本外 ", dict(start_ms=cut))]
    for label, seg in runs:
        r = run_backtest(rot, data, **kw, **seg)
        print(json.dumps({"segment": label.strip() or "full", **r.summary()}, ensure_ascii=False) if args.json
              else label + format_result(r).replace("对比囤币", "对比等权囤币"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
