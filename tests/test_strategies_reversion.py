"""抄底派：RSI 超卖抄底、布林带抄底。全用合成行情，一秒内跑完。"""
import math
import random

import pytest

from bot import indicators as ind
from bot.backtest import run_backtest
from bot.candles import timeframe_ms
from bot.strategies import Context, get, normalize

from .helpers import make_candles

RSI = get("rsi_revert")
BB = get("bollinger_revert")
TF = "4h"

RSI_TEXTBOOK = {"period": 14, "low": 30, "high": 70, "trend": 0}
RSI_CONNORS = {"period": 2, "low": 10, "high": 70, "trend": 200}
BB_TEXTBOOK = {"period": 20, "k": 2.0, "exit": "mid", "trend": 0, "min_edge": 0.0}


def _ctx(window):
    return Context(timeframe=TF, asof_ms=window[-1].ts + timeframe_ms(TF), symbol="TEST/USDT")


def _signal(strat, closes, position, **params):
    """用恰好 warmup 根 K 线（和实盘、回测一样）算一次信号。"""
    p = strat.resolve(params)
    look = strat.lookback(p)
    c = make_candles(closes, TF)
    assert len(c) >= look, "测试数据比 warmup 还短"
    w = c[-look:]
    return normalize(strat.fn(w, position, p, _ctx(w)))


def _wave(n, drift, amp=0.12, period=60):
    """乘法波浪：每根涨 drift，叠加 ±amp 的正弦起伏，回调要好几根 K 线才走完。"""
    return [100 * (1 + drift) ** i * (1 + amp * math.sin(2 * math.pi * i / period)) for i in range(n)]


def _noisy_trend(n, drift, sigma=0.03, seed=1):
    """趋势线 × 独立噪声：价格围着一条线乱跳，跌下去的下一根就弹回来（抄底派的理想世界）。"""
    rng = random.Random(seed)
    return [100 * (1 + drift) ** i * math.exp(sigma * rng.gauss(0, 1)) for i in range(n)]


def _walk(n, seed=7, vol=0.02):
    """带种子的随机游走，可复现。"""
    rng = random.Random(seed)
    px, out = 100.0, []
    for _ in range(n):
        px *= math.exp(rng.gauss(0, vol))
        out.append(px)
    return out


def _dip(n_up=250, up=0.003, n_down=4, down=0.04):
    """稳步上涨之后连跌几根：牛市里的一次急跌。"""
    px = [100 * (1 + up) ** i for i in range(n_up)]
    for _ in range(n_down):
        px.append(px[-1] * (1 - down))
    return px


# ---------------- 上涨趋势里的回调：抄底上车 ----------------

@pytest.mark.parametrize("params", [{}, RSI_TEXTBOOK, RSI_CONNORS])
def test_rsi_buys_the_dip_in_uptrend(params):
    px = _dip()
    s, note = _signal(RSI, px, 0.0, **params)
    assert s == 1.0 and "梭" in note
    # 急跌之前一路上涨、RSI 超买：空仓时不追高
    assert _signal(RSI, px[:-4], 0.0, **params)[0] == 0.0


@pytest.mark.parametrize("params", [{}, {"trend": 200}, BB_TEXTBOOK])
def test_bollinger_buys_the_dip_in_uptrend(params):
    px = _dip()
    s, note = _signal(BB, px, 0.0, **params)
    assert s == 1.0 and "梭" in note
    assert _signal(BB, px[:-4], 0.0, **params)[0] == 0.0


@pytest.mark.parametrize("strat,params,world", [
    # RSI(14) 吃的是好几根 K 线的回调（波浪）；RSI(2) 和教科书布林带吃的是一两根 K 线的噪声
    (RSI, {}, "wave"), (RSI, {"trend": 200}, "wave"), (RSI, RSI_TEXTBOOK, "wave"), (RSI, RSI_CONNORS, "noise"),
    (BB, {}, "wave"), (BB, {"trend": 200}, "wave"), (BB, BB_TEXTBOOK, "noise"),
])
def test_uptrend_with_pullbacks_makes_money(strat, params, world):
    px = _wave(1200, drift=0.001) if world == "wave" else _noisy_trend(1200, drift=0.001)
    r = run_backtest(strat, make_candles(px, TF), timeframe=TF, params=params, fee=0.002, slippage=0.0005, min_cost=1)
    assert r.round_trips >= 5 and r.win_rate > 0.6 and r.total_return > 0.1


# ---------------- 下跌趋势：开了趋势过滤就空仓 ----------------

@pytest.mark.parametrize("strat,params", [
    (RSI, {"trend": 200}), (RSI, {**RSI_TEXTBOOK, "trend": 200}), (RSI, RSI_CONNORS),
    (BB, {"trend": 200}), (BB, {**BB_TEXTBOOK, "trend": 200}),
])
def test_trend_filter_stays_flat_in_downtrend(strat, params):
    for px in (_noisy_trend(900, drift=-0.002), _wave(900, drift=-0.002)):
        r = run_backtest(strat, make_candles(px, TF), timeframe=TF, params=params, fee=0.002, slippage=0.0005, min_cost=1)
        assert r.fills == 0 and r.total_return == 0.0


def test_trend_filter_note_says_why():
    px = _wave(500, drift=-0.002)
    p = RSI.resolve({"trend": 200})
    rsi = ind.rsi(px, p["period"])
    # 找一根超卖的 K 线：不开过滤会抄底，开了过滤不抄并说明原因
    i = next(i for i in range(RSI.lookback(p), len(px)) if rsi[i] < p["low"])
    assert _signal(RSI, px[: i + 1], 0.0)[0] == 1.0
    s, note = _signal(RSI, px[: i + 1], 0.0, trend=200)
    assert s == 0.0 and "飞刀" in note


def test_without_trend_filter_it_catches_falling_knives():
    """不开趋势过滤的抄底在单边下跌里会一路接刀：这是抄底派的本性，测试把它写明白。"""
    px = [100 * 0.99 ** i for i in range(200)]
    assert _signal(RSI, px, 0.0)[0] == 1.0
    assert _signal(BB, px, 0.0, k=1.0)[0] == 1.0
    r = run_backtest(RSI, make_candles(px, TF), timeframe=TF, fee=0.002, slippage=0.0005, min_cost=1)
    assert r.exposure > 0.9 and r.total_return < -0.5  # 满仓扛着跌


@pytest.mark.parametrize("strat,params", [(RSI, {}), (RSI, RSI_TEXTBOOK), (BB, {}), (BB, BB_TEXTBOOK)])
def test_sells_into_the_rally(strat, params):
    """抄底之后反弹到超买 / 回到中轨（或上轨）就卖：满仓的人拿到的是 0。"""
    px = _dip()
    px = px + [px[-1] * 1.04 ** i for i in range(1, 7)]
    s, note = _signal(strat, px, 1.0, **params)
    assert s == 0.0 and "跑" in note


# ---------------- 迟滞：中间地带维持现状 ----------------

@pytest.mark.parametrize("params", [{}, RSI_TEXTBOOK])
def test_rsi_hysteresis_in_neutral_zone(params):
    px = _walk(400)
    p = RSI.resolve(params)
    rsi = ind.rsi(px, p["period"])
    mids = [i for i in range(RSI.lookback(p), len(px)) if p["low"] + 3 < rsi[i] < p["high"] - 3]
    assert len(mids) > 20
    for i in mids[:20]:
        assert _signal(RSI, px[: i + 1], 1.0, **params)[0] == 1.0
        assert _signal(RSI, px[: i + 1], 0.0, **params)[0] == 0.0
        assert _signal(RSI, px[: i + 1], 0.6, **params)[0] == 1.0  # 过半算在车上
        assert _signal(RSI, px[: i + 1], 0.4, **params)[0] == 0.0  # 卖剩的零头不算


def test_rsi_oversold_holds_and_overbought_flat_regardless_of_position():
    down = [100 * 0.98 ** i for i in range(120)]
    up = [100 * 1.02 ** i for i in range(120)]
    assert _signal(RSI, down, 1.0)[0] == 1.0  # 拿着的继续拿
    assert _signal(RSI, up, 0.0)[0] == 0.0 and _signal(RSI, up, 1.0)[0] == 0.0


@pytest.mark.parametrize("period", [20, 40])
def test_bollinger_hysteresis_between_lower_and_exit_line(period):
    px = _walk(800, seed=3)
    base = {"period": period, "k": 2.0, "min_edge": 0.0}
    mid, up, lo = ind.bollinger(px, period, 2.0)
    start = BB.lookback(BB.resolve(base))
    between = [i for i in range(start, len(px)) if lo[i] < px[i] < mid[i]]
    above_mid = [i for i in range(start, len(px)) if mid[i] < px[i] < up[i]]
    assert len(between) > 10 and len(above_mid) > 10
    for i in between[:10]:
        for exit_ in ("mid", "upper"):
            assert _signal(BB, px[: i + 1], 1.0, exit=exit_, **base)[0] == 1.0
            assert _signal(BB, px[: i + 1], 0.0, exit=exit_, **base)[0] == 0.0
    for i in above_mid[:10]:
        assert _signal(BB, px[: i + 1], 1.0, exit="mid", **base)[0] == 0.0  # 回到中轨上方：卖
        assert _signal(BB, px[: i + 1], 1.0, exit="upper", **base)[0] == 1.0  # 等上轨：继续拿
        assert _signal(BB, px[: i + 1], 0.0, exit="upper", **base)[0] == 0.0


def test_bollinger_min_edge_skips_tiny_bounces():
    """下轨离卖出线太近（比如股票代币休市时段几乎不动），弹回去也赚不回手续费，不抄。"""
    px = [100 + 0.01 * math.sin(i) for i in range(60)] + [99.9]
    for exit_ in ("mid", "upper"):
        assert _signal(BB, px, 0.0, exit=exit_, min_edge=0.0)[0] == 1.0
        s, note = _signal(BB, px, 0.0, exit=exit_, min_edge=0.01)
        assert s == 0.0 and "不抄" in note
    assert _signal(BB, px, 0.0)[0] == 0.0  # 默认就开着 1% 的门槛


# ---------------- 预热够不够 ----------------

def test_rsi_warmup_matches_long_history():
    """用 warmup 根 K 线算的 RSI 和用长得多的历史算的几乎一样（实盘只拉 warmup 根）。"""
    px = _walk(1200, seed=11, vol=0.03)
    for params in ({}, RSI_CONNORS, {"period": 4, "low": 20}, {"period": 21}):
        p = RSI.resolve(params)
        n, look = p["period"], RSI.lookback(p)
        assert look >= 6 * n
        full = ind.rsi(px, n)
        worst = max(abs(ind.rsi(px[i - look + 1 : i + 1], n)[-1] - full[i]) for i in range(600, len(px), 3))
        assert worst < 0.5, (params, worst)


@pytest.mark.parametrize("strat,params", [
    (RSI, {}), (RSI, RSI_TEXTBOOK), (RSI, RSI_CONNORS), (BB, {}), (BB, BB_TEXTBOOK), (BB, {"trend": 200}),
])
def test_signal_with_warmup_equals_signal_with_more_history(strat, params):
    px = _walk(900, seed=5, vol=0.025)
    c = make_candles(px, TF)
    p = strat.resolve(params)
    look = strat.lookback(p)
    flips = 0
    for i in range(3 * look, len(c), 4):
        for pos in (0.0, 1.0):
            a = normalize(strat.fn(c[i - look + 1 : i + 1], pos, p, _ctx(c[: i + 1])))[0]
            b = normalize(strat.fn(c[i - 3 * look + 1 : i + 1], pos, p, _ctx(c[: i + 1])))[0]
            flips += a != b
    assert flips == 0


def test_warmup_covers_trend_and_limits():
    assert RSI.lookback(RSI.resolve({"period": 14})) == 6 * 14 + 5
    assert RSI.lookback(RSI.resolve({"period": 2})) == 17
    assert RSI.lookback(RSI.resolve({"trend": 200})) == 201
    assert BB.lookback(BB.resolve({"period": 20})) == 21
    assert BB.lookback(BB.resolve({"period": 20, "trend": 200})) == 201
    for s in (RSI, BB):
        assert s.lookback(s.resolve({})) <= 990
    with pytest.raises(ValueError):
        RSI.lookback(RSI.resolve({"period": 200}))
    with pytest.raises(ValueError):
        BB.lookback(BB.resolve({"trend": 2000}))


@pytest.mark.parametrize("strat,params", [
    (RSI, {"low": 70, "high": 30}), (RSI, {"low": 0}), (RSI, {"high": 100}), (RSI, {"trend": -1}), (RSI, {"period": 0}),
    (BB, {"exit": "top"}), (BB, {"k": 0}), (BB, {"period": 1}), (BB, {"min_edge": 1.5}), (BB, {"trend": -5}),
])
def test_bad_params_fail_loudly(strat, params):
    with pytest.raises(ValueError):
        strat.lookback(strat.resolve(params))


# ---------------- 幂等 ----------------

@pytest.mark.parametrize("strat", [RSI, BB])
def test_idempotent_and_pure(strat):
    c = make_candles(_walk(500, seed=9), TF)
    p = strat.resolve({})
    look = strat.lookback(p)
    w = c[-look:]
    frozen, pcopy = list(w), dict(p)
    for pos in (0.0, 0.3, 1.0):
        a = strat.fn(w, pos, p, _ctx(w))
        b = strat.fn(w, pos, p, _ctx(w))
        assert a == b and isinstance(a, tuple) and a[1]
    assert w == frozen and p == pcopy  # 不改输入
    r1 = run_backtest(strat, c, timeframe=TF, min_cost=1)
    r2 = run_backtest(strat, c, timeframe=TF, min_cost=1)
    assert r1.summary() == r2.summary() and r1.trades == r2.trades
