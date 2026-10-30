"""趋势派：涨了才追，跌了就跑，不猜顶也不抄底。

- sma_cross：均线金叉死叉，快线在慢线上方就拿着
- donchian：海龟突破，收盘创 N 根新高上车、跌破 M 根新低下车
- supertrend：ATR 通道趋势，收盘站上 / 跌破一条“只进不退”的 ATR 止损线时翻多 / 翻空

都是只做多、要么满仓要么空仓，天生就是梭哈的形状。共同的毛病：横盘震荡时反复上车下车，
每来回一次约 0.5%（Gate 手续费 0.2% × 2 + 滑点）；趋势掉头时要先回吐一截利润才下车。

研究结论（research.harness，Gate 缓存到 2026-10，成本按 harness 默认）：
- 只用 1d。1h / 4h 上三个都跑输囤币，1h 一年进出两三百次，光手续费就把本金磨掉一大半。
- 1d 加密币（2017 起）：全样本 5 个币里赢囤币 4~5 个，最大回撤从 77~96% 降到 52~84%。
  但样本外（最后 40%，约 2022-11 起）只和囤币打平：SOL / DOGE / GT 赢，BTC / ETH 输，
  也只有 GT 赢过 p90 猴子。它的本事是熊市少亏，不是牛市多赚。
- 股票代币（xStocks，2025-07 才上线）日线历史太短，全输囤币，别拿来跑。
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
    # SMA 只看最近 slow 根，多 2 根用来判断是不是刚交叉。
    # EMA 有记忆：3 倍慢线周期时，窗口开头那段 SMA 种子还剩 1~2% 权重。实测 Gate 缓存数据（9 个标的
    # × 1h/4h/1d × 10 组快慢线），和用全部历史算出来的多空结论一致率最差 99.2%、通常 99.6%+；
    # 只给 2 倍周期会掉到 96.6%。
    return _check_warmup("sma_cross", slow + 2 if kind == "sma" else 3 * slow + 10)


# 默认参数只用样本内（前 60%）挑：日线 EMA 5/60 在 5 个加密币上都跑赢囤币，手续费翻倍也一样；
# 经典 50/200 只赢 4 个、回撤和囤币差不多。1h / 4h 上 20 组参数没有一组跑赢囤币。
@register(
    "sma_cross",
    title="均线金叉死叉",
    school="趋势派",
    summary="快均线在慢均线上方就梭哈拿着，跌到下方就清仓。默认 EMA 5/60 配日线（约等于收盘站上 60 日均线就上车）：熊市躲大跌，横盘时来回挨手续费。",
    defaults={"fast": 5, "slow": 60, "ma": "ema"},
    warmup=_cross_warmup,
    timeframes=("1d",),
)
def sma_cross(candles, position, params, ctx):
    fast, slow, kind = _cross_params(params)
    px = ind.closes(candles)
    f, s = _MA[kind](px, fast), _MA[kind](px, slow)
    if len(px) < 2 or s[-2] is None or not s[-1]:
        return 0.0, f"K 线不够算 {slow} 根均线，先空仓"
    gap = f[-1] / s[-1] - 1
    was_above = f[-2] > s[-2]
    if gap > 0:
        if not was_above:
            return 1.0, f"金叉！快线上穿慢线（{gap:+.1%}），梭！"
        if position >= HELD:
            return 1.0, f"快线在慢线上方（{gap:+.1%}），拿住"
        return 1.0, f"快线在慢线上方（{gap:+.1%}），趋势还在，上车"
    if was_above:
        return 0.0, f"死叉！快线跌破慢线（{gap:+.1%}），跑！"
    if position >= HELD:
        return 0.0, f"快线在慢线下方（{gap:+.1%}），下车"
    return 0.0, f"快线在慢线下方（{gap:+.1%}），空仓等金叉"


# ---------------- 海龟突破 ----------------

def _donchian_params(params) -> tuple[int, int]:
    entry, exit_ = int(params["entry"]), int(params["exit"])
    if entry < 1 or exit_ < 1:
        raise ValueError(f"donchian 的 entry / exit 必须 >= 1，收到 entry={entry}, exit={exit_}")
    return entry, exit_


# 默认就是经典海龟 20/10：样本内日线 5 个加密币全赢囤币（手续费翻倍也是），和 20/5 打平，
# 但在 4h / 1h 上比 20/5 亏得少，所以留经典值。
@register(
    "donchian",
    title="海龟突破",
    school="趋势派",
    summary="收盘价突破前 entry 根 K 线的最高价就梭，跌破前 exit 根的最低价就跑，夹在中间就维持现状。经典海龟 20/10，配日线。",
    defaults={"entry": 20, "exit": 10},
    warmup=lambda p: _check_warmup("donchian", max(_donchian_params(p)) + 1),
    timeframes=("1d",),
)
def donchian(candles, position, params, ctx):
    entry, exit_ = _donchian_params(params)
    if len(candles) < max(entry, exit_) + 1:
        return 0.0, f"K 线不够 {max(entry, exit_) + 1} 根，先空仓"
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
    if n < 1 or not mult > 0:
        raise ValueError(f"supertrend 要求 period >= 1、mult > 0，收到 period={n}, mult={mult}")
    return n, mult


def supertrend_state(candles, period: int, mult: float) -> tuple[bool, float | None, int | None]:
    """在窗口内从头递推 Supertrend，返回 (是否多头, 当前那条线, 上次翻转距今几根)。

    标准写法（和 TradingView ta.supertrend 一致）：中价 (高+低)/2 ± mult × Wilder ATR 得到上下轨；
    上轨只降不升、下轨只升不降，上一根收盘穿过去了才重置；收盘站上上轨翻多、跌破下轨翻空。
    多头时返回下轨（止损线），空头时返回上轨（翻多线）。窗口开头一律当空头起步。
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
        up = bu if bu < up or pc > up else up
        dn = bl if bl > dn or pc < dn else dn
        if bull and c.close < dn:
            bull, flip = False, i
        elif not bull and c.close > up:
            bull, flip = True, i
    line = dn if bull else up
    return bull, line, (None if flip is None else len(candles) - 1 - flip)


def _st_warmup(params) -> int:
    """Supertrend 是递推出来的（路径依赖），窗口太短时开头“空头起步”的假设会一直影响到最后一根。

    要多长取决于两件事：ATR 种子衰减（约 period 的几倍）和收盘多久能碰到一次 mult 倍 ATR 外的轨道
    （mult 越大越久，超过 5 以后轨道宽到很少被碰到，要多给很多）。实测 Gate 缓存数据（9 个标的 ×
    1h/4h/1d，period 5~30）：最后一根的多空和用全部历史递推的结果相比，mult <= 5 时一致率最差 99.96%，
    mult 5.5~8 时最差 99.4%；老写法 10×period + 50 在 period=10、mult=5 时只有 96.6%。
    """
    n, mult = _st_params(params)
    return _check_warmup("supertrend", int(5 * n + 50 * mult + 50 + 150 * max(0.0, mult - 5)))


# 默认就是 TradingView 的 10/3：样本内日线 mult=3 这一排（period 7~20）都是 5 个加密币赢 4 个
# （都输在 DOGE 2021 那波暴涨上），10/3 在 DOGE 上输得最少、手续费翻倍照样赢 4 个，4h / 1h 上也是这排最好的。
@register(
    "supertrend",
    title="Supertrend 趋势线",
    school="趋势派",
    summary="以 (最高+最低)/2 ± mult×ATR 画一条只进不退的止损线，收盘站上去翻多梭哈、跌破就翻空清仓。默认 TradingView 的 10/3，配日线。",
    defaults={"period": 10, "mult": 3.0},
    warmup=_st_warmup,
    timeframes=("1d",),
)
def supertrend(candles, position, params, ctx):
    n, mult = _st_params(params)
    bull, line, ago = supertrend_state(candles, n, mult)
    if line is None:
        return 0.0, f"K 线不够算 {n} 根 ATR，先空仓"
    close = candles[-1].close
    if bull:
        if ago == 0:
            return 1.0, f"收盘站上 ATR 上轨，翻多，梭！止损线 {line:.6g}"
        room = f"（在它上方 {close / line - 1:+.1%}）" if line > 0 else ""
        return 1.0, f"多头第 {ago + 1} 根，止损线 {line:.6g}{room}，" + ("拿住" if position >= HELD else "上车")
    if ago == 0:
        return 0.0, f"收盘跌破止损线，翻空，跑！翻多线 {line:.6g}"
    room = f"（还差 {line / close - 1:+.1%}）" if close > 0 else ""
    return 0.0, f"空头趋势，翻多线 {line:.6g}{room}，" + ("下车" if position >= HELD else "空仓")
