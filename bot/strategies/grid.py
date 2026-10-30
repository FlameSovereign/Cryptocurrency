"""网格派：在一个价格区间里低吸高抛，赚来回震荡的钱。

- grid：拿前 lookback 根 K 线的 [最低价, 最高价] 当网格区间，切成 levels 格，价格每跌下一格就
  多买一格仓位、每涨上一格就卖掉一格：目标仓位 ≈ (上沿 − 收盘) / (上沿 − 下沿)，按格取整。
  和 Gate 现货网格机器人一个思路，区别是：
  · 区间每根 K 线按前 lookback 根的高低点滚动更新（最新这根不算，否则永远破不了位）。不用
    “均线 ± k 倍标准差”：高低点画的线落在真实成交过的价位上，只在破位或旧高低点滚出窗口时才挪，
    更像一张挂好不动的网（同参数回测在加密币上也好一些）；
  · 只在 K 线收盘时看一眼（机器人每小时才醒一次），盘中来回穿格子的钱赚不到；
  · 一格买卖来回要付约 0.5%（手续费 0.2% × 2 + 滑点），min_step 规定格子最小间距，区间太窄就少分几格。
  跌破下沿（breakout）："hold" 满仓扛着（Gate 网格的止损价是选填的，不填就是这样）；"stop" 止损
  空仓，等 lookback 根 K 线里不再创新低（区间重新筑底）再重开网格。涨破上沿：卖光，空仓看它飞。

迟滞：真实网格同一格里的持仓取决于价格从哪边来（从上面跌下来是 k 格，从下面涨上来是 k+1 格，
买在一条线、卖在上一条线）。这里把当前仓位夹在 [k/n, (k+1)/n] 里，效果一样，不用藏状态；价格在
格子里晃带来的仓位漂移（不到半格）不去修剪，和真实网格“不穿线不成交”一致。

回测结论（Gate 真实 K 线，手续费 0.2%）：网格是“卖波动”的生意，赚得少、亏得多。60 天一段拆开看，
震荡的段里网格过半跑赢半仓囤币，但只多赚零点几到一个多点；单边涨（早早卖飞）和单边跌（hold 一路
接到底）的段里多半输，每段少 3~7 个点。加密币趋势多，整体打不过半仓囤币（hodl --size 0.5：网格
平均也就半仓，该和它比，它本身也在做再平衡）；股票代币震荡多，4h 上小胜。格子越密越亏：教科书的
20 根区间 + 10 格在 1h / 4h 上每个加密币成交约四千笔、全样本中位数亏八九成。stop 是熊市工具：单边跌时
回撤减半，单边涨时一直空仓。
"""
from __future__ import annotations

import math

from .. import indicators as ind
from . import register

MAX_WARMUP = 990  # Gate 一页 1000 根，留点余量
_BREAKOUT = {"hold": "满仓扛着", "stop": "止损空仓"}
_EPS = 1e-9  # 浮点误差余量：3 × 0.1 不该被取整成 0.2999


def _check_warmup(name: str, n: int) -> int:
    if n > MAX_WARMUP:
        raise ValueError(f"{name} 需要 {n} 根 K 线预热，超过上限 {MAX_WARMUP}（Gate 一页 1000 根），把 lookback 调小点")
    return n


def _grid_params(params) -> tuple[int, int, str, float]:
    lookback, levels = int(params["lookback"]), int(params["levels"])
    breakout = str(params["breakout"]).strip().lower()
    min_step = float(params["min_step"])
    if lookback < 2:
        raise ValueError(f"grid 的 lookback 必须 >= 2，收到 {lookback}")
    if levels < 1:
        raise ValueError(f"grid 的 levels（格数）必须 >= 1，收到 {levels}")
    if breakout not in _BREAKOUT:
        raise ValueError(f"grid 的 breakout 只能是 'hold'（跌破下沿满仓扛着）或 'stop'（跌破下沿止损），收到 {params['breakout']!r}")
    if not 0 <= min_step < 1:
        raise ValueError(f"grid 的 min_step 是小数（0.01 = 每格至少 1%，0 = 不限制），收到 {min_step:g}")
    return lookback, levels, breakout, min_step


def _grid_warmup(params) -> int:
    lookback, _, breakout, _ = _grid_params(params)
    # 区间只看前 lookback 根，没有记忆；stop 模式还要回看最近 lookback 根有没有各自跌破过区间
    return _check_warmup("grid", (2 * lookback if breakout == "stop" else lookback) + 1)


def grid_range(candles, lookback: int) -> tuple[float, float]:
    """前 lookback 根 K 线（不含最新这根）的 (最低价, 最高价)。"""
    prev = candles[-lookback - 1 : -1]
    return min(c.low for c in prev), max(c.high for c in prev)


def grid_cells(lower: float, upper: float, price: float, levels: int, min_step: float) -> int:
    """实际分几格：每格间距至少是现价的 min_step，区间太窄就少分几格，最少 1 格。"""
    if min_step <= 0 or price <= 0:
        return levels
    return max(1, min(levels, int((upper - lower) / (price * min_step) + _EPS)))


def grid_band(depth: float, n: int) -> tuple[float, float]:
    """depth = 价格在网格里有多深（0 = 上沿，1 = 下沿）。返回真实网格此处可能的持仓范围 [lo, hi]。

    价格落在从上往下第 k+1 格时，真实网格要么持有 k/n（从上面跌下来，这格的买单还没成交），
    要么 (k+1)/n（从下面涨上来，这格的卖单还没成交）。正好压在线上时两者相等。
    """
    d = min(1.0, max(0.0, depth))
    return math.floor(d * n + _EPS) / n, math.ceil(d * n - _EPS) / n


def breakdown_ago(candles, lookback: int) -> int | None:
    """最近 lookback 根里，最近一次“收盘跌破它之前 lookback 根的最低价”是几根之前（0 = 最新这根）。"""
    floor_ = ind.lowest([c.low for c in candles], lookback)  # floor_[i] = 第 i-lookback+1 ~ i 根的最低价
    last = len(candles) - 1
    for ago in range(lookback):
        i = last - ago
        if i < 1:
            break
        ref = floor_[i - 1]
        if ref is not None and candles[i].close < ref:
            return ago
    return None


# 默认参数只用样本内（前 60%）挑，32 组里选的（含一组均线带对照）：200 根区间、3 格、每格至少 1%、
# 跌破不止损，在 27 个（标的 × 周期）里赢半仓囤币 9 个，最多；并列的“5 格 + 每格至少 2%”在 6 个
# （周期 × 篮子）分组里有 5 个中位数更低。格子越密越差：同样 200 根区间，20 格每年手续费约占本金 6%，
# 3 格约 3%。stop 样本内只在 1h 加密币（这段全是熊市）上赢，整体不如 hold，所以默认不止损。
@register(
    "grid",
    title="网格交易",
    school="网格派",
    summary="拿前 lookback 根 K 线的最高/最低价当区间、切 levels 格：每跌一格加一格仓位，每涨一格卖一格，"
    "每格间距至少 min_step（不够就少分几格，免得赚的不够交手续费）。跌破下沿 breakout='hold' 满仓扛着、"
    "'stop' 止损空仓等重新筑底；涨破上沿卖光。震荡市小赚，单边市不是卖飞就是套牢。",
    defaults={"lookback": 200, "levels": 3, "breakout": "hold", "min_step": 0.01},
    warmup=_grid_warmup,
    timeframes=("1d", "4h"),
)
def grid(candles, position, params, ctx):
    lookback, levels, breakout, min_step = _grid_params(params)
    close = candles[-1].close
    lower, upper = grid_range(candles, lookback)
    if not (close > 0 and lower > 0):
        return 0.0, "价格数据异常，空仓"
    if breakout == "stop":
        ago = breakdown_ago(candles, lookback)
        if ago == 0:
            return 0.0, f"收盘 {close:.6g} 跌破网格下沿 {lower:.6g}，止损，跑！"
        if ago is not None:
            return 0.0, f"{ago} 根前跌破过网格下沿，止损冷静期：再 {lookback - ago} 根不创新低才重开网格"
    if close >= upper:
        return 0.0, f"收盘 {close:.6g} 涨到网格上沿 {upper:.6g} 以上，卖光，看它飞"
    if close < lower:  # 只有 hold 模式会走到这里
        return 1.0, f"收盘 {close:.6g} 跌破网格下沿 {lower:.6g}，满仓扛着"
    n = grid_cells(lower, upper, close, levels, min_step)
    depth = (upper - close) / (upper - lower)
    lo, hi = grid_band(depth, n)
    # 真实网格每格买的是固定数量的币，价格在格子里晃，仓位占比也跟着晃，但不穿线就不成交。
    # 所以仓位离这格的范围不到半格都算“还在这格”，不为漂移去修剪仓位（日线上这能省掉六成多的零碎成交）
    tol = 0.5 / n
    target = position if lo - tol < position < hi + tol else min(max(position, lo), hi)
    cell = min(n, int(depth * n + _EPS) + 1)
    where = f"网格 {lower:.6g}~{upper:.6g} 共 {n} 格，价格在从上往下第 {cell} 格"
    if target > position + _EPS:
        return target, f"{where}，低吸：加仓到 {target:.0%}"
    if target < position - _EPS:
        return target, f"{where}，高抛：减仓到 {target:.0%}"
    return target, f"{where}，仓位 {position:.0%} 正好在这格的范围里，不动"
