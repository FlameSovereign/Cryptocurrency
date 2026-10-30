"""动量派：涨着的东西倾向于继续涨，跌着的倾向于继续跌——直到某天不再如此。

- tsmom：时间序列动量，过去 N 根 K 线涨幅过了进场线就梭，跌破离场线就跑
- macd：MACD 金叉死叉（DIF 上穿 DEA），国内炒股软件里人手一个
- momentum_vol：动量为正才上车，仓位按波动率打折（行情越疯仓位越轻）

都只做多。共同的毛病：横盘震荡时来回打脸，每进出一次约 0.5%（Gate 手续费 0.2% × 2 + 滑点）；
顶部要先回吐一截利润才下车。参数都按“根 K 线”算，换周期要跟着换算（1d 的 14 根 ≈ 4h 的 84 根）。
"""
from __future__ import annotations

import math

from ..candles import YEAR_MS, timeframe_ms
from ..indicators import closes, ema
from . import register

HELD = 0.5  # 仓位过半才算“在车上”，卖剩的零头不算
MAX_WARMUP = 990  # Gate 一页 1000 根，留点余量


def _check_warmup(name: str, n: int) -> int:
    if n > MAX_WARMUP:
        raise ValueError(f"{name} 需要 {n} 根 K 线预热，超过上限 {MAX_WARMUP}（Gate 一页 1000 根），把周期调小点")
    return n


def _ret(candles, n: int) -> float | None:
    """最新收盘价相对 n 根之前收盘价的涨跌幅（candles 至少 n + 1 根）。"""
    past = candles[-1 - n].close
    return candles[-1].close / past - 1.0 if past > 0 else None


# ---------------- 时间序列动量 ----------------

def _tsmom_params(params, name: str = "tsmom") -> tuple[int, float, float]:
    n, enter, exit_ = int(params["lookback"]), float(params["enter"]), float(params["exit"])
    if n < 1:
        raise ValueError(f"{name} 的 lookback 必须 >= 1，收到 {n}")
    if exit_ > enter:
        raise ValueError(f"{name} 的 exit（{exit_}）不能高于 enter（{enter}），不然刚上车就下车")
    return n, enter, exit_


@register(
    "tsmom",
    title="时间序列动量",
    school="动量派",
    summary="过去 lookback 根 K 线涨超 enter 才梭，跌破 exit 就跑，两条线中间维持现状（少在零轴附近来回挨手续费）。"
            "默认按日线调：14 根（两周）、+5% 进场、跌回 0 离场；4h 用 lookback=84。1h 上试过的参数样本内夏普全是负的，别用。",
    defaults={"lookback": 14, "enter": 0.05, "exit": 0.0},
    warmup=lambda p: _check_warmup("tsmom", _tsmom_params(p)[0] + 1),
    timeframes=("1d",),
)
def tsmom(candles, position, params, ctx):
    n, enter, exit_ = _tsmom_params(params)
    r = _ret(candles, n)
    if r is None:
        return 0.0, "价格数据异常，空仓"
    if position >= HELD:
        if r >= exit_:
            return 1.0, f"近 {n} 根涨跌 {r:+.1%}，没破离场线 {exit_:+.1%}，拿住"
        return 0.0, f"近 {n} 根涨跌 {r:+.1%}，跌破离场线 {exit_:+.1%}，动量没了，跑！"
    if r > enter:
        return 1.0, f"近 {n} 根涨跌 {r:+.1%}，超过进场线 {enter:+.1%}，梭！"
    return 0.0, f"近 {n} 根涨跌 {r:+.1%}，没过进场线 {enter:+.1%}，观望"


# ---------------- MACD ----------------

def _macd_params(params) -> tuple[int, int, int, bool]:
    fast, slow, sig, zf = int(params["fast"]), int(params["slow"]), int(params["signal"]), params["zero_filter"]
    if not 1 <= fast < slow or sig < 1:
        raise ValueError(f"macd 要求 1 <= fast < slow、signal >= 1，收到 fast={fast}, slow={slow}, signal={sig}")
    if not isinstance(zf, (bool, int)):  # 配置里写成 "false" 字符串会被当成真
        raise ValueError(f"macd 的 zero_filter 要写 true / false（不带引号），收到 {zf!r}")
    return fast, slow, sig, bool(zf)


def macd_lines(values, fast: int, slow: int, signal: int) -> tuple[list[float], list[float]]:
    """(DIF, DEA)，只保留两条线都有值的部分，末尾对齐输入。

    DIF = EMA(fast) - EMA(slow)，DEA = EMA(DIF, signal)。柱子（MACD 柱）= 2 × (DIF - DEA)。
    """
    ef, es = ema(values, fast), ema(values, slow)
    dif = [a - b for a, b in zip(ef[slow - 1 :], es[slow - 1 :])]
    dea = ema(dif, signal)
    return dif[signal - 1 :], dea[signal - 1 :]


def _macd_warmup(params) -> int:
    _, slow, sig, _ = _macd_params(params)
    # 慢线和 DEA 都是 EMA，各给约 3 倍周期让种子的影响衰减掉，再多 10 根判断刚交叉
    return _check_warmup("macd", 3 * (slow + sig) + 10)


@register(
    "macd",
    title="MACD 金叉死叉",
    school="动量派",
    summary="DIF（快慢 EMA 之差）在 DEA（DIF 的均线）上方就梭哈拿着，死叉清仓；zero_filter 只认零轴上方的金叉（阴跌里的假金叉靠它挡）。"
            "默认 18/39/14（经典 12/26/9 放慢 1.5 倍）+ 零轴过滤，日线用；4h 用 48/104/36。"
            "经典 12/26/9 放在 1h/4h 上金叉死叉太频繁，基本是给交易所打工。",
    defaults={"fast": 18, "slow": 39, "signal": 14, "zero_filter": True},
    warmup=_macd_warmup,
    timeframes=("1d",),
)
def macd(candles, position, params, ctx):
    fast, slow, sig, zero_filter = _macd_params(params)
    dif, dea = macd_lines(closes(candles), fast, slow, sig)
    d, e = dif[-1], dea[-1]
    fresh = dif[-2] <= dea[-2]  # 上一根还在 DEA 下方 = 刚金叉
    if d <= e:
        if position >= HELD:
            return 0.0, f"死叉（DIF {d:.4g} < DEA {e:.4g}），跑！"
        return 0.0, f"DIF {d:.4g} 在 DEA {e:.4g} 下方，空仓等金叉"
    if zero_filter and d <= 0:
        return 0.0, f"金叉但 DIF {d:.4g} 还在零轴下方，反弹不追"
    if fresh:
        return 1.0, ("零轴上方金叉，梭！" if d > 0 else "金叉，梭！")
    if position < HELD:  # 没有刚交叉，但条件刚满足（比如 DIF 刚站上零轴）
        return 1.0, f"DIF {d:.4g} 在 DEA {e:.4g} 上方" + ("、站上零轴" if zero_filter else "") + "，梭！"
    return 1.0, f"DIF {d:.4g} 在 DEA {e:.4g} 上方，拿住"


# ---------------- 波动率缩放动量 ----------------

def _vol_params(params) -> tuple[int, float, float, int, float, float]:
    n, enter, exit_ = _tsmom_params(params, "momentum_vol")
    w, target, band = int(params["vol_window"]), float(params["target_vol"]), float(params["band"])
    if w < 2 or target <= 0 or not 0 <= band < 1:
        raise ValueError(f"momentum_vol 要求 vol_window >= 2、target_vol > 0、0 <= band < 1，"
                         f"收到 vol_window={w}, target_vol={target}, band={band}")
    return n, enter, exit_, w, target, band


def realized_vol(candles, window: int, timeframe: str) -> float:
    """最近 window 根 K 线对数收益的年化标准差（candles 至少 window + 1 根）。"""
    px = closes(candles[-window - 1 :])
    lr = [math.log(b / a) for a, b in zip(px, px[1:]) if a > 0 and b > 0]
    if len(lr) < 2:
        return 0.0
    m = sum(lr) / len(lr)
    var = sum((x - m) ** 2 for x in lr) / (len(lr) - 1)
    return math.sqrt(var * YEAR_MS / timeframe_ms(timeframe))


@register(
    "momentum_vol",
    title="波动率缩放动量",
    school="动量派",
    summary="进出场和 tsmom 一样，但仓位 = target_vol ÷ 最近 vol_window 根的年化波动（最多满仓），行情越疯仓位越轻；"
            "差不到 band 不调仓。回撤大约砍半、牛市少赚一大截，不是梭哈形状，求稳用。4h 用 lookback=84、vol_window=180。",
    defaults={"lookback": 14, "enter": 0.05, "exit": 0.0, "vol_window": 30, "target_vol": 0.4, "band": 0.1},
    warmup=lambda p: _check_warmup("momentum_vol", max(_vol_params(p)[0], _vol_params(p)[3]) + 1),
    timeframes=("1d",),
)
def momentum_vol(candles, position, params, ctx):
    n, enter, exit_, w, target, band = _vol_params(params)
    r = _ret(candles, n)
    if r is None:
        return 0.0, "价格数据异常，空仓"
    held = position > 0.05  # 仓位本来就是零点几，不能用 HELD
    if not (r >= exit_ if held else r > enter):
        if held:
            return 0.0, f"近 {n} 根涨跌 {r:+.1%}，跌破离场线 {exit_:+.1%}，跑！"
        return 0.0, f"近 {n} 根涨跌 {r:+.1%}，没过进场线 {enter:+.1%}，观望"
    vol = realized_vol(candles, w, ctx.timeframe)
    want = 1.0 if vol <= 0 else min(1.0, target / vol)
    if held and abs(want - position) < band:
        return position, f"近 {n} 根涨跌 {r:+.1%}，年化波动 {vol:.0%}，该 {want:.0%} 仓，差得不多，维持 {position:.0%}"
    return want, f"近 {n} 根涨跌 {r:+.1%}，年化波动 {vol:.0%}，仓位调到 {want:.0%}"
