"""网格派（bot/strategies/grid.py）的合成行情测试。

网格和趋势派反着来：跌进区间底部才“上车”（多买几格），涨破区间就卖光。所以这里的“涨势 / 跌势”
场景是：区间里跌到底 -> 重仓，单边涨破上沿 -> 空仓，单边跌破下沿 -> stop 空仓、hold 满仓。
另外验证：和真实挂单网格逐笔一致（迟滞）、预热长度刚好够、同样输入同样输出、格子间距够付手续费。
"""
from __future__ import annotations

import math
import random

import pytest

from bot.backtest import run_backtest
from bot.candles import Candle, timeframe_ms
from bot.strategies import Context, get, normalize
from bot.strategies.grid import MAX_WARMUP, breakdown_ago, grid_band, grid_cells, grid_range

from .helpers import T0, make_candles, wave

TF = "4h"
STEP = timeframe_ms(TF)
HOLD = {"breakout": "hold"}
STOP = {"breakout": "stop"}


def _ctx(window):
    return Context(timeframe=TF, asof_ms=window[-1].ts + STEP, symbol="TEST/USDT")


def _look(params=None):
    s = get("grid")
    return s.lookback(s.resolve(params))


def _call(window, position=0.0, params=None):
    s = get("grid")
    return normalize(s.fn(window, position, s.resolve(params), _ctx(window)))


def _signal(candles, position=0.0, params=None):
    """用最近恰好 warmup 根 K 线问策略要目标仓位（和实盘、回测喂数据的方式一样）。"""
    look = _look(params)
    assert len(candles) >= look
    return _call(candles[-look:], position, params)


def _box(closes, lo=90.0, hi=110.0):
    """每根 K 线都带 lo / hi 的影线：滚动区间永远是 [lo, hi]，方便和固定区间的真实网格对比。"""
    return [Candle(T0 + i * STEP, c, hi, lo, c, 1.0) for i, c in enumerate(closes)]


def _walk(n, seed, drift=0.0, vol=0.02):
    rng = random.Random(seed)
    px, out = 100.0, []
    for _ in range(n):
        px *= math.exp(drift + rng.gauss(0, vol))
        out.append(px)
    return out


# ---------------- 区间里低吸高抛；单边行情 ----------------

@pytest.mark.parametrize("params", [HOLD, STOP])
def test_dip_to_range_bottom_is_long_and_top_is_flat(params):
    p = {**params, "lookback": 50, "levels": 10, "min_step": 0.01}
    c = make_candles(wave(400, base=100, amp=20, period=40), TF)  # 80~120 来回震荡
    look = _look(p)
    trough = max(range(look, len(c)), key=lambda i: -c[i].close)  # 最近一个波谷
    peak = max(range(look, len(c)), key=lambda i: c[i].close)
    for pos in (0.0, 1.0):
        assert _signal(c[: trough + 1], pos, p)[0] >= 0.9  # 跌到区间底部：重仓
        assert _signal(c[: peak + 1], pos, p)[0] <= 0.1  # 涨到区间顶部：基本卖光


@pytest.mark.parametrize("params", [HOLD, STOP])
def test_uptrend_breaking_the_range_sells_out(params):
    # 网格的宿命：单边涨破上沿就卖光，空仓看它飞（和趋势派正好相反）
    c = make_candles([100 * 1.01 ** i for i in range(_look(params) + 30)], TF)
    for pos in (0.0, 0.5, 1.0):
        target, note = _signal(c, pos, params)
        assert target == 0.0 and "上沿" in note


def test_downtrend_breakdown_stop_goes_flat_hold_goes_full():
    c = make_candles([100 * 0.99 ** i for i in range(_look(STOP) + 30)], TF)
    for pos in (0.0, 0.5, 1.0):
        target, note = _signal(c, pos, STOP)
        assert target == 0.0 and "止损" in note
        target, note = _signal(c, pos, HOLD)
        assert target == 1.0 and "扛着" in note


def test_range_uses_previous_bars_only():
    p = {"lookback": 20, "levels": 10, "min_step": 0.0}
    flat = _box([100.0] * 25)

    def with_last(close, high):
        return flat + [Candle(T0 + 25 * STEP, 100.0, high, 95.0, close, 1.0)]

    assert grid_range(with_last(109.0, 200.0), 20) == (90.0, 110.0)
    # 最新这根自己的最高价（200）不进区间：109 离上沿 110 只有半格，基本卖光
    assert _signal(with_last(109.0, 200.0), 1.0, p)[0] == pytest.approx(0.1)
    assert _signal(with_last(110.5, 200.0), 1.0, p)[0] == 0.0


# ---------------- 迟滞：和真实挂单网格逐笔一致 ----------------

def _real_grid(closes, lower, upper, n):
    """参照系：真实的挂单网格。第 j 格（lower + j*step ~ lower + (j+1)*step）跌到下线买一份、涨到上线卖掉。"""
    step = (upper - lower) / n
    lines = [lower + j * step for j in range(n + 1)]
    p0 = closes[0]
    held = [p0 <= lines[j] for j in range(n)]  # 开网格时：现价上方的格子先买好
    out = [sum(held) / n]
    for p in closes[1:]:
        for j in range(n):
            if p <= lines[j]:
                held[j] = True
            elif p >= lines[j + 1]:
                held[j] = False
        out.append(sum(held) / n)
    return out


@pytest.mark.parametrize("n,seed", [(4, 1), (10, 2), (10, 3), (20, 4)])
def test_matches_real_limit_order_grid(n, seed):
    p = {"lookback": 30, "levels": n, "breakout": "hold", "min_step": 0.0}
    look = _look(p)
    rng = random.Random(seed)
    px, path = 100.0, []
    for _ in range(300):  # 围着 100 来回震荡，经常穿线
        px = min(109.9, max(90.1, 100 + 0.8 * (px - 100) + rng.gauss(0, 4)))
        path.append(round(px, 2))
    closes = [100.0] * (look - 1) + path
    c = _box(closes)
    want = _real_grid(path, 90.0, 110.0, n)
    pos, got = 0.0, []
    for k in range(len(path)):
        t = look - 1 + k
        pos = _call(c[t - look + 1 : t + 1], pos, p)[0]  # 假设每次都按目标成交
        got.append(pos)
    assert got == pytest.approx(want, abs=1e-12)
    flips = sum(a != b for a, b in zip(got, got[1:]))
    assert 10 < flips < len(path)  # 真的在交易，但不是每根都动


def test_wiggling_around_a_grid_line_does_not_churn():
    p = {"lookback": 20, "levels": 10, "breakout": "hold", "min_step": 0.0}  # 90~110，每 2 块一条线
    look = _look(p)

    def at(close, pos):
        return _call(_box([100.0] * (look - 1) + [close]), pos, p)[0]

    assert at(100.0, 0.0) == pytest.approx(0.5)  # 正好压在 100 这条线上
    for wiggle in (99.9, 100.1, 99.95, 101.9):
        assert at(wiggle, 0.5) == pytest.approx(0.5)  # 线附近抖来抖去：不动
    assert at(102.1, 0.5) == pytest.approx(0.4)  # 涨过上面一条线（102）：卖一格
    assert at(101.0, 0.4) == pytest.approx(0.4)  # 回落但没碰到 100：不追回来
    assert at(99.9, 0.4) == pytest.approx(0.5)  # 跌破 100：买回这一格，一来一回赚一格 2%


def test_grid_band():
    assert grid_band(0.0, 10) == (0.0, 0.0) and grid_band(1.0, 10) == (1.0, 1.0)
    assert grid_band(0.3, 10) == pytest.approx((0.3, 0.3))  # 3 × 0.1 的浮点误差不该多出一格
    assert grid_band(0.35, 10) == pytest.approx((0.3, 0.4))
    assert grid_band(-0.5, 4) == (0.0, 0.0) and grid_band(1.5, 4) == (1.0, 1.0)


# ---------------- 手续费意识：格子太密就少分几格 ----------------

def test_min_step_merges_cells_when_range_is_narrow():
    assert grid_cells(90, 110, 100, 10, 0.0) == 10
    assert grid_cells(90, 110, 100, 10, 0.01) == 10  # 每格 2%，够
    assert grid_cells(99, 101, 100, 10, 0.01) == 2  # 区间才 2%：只能分 2 格
    assert grid_cells(99.5, 100.5, 100, 10, 0.01) == 1  # 比一格还窄：至少留 1 格
    p = {"lookback": 20, "levels": 10, "breakout": "hold"}
    tight = [Candle(T0 + i * STEP, 100.0, 101.0, 99.0, 100.0, 1.0) for i in range(25)]
    tight[-1] = tight[-1]._replace(close=100.6)
    assert _signal(tight, 0.0, {**p, "min_step": 0.0})[0] == pytest.approx(0.2)  # 10 格，每格 0.2%
    assert _signal(tight, 0.0, {**p, "min_step": 0.01})[0] == pytest.approx(0.0)  # 2 格，每格 1%：还在上面那格


# ---------------- 止损冷静期 ----------------

def test_stop_waits_lookback_bars_without_new_lows():
    L = 20
    p = {"lookback": L, "levels": 10, "breakout": "stop", "min_step": 0.0}
    base = _box([100.0] * (2 * L + 1))
    crash = Candle(T0 + len(base) * STEP, 92.0, 92.0, 84.0, 85.0, 1.0)  # 收盘 85 跌破下沿 90
    tail = [Candle(T0 + (len(base) + 1 + k) * STEP, 88.0, 89.0, 87.0, 88.0, 1.0) for k in range(L)]
    c = base + [crash] + tail
    look = _look(p)
    k_crash = len(base)
    assert _call(c[k_crash - look + 1 : k_crash + 1], 1.0, p)[0] == 0.0
    assert breakdown_ago(c[: k_crash + 1], L) == 0
    for k in range(1, L):  # 冷静期：没再创新低也先空着
        t = k_crash + k
        target, note = _call(c[t - look + 1 : t + 1], 0.0, p)
        assert target == 0.0 and "冷静期" in note
    t = k_crash + L  # 跌破那根已经滚出最近 lookback 根：按新区间 [84, 92] 重开网格
    target, note = _call(c[t - look + 1 : t + 1], 0.0, p)
    assert target > 0 and "网格" in note
    assert grid_range(c[t - look + 1 : t + 1], L) == (84.0, 92.0)
    # hold 模式在跌破那一刻是满仓扛着
    assert _call(c[k_crash - _look(HOLD) + 1 : k_crash + 1], 0.5, HOLD)[0] == 1.0


# ---------------- 预热、幂等、参数检查 ----------------

@pytest.mark.parametrize("params", [HOLD, STOP, {"lookback": 7, "levels": 3, "breakout": "stop", "min_step": 0.02}])
def test_warmup_is_exact(params):
    c = make_candles(_walk(400, seed=5), TF)
    look = _look(params)
    for t in range(2 * look, len(c) + 1, 3):
        for pos in (0.0, 0.33, 1.0):
            assert _call(c[t - look : t], pos, params) == _call(c[:t], pos, params)


def test_same_inputs_same_output():
    c = make_candles(_walk(300, seed=8), TF)
    for params in (HOLD, STOP):
        look = _look(params)
        for t in range(look, len(c) + 1, 5):
            w = c[t - look : t]
            for pos in (0.0, 0.5, 1.0):
                assert _call(w, pos, params) == _call(list(w), pos, params)


def test_bad_params_raise():
    s = get("grid")
    with pytest.raises(ValueError):
        s.lookback(s.resolve({"breakout": "yolo"}))
    with pytest.raises(ValueError):
        s.lookback(s.resolve({"levels": 0}))
    with pytest.raises(ValueError):
        s.lookback(s.resolve({"min_step": 1.5}))
    with pytest.raises(ValueError):
        s.lookback(s.resolve({"lookback": 500, "breakout": "stop"}))  # 2×500+1 > 990
    assert s.lookback(s.resolve({"lookback": 494, "breakout": "stop"})) <= MAX_WARMUP


# ---------------- 回测层面的常识 ----------------

def test_grid_earns_in_a_range_and_stop_limits_a_crash():
    p = {"lookback": 50, "levels": 10, "min_step": 0.01}
    rng = make_candles(wave(1200, base=100, amp=20, period=60), TF)
    g = run_backtest("grid", rng, timeframe=TF, params={**p, "breakout": "hold"}, fee=0.002, slippage=0.0005, min_cost=1)
    h = run_backtest("hodl", rng, timeframe=TF, size=0.5, fee=0.002, slippage=0.0005, min_cost=1)
    assert g.total_return > 0.5 and g.total_return > h.total_return  # 纯震荡：网格的主场

    crash = make_candles(_walk(600, seed=11, drift=-0.004), TF)
    hold = run_backtest("grid", crash, timeframe=TF, params={**p, "breakout": "hold"}, fee=0.002, slippage=0.0005, min_cost=1)
    stop = run_backtest("grid", crash, timeframe=TF, params={**p, "breakout": "stop"}, fee=0.002, slippage=0.0005, min_cost=1)
    assert hold.hodl_return < -0.6
    assert stop.total_return > hold.total_return + 0.2  # 单边跌：止损比一路接到底少亏一大截
