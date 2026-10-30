"""动量派：涨着的东西倾向于继续涨，跌着的倾向于继续跌——直到某天不再如此。

- tsmom：时间序列动量，过去 N 根 K 线涨了就拿着，跌了就跑
- macd：MACD 金叉死叉，国内炒股软件里人手一个
- momentum_vol：动量为正才上车，仓位按波动率打折（研究用，梭哈党慎用）

共同的毛病：震荡市来回打脸，每进出一次就交两笔约 0.2% 的手续费；顶部要回吐一截才下车。
"""
from __future__ import annotations

import math

from ..candles import YEAR_MS, timeframe_ms
from ..indicators import ema
from . import register

HELD = 0.5  # 仓位过半才算“在车上”，卖剩的零头不算


def _ret(candles, n: int) -> float | None:
    """最新收盘价相对 n 根之前收盘价的涨幅。"""
    past = candles[-1 - n].close
    return candles[-1].close / past - 1.0 if past > 0 else None


def _band(params) -> tuple[float, float]:
    enter = float(params["enter"])
    # 离场线比进场线还高会刚上车就下车，压回进场线
    return enter, min(float(params["exit"]), enter)


@register(
    "tsmom",
    title="时间序列动量",
    school="动量派",
    summary="过去 N 根 K 线涨幅超过进场线就梭，跌破离场线就跑；进出两条线分开，少在零轴附近来回挨手续费。",
    defaults={"lookback": 20, "enter": 0.0, "exit": 0.0},
    warmup=lambda p: int(p["lookback"]) + 1,
    timeframes=("1d", "4h"),
)
def tsmom(candles, position, params, ctx):
    n = max(1, int(params["lookback"]))
    enter, exit_ = _band(params)
    r = _ret(candles, n)
    if r is None:
        return 0.0, "价格数据异常，空仓"
    if position >= HELD:
        if r >= exit_:
            return 1.0, f"近 {n} 根涨 {r:+.1%}，没破离场线 {exit_:+.1%}，拿住"
        return 0.0, f"近 {n} 根涨 {r:+.1%}，跌破离场线 {exit_:+.1%}，动量没了，跑！"
    if r > enter:
        return 1.0, f"近 {n} 根涨 {r:+.1%}，超过进场线 {enter:+.1%}，梭！"
    return 0.0, f"近 {n} 根涨 {r:+.1%}，没过进场线 {enter:+.1%}，观望"


def macd_lines(closes, fast: int, slow: int, signal: int) -> tuple[list[float], list[float]]:
    """(DIF, DEA)，从 DEA 第一个有效值开始对齐。DIF = EMA(fast) - EMA(slow)，DEA = EMA(DIF, signal)。"""
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef[slow - 1 :], es[slow - 1 :])]
    dea = ema(dif, signal)
    return dif[signal - 1 :], dea[signal - 1 :]


def _macd_periods(params) -> tuple[int, int, int]:
    fast, slow = sorted((max(1, int(params["fast"])), max(1, int(params["slow"]))))
    return fast, slow, max(1, int(params["signal"]))


@register(
    "macd",
    title="MACD 金叉死叉",
    school="动量派",
    summary="DIF（快慢 EMA 之差）上穿 DEA（DIF 的均线）= 金叉上车，下穿 = 死叉下车；开 zero_filter 只做零轴上方的金叉。",
    defaults={"fast": 12, "slow": 26, "signal": 9, "zero_filter": False},
    warmup=lambda p: 3 * (max(int(p["fast"]), int(p["slow"])) + int(p["signal"])) + 10,
    timeframes=("1d",),
)
def macd(candles, position, params, ctx):
    fast, slow, sig = _macd_periods(params)
    dif, dea = macd_lines([c.close for c in candles], fast, slow, sig)
    d, e = dif[-1], dea[-1]
    fresh = len(dif) > 1 and dif[-2] <= dea[-2]  # 上一根还在 DEA 下方 = 刚金叉
    if d <= e:
        return 0.0, ("死叉，跑！" if position >= HELD else "DIF 在 DEA 下方，空仓观望")
    if params["zero_filter"] and d <= 0:
        return 0.0, "金叉但在零轴下方，反弹不追"
    if fresh:
        return 1.0, "零轴上方金叉，梭！" if d > 0 else "金叉，梭！"
    return 1.0, "DIF 在 DEA 上方，拿住"


@register(
    "momentum_vol",
    title="波动率缩放动量",
    school="动量派",
    summary="动量为正才上车，仓位 = 目标波动率 ÷ 最近实际波动率（最多满仓），行情越疯仓位越轻；仓位差不到 band 就不调，省手续费。",
    defaults={"lookback": 20, "enter": 0.0, "exit": 0.0, "vol_window": 30, "target_vol": 0.6, "band": 0.25},
    warmup=lambda p: max(int(p["lookback"]), int(p["vol_window"])) + 1,
    timeframes=("1d",),
)
def momentum_vol(candles, position, params, ctx):
    n = max(1, int(params["lookback"]))
    enter, exit_ = _band(params)
    r = _ret(candles, n)
    if r is None:
        return 0.0, "价格数据异常，空仓"
    held = position > 0.05
    if not (r >= exit_ if held else r > enter):
        return 0.0, (f"近 {n} 根涨 {r:+.1%}，动量没了，跑！" if held else f"近 {n} 根涨 {r:+.1%}，观望")
    w = max(2, int(params["vol_window"]))
    cl = [c.close for c in candles[-w - 1 :]]
    lr = [math.log(b / a) for a, b in zip(cl, cl[1:]) if a > 0 and b > 0]
    m = sum(lr) / len(lr)
    vol = math.sqrt(sum((x - m) ** 2 for x in lr) / max(1, len(lr) - 1) * YEAR_MS / timeframe_ms(ctx.timeframe))
    want = 1.0 if vol <= 0 else min(1.0, float(params["target_vol"]) / vol)
    if held and abs(want - position) < float(params["band"]):
        return position, f"近 {n} 根涨 {r:+.1%}，年化波动 {vol:.0%}，仓位 {position:.0%} 不折腾"
    return want, f"近 {n} 根涨 {r:+.1%}，年化波动 {vol:.0%}，仓位调到 {want:.0%}"
