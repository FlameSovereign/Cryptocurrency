"""趋势派：涨了才追，跌了就跑，不猜顶也不抄底。

- sma_cross：均线金叉死叉，快线在慢线上方就拿着
- donchian：海龟突破，收盘创 N 根新高上车、跌破 M 根新低下车
- supertrend：ATR 通道趋势，收盘站上 / 跌破一条“只进不退”的 ATR 止损线时翻多 / 翻空

都是只做多、要么满仓要么空仓，天生就是梭哈的形状。共同的毛病：横盘震荡时反复上车下车，
每来回一次约 0.5%（Gate 手续费 0.2% × 2 + 滑点）；趋势掉头时要先回吐一截利润才下车。
"""
from __future__ import annotations

from .. import indicators as ind
from . import register

HELD = 0.5  # 仓位过半才算“在车上”，卖剩的零头不算
MAX_WARMUP = 990  # Gate 一页 1000 根，留点余量

_MA = {"sma": ind.sma, "ema": ind.ema}


def _check_warmup(name: str, n: int) -> int:
    if n > MAX_WARMUP:
        raise ValueError(f"{name} 需要 {n} 根 K 线预热，超过上限 {MAX_WARMUP}（Gate 一页 1000 根），把周期调小点")
    return n


# ---------------- 均线金叉死叉 ----------------

def _cross_params(params) -> tuple[int, int, str]:
    fast, slow = int(params["fast"]), int(params["slow"])
    kind = str(params["ma"]).strip().lower()
    if kind not in _MA:
        raise ValueError(f"sma_cross 的 ma 只能是 'sma' 或 'ema'，收到 {params['ma']!r}")
    if not 1 <= fast < slow:
        raise ValueError(f"sma_cross 要求 1 <= fast < slow，收到 fast={fast}, slow={slow}")
    return fast, slow, kind


def _cross_warmup(params) -> int:
    _, slow, kind = _cross_params(params)
    # SMA 只看最近 slow 根，多 1 根用来判断是不是刚交叉；
    # EMA 有记忆，要 3 倍周期让种子（开头那段 SMA）的影响衰减到约 0.25%
    return _check_warmup("sma_cross", slow + 2 if kind == "sma" else 3 * slow + 10)


@register(
    "sma_cross",
    title="均线金叉死叉",
    school="趋势派",
    summary="快均线在慢均线上方就梭哈拿着，跌到下方就清仓。最老牌的趋势跟踪：大牛市吃主升浪、熊市躲大跌，横盘时来回挨手续费。",
    defaults={"fast": 50, "slow": 200, "ma": "sma"},
    warmup=_cross_warmup,
    timeframes=("1d",),
)
def sma_cross(candles, position, params, ctx):
    fast, slow, kind = _cross_params(params)
    px = ind.closes(candles)
    f, s = _MA[kind](px, fast), _MA[kind](px, slow)
    gap = f[-1] / s[-1] - 1 if s[-1] else 0.0
    was_above = f[-2] > s[-2]
    if gap > 0:
        return 1.0, (f"金叉！快线上穿慢线（{gap:+.1%}），梭！" if not was_above else f"快线在慢线上方（{gap:+.1%}），拿住")
    return 0.0, (f"死叉！快线跌破慢线（{gap:+.1%}），跑！" if was_above else f"快线在慢线下方（{gap:+.1%}），空仓等金叉")


# ---------------- 海龟突破 ----------------

def _donchian_params(params) -> tuple[int, int]:
    entry, exit_ = int(params["entry"]), int(params["exit"])
    if entry < 1 or exit_ < 1:
        raise ValueError(f"donchian 的 entry / exit 必须 >= 1，收到 entry={entry}, exit={exit_}")
    return entry, exit_


@register(
    "donchian",
    title="海龟突破",
    school="趋势派",
    summary="收盘价突破前 entry 根 K 线的最高价就梭，跌破前 exit 根的最低价就跑，夹在中间就维持现状。海龟交易法的唐奇安通道。",
    defaults={"entry": 20, "exit": 10},
    warmup=lambda p: _check_warmup("donchian", max(_donchian_params(p)) + 1),
    timeframes=("1d",),
)
def donchian(candles, position, params, ctx):
    entry, exit_ = _donchian_params(params)
    close = candles[-1].close
    # 只和“之前”的 K 线比，最新这根自己的最高价不算，否则永远突破不了
    hi = max(c.high for c in candles[-entry - 1 : -1])
    lo = min(c.low for c in candles[-exit_ - 1 : -1])
    if close > hi:
        return 1.0, f"收盘 {close:.6g} 突破前 {entry} 根最高 {hi:.6g}，梭！"
    if close < lo:
        return 0.0, f"收盘 {close:.6g} 跌破前 {exit_} 根最低 {lo:.6g}，跑！"
    if position >= HELD:
        return 1.0, f"还在通道里，没破离场线 {lo:.6g}，拿住"
    return 0.0, f"还在通道里，没过突破线 {hi:.6g}，空仓等突破"


# ---------------- Supertrend ----------------

def _st_params(params) -> tuple[int, float]:
    n, mult = int(params["period"]), float(params["mult"])
    if n < 1 or mult <= 0:
        raise ValueError(f"supertrend 要求 period >= 1、mult > 0，收到 period={n}, mult={mult}")
    return n, mult


def supertrend_state(candles, period: int, mult: float) -> tuple[bool, float, int | None]:
    """在窗口内从头递推 Supertrend，返回 (是否多头, 当前那条线, 上次翻转距今几根)。

    多头时那条线是止损线（下轨），空头时是翻多线（上轨）。窗口开头一律当空头起步；
    递推对开头不敏感，warmup 够长时和用更长历史算出来的方向一致（见 _st_warmup）。
    """
    a = ind.atr(candles, period)
    up = dn = None
    bull, flip = False, None
    for i in range(period - 1, len(candles)):
        c = candles[i]
        mid = (c.high + c.low) / 2
        bu, bl = mid + mult * a[i], mid - mult * a[i]
        if up is None:
            up, dn = bu, bl
            continue
        pc = candles[i - 1].close
        # 上轨只降不升、下轨只升不降；上一根收盘已经穿过去了才重置
        up = bu if bu < up or pc > up else up
        dn = bl if bl > dn or pc < dn else dn
        if bull and c.close < dn:
            bull, flip = False, i
        elif not bull and c.close > up:
            bull, flip = True, i
    return bull, (dn if bull else up), (None if flip is None else len(candles) - 1 - flip)


def _st_warmup(params) -> int:
    n, _ = _st_params(params)
    return _check_warmup("supertrend", 10 * n + 50)


@register(
    "supertrend",
    title="Supertrend 趋势线",
    school="趋势派",
    summary="以 (最高+最低)/2 ± mult×ATR 画一条只进不退的止损线，收盘站上去翻多梭哈、跌破就翻空清仓。",
    defaults={"period": 10, "mult": 3.0},
    warmup=_st_warmup,
    timeframes=("1d",),
)
def supertrend(candles, position, params, ctx):
    n, mult = _st_params(params)
    bull, line, ago = supertrend_state(candles, n, mult)
    close = candles[-1].close
    if bull:
        if ago == 0:
            return 1.0, f"收盘站上 ATR 上轨，翻多，梭！止损线 {line:.6g}"
        return 1.0, f"多头第 {ago + 1} 根，止损线 {line:.6g}（距离 {close / line - 1:+.1%}），拿住"
    if ago == 0:
        return 0.0, f"收盘跌破止损线，翻空，跑！翻多线 {line:.6g}"
    return 0.0, f"空头趋势，翻多线 {line:.6g}（还差 {line / close - 1:+.1%}），空仓"
