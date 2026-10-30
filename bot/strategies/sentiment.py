"""情绪派：不看 K 线看人心——全市场恐惧贪婪指数（alternative.me，每天一个 0~100 的数）。

- fear_greed：指数跌进极度恐惧就梭，涨进极度贪婪就跑，中间拿着不动。"别人恐惧我贪婪"的直译版
- fear_greed_trend：恐慌过后，等价格重新站上均线才上车（不接飞刀）；跌破均线或者极度贪婪就跑
- sentiment_momentum：反着用——大盘转贪婪就上车、转恐惧就下车。指数有一半是拿波动率和动量/成交量算的，
  本质上更像一个"大盘趋势"信号，而不是抄底信号；回测里也是它最能打

注意事项：
- 指数是整个币圈（大头是 BTC）的情绪，拿去炒山寨币要打折，拿去炒股票代币（SPYX/NVDAX…）基本是玄学
- 指数每天 UTC 0 点更新一次，1d 最自然；1h/4h 上信号一天最多变一次，只是成交时点早一点
- 用 ctx.fear_greed.at() 取值，已经按"发布 1 小时后才可用"处理，回测不偷看未来
- 拿不到指数（数据源挂了、2018-02 以前没数据）时仓位原样不动，不会因为断数据乱下单
- 回测结论（Gate 2018-02 ~ 2026-10，样本内挑参数、样本外检验）：抄底那两个整体打不过囤币，结果主要看从哪天开始跑；
  跟着情绪走在币上样本外多数赢囤币、回撤小一截，但 BTC 1d 样本外照样输给囤币
"""
from __future__ import annotations

from .. import indicators as ind
from . import register

DAY_MS = 86_400_000
HELD = 0.5  # 仓位过半才算"在车上"，卖剩的零头不算
MAX_WARMUP = 990  # Gate 一页 1000 根，留点余量
MAX_FEAR_DAYS = 365  # 实盘 run.py 只拉最近 400 天指数，回看再长实盘就和回测对不上了


def _thresholds(name: str, params) -> tuple[float, float]:
    lo, hi = float(params["buy_below"]), float(params["sell_above"])
    if not lo < hi:
        raise ValueError(f"{name} 要求 buy_below < sell_above，收到 buy_below={lo:g}, sell_above={hi:g}")
    return lo, hi


def _index(ctx) -> int | None:
    """决策时点已经发布的最新指数；没有数据源或者太早没数据时返回 None。"""
    fg = ctx.fear_greed
    return None if fg is None else fg.at(ctx.asof_ms)


def _keep(position: float, ctx) -> tuple[float, str]:
    # 目标仓位 = 当前仓位，引擎算出来仓位差为 0，不会下单
    why = "没有接恐惧贪婪指数数据源" if ctx.fear_greed is None else "拿不到恐惧贪婪指数（这个时点还没有数据）"
    return min(1.0, max(0.0, float(position))), f"{why}，仓位原样不动"


# ---------------- 别人恐惧我贪婪 ----------------

@register(
    "fear_greed",
    title="别人恐惧我贪婪",
    school="情绪派",
    summary="恐惧贪婪指数跌到 buy_below 及以下（极度恐惧）就梭，涨到 sell_above 及以上（极度贪婪）就跑，中间拿着不动。"
            "回测里\"贪婪就跑\"这条腿在牛市全线亏钱，所以默认 sell_above=101 = 只抄底不逃顶：等一次大恐慌梭进去然后一直拿着。",
    defaults={"buy_below": 15, "sell_above": 101},
    warmup=lambda p: 1,
    needs=("fear_greed",),
    timeframes=("1d",),
)
def fear_greed(candles, position, params, ctx):
    lo, hi = _thresholds("fear_greed", params)
    v = _index(ctx)
    if v is None:
        return _keep(position, ctx)
    held = position >= HELD
    if v <= lo:
        return 1.0, (f"恐惧贪婪指数 {v}，还在极度恐惧，拿住" if held
                     else f"恐惧贪婪指数 {v}（≤{lo:g}），别人恐惧我贪婪，梭！")
    if v >= hi:
        return 0.0, (f"恐惧贪婪指数 {v}（≥{hi:g}），别人贪婪我恐惧，跑！" if held
                     else f"恐惧贪婪指数 {v}，太贪婪了，空仓等恐慌")
    if held:
        return 1.0, (f"恐惧贪婪指数 {v}，只抄底不逃顶，拿住" if hi > 100 else f"恐惧贪婪指数 {v}，没到贪婪线 {hi:g}，拿住")
    return 0.0, f"恐惧贪婪指数 {v}，没到恐惧线 {lo:g}，空仓等恐慌"


# ---------------- 恐慌见底再上车 ----------------

def _trend_params(params) -> tuple[float, float, int, int]:
    lo, hi = _thresholds("fear_greed_trend", params)
    n, days = int(params["ema"]), int(params["fear_days"])
    if n < 1 or not 1 <= days <= MAX_FEAR_DAYS:
        raise ValueError(f"fear_greed_trend 要求 ema >= 1、1 <= fear_days <= {MAX_FEAR_DAYS}，收到 ema={n}, fear_days={days}")
    return lo, hi, n, days


def _trend_warmup(params) -> int:
    _, _, n, _ = _trend_params(params)
    # EMA 有记忆，3 倍周期让开头那段 SMA 种子的影响衰减到约 0.25%
    need = 3 * n + 10
    if need > MAX_WARMUP:
        raise ValueError(f"fear_greed_trend 需要 {need} 根 K 线预热，超过上限 {MAX_WARMUP}（Gate 一页 1000 根），把 ema 调小点")
    return need


def last_extreme(ctx, lo: float, hi: float, days: int) -> tuple[str | None, int | None, int | None]:
    """往回翻最近 days 天（含今天）已发布的指数，找最近一次极端读数。

    返回 ("fear" | "greed" | None, 那天的值, 距今几天)。只往过去看，不偷看未来。
    """
    fg = ctx.fear_greed
    if fg is None:
        return None, None, None
    for j in range(days):
        v = fg.at(ctx.asof_ms - j * DAY_MS)
        if v is None:  # 翻到数据开头了
            break
        if v <= lo:
            return "fear", v, j
        if v >= hi:
            return "greed", v, j
    return None, None, None


@register(
    "fear_greed_trend",
    title="恐慌见底再上车",
    school="情绪派",
    summary="最近 fear_days 天里最后一次极端读数是极度恐惧（≤buy_below），而且收盘重新站上 EMA(ema) 才梭——"
            "恐慌过后等价格先证明自己，不接飞刀；收盘跌破均线（趋势坏了）就跑，指数到 sell_above 及以上也跑"
            "（默认 101 = 不因贪婪下车，回测里贪婪就跑同样亏钱）。ema 按 K 线根数算，只适合 1d，4h 上会被来回打脸。",
    defaults={"buy_below": 25, "sell_above": 101, "ema": 50, "fear_days": 365},
    warmup=_trend_warmup,
    needs=("fear_greed",),
    timeframes=("1d",),
)
def fear_greed_trend(candles, position, params, ctx):
    lo, hi, n, days = _trend_params(params)
    v = _index(ctx)
    if v is None:
        return _keep(position, ctx)
    close = candles[-1].close
    line = ind.ema(ind.closes(candles), n)[-1]
    gap = close / line - 1 if line else 0.0
    if position >= HELD:
        if v >= hi:
            return 0.0, f"恐惧贪婪指数 {v}（≥{hi:g}），极度贪婪，落袋为安，跑！"
        if close < line:
            return 0.0, f"收盘跌破 EMA{n}（{gap:+.1%}），趋势坏了，跑！"
        return 1.0, f"恐惧贪婪指数 {v}，收盘在 EMA{n} 上方（{gap:+.1%}），拿住"
    if close <= line:
        return 0.0, f"恐惧贪婪指数 {v}，收盘在 EMA{n} 下方（{gap:+.1%}），不接飞刀"
    kind, val, ago = last_extreme(ctx, lo, hi, days)
    when = "今天" if ago == 0 else f"{ago} 天前"
    if kind == "fear":
        return 1.0, f"{when}指数到过 {val}（极度恐惧），现在收盘站上 EMA{n}（{gap:+.1%}），恐慌见底，梭！"
    if kind == "greed":
        return 0.0, f"最近一次极端读数是{when}的 {val}（极度贪婪），等下一次恐慌"
    return 0.0, f"收盘在 EMA{n} 上方，但近 {days} 天没出现过极度恐惧，不追"


# ---------------- 跟着情绪走 ----------------

def _momentum_params(params) -> tuple[float, float]:
    up, down = float(params["enter"]), float(params["exit"])
    if not down < up:
        raise ValueError(f"sentiment_momentum 要求 exit < enter，收到 enter={up:g}, exit={down:g}")
    return up, down


@register(
    "sentiment_momentum",
    title="跟着情绪走",
    school="情绪派",
    summary="反着用恐惧贪婪指数：指数涨到 enter 及以上（大盘转贪婪）就梭，跌到 exit 及以下（大盘转恐惧）就跑，中间维持现状。"
            "指数有一半是拿波动率和动量算的，当趋势信号用比当抄底信号靠谱；一年只成交两到四笔，手续费很省。",
    defaults={"enter": 70, "exit": 40},
    warmup=lambda p: 1,
    needs=("fear_greed",),
    timeframes=("1d", "4h"),
)
def sentiment_momentum(candles, position, params, ctx):
    up, down = _momentum_params(params)
    v = _index(ctx)
    if v is None:
        return _keep(position, ctx)
    held = position >= HELD
    if v >= up:
        return 1.0, (f"恐惧贪婪指数 {v}，大盘还在贪婪区，拿住" if held
                     else f"恐惧贪婪指数 {v}（≥{up:g}），大盘转贪婪，跟上，梭！")
    if v <= down:
        return 0.0, (f"恐惧贪婪指数 {v}（≤{down:g}），大盘转恐惧，跑！" if held
                     else f"恐惧贪婪指数 {v}，大盘还在恐惧区，空仓")
    if held:
        return 1.0, f"恐惧贪婪指数 {v}，没跌到下车线 {down:g}，拿住"
    return 0.0, f"恐惧贪婪指数 {v}，没涨到上车线 {up:g}，空仓等"
