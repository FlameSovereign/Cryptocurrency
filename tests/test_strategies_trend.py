"""趋势派（bot/strategies/trend.py）的合成行情测试。

涨势要上车、跌势要空仓、海龟在通道里靠 position 维持现状、预热长度够用、同样输入同样输出。
"""
from __future__ import annotations

import math
import random

import pytest

from bot import indicators as ind
from bot.backtest import run_backtest
from bot.candles import Candle, timeframe_ms
from bot.strategies import Context, get, normalize
from bot.strategies.trend import MAX_WARMUP, supertrend_state

from .helpers import T0, make_candles, wave

NAMES = ("sma_cross", "donchian", "supertrend")
TF = "4h"
STEP = timeframe_ms(TF)


def _ctx(window, tf=TF):
    return Context(timeframe=tf, asof_ms=window[-1].ts + timeframe_ms(tf), symbol="TEST/USDT")


def _call(name, window, position=0.0, params=None):
    s = get(name)
    p = s.resolve(params)
    return normalize(s.fn(window, position, p, _ctx(window)))


def _signal(name, candles, position=0.0, params=None):
    """用最近恰好 warmup 根 K 线问策略要目标仓位（和实盘、回测喂数据的方式一样）。"""
    s = get(name)
    look = s.lookback(s.resolve(params))
    assert len(candles) >= look
    return _call(name, candles[-look:], position, params)


def _walk(n, seed, vol=0.02, regime=120):
    """带牛熊切换的随机游走：每 regime 根随机换一次漂移方向（涨 / 跌 / 横）。"""
    rng = random.Random(seed)
    px, out, drift = 100.0, [], 0.0
    for i in range(n):
        if i % regime == 0:
            drift = rng.choice([-0.004, 0.0, 0.004])
        px *= math.exp(drift + rng.gauss(0, vol))
        out.append(px)
    return out


def _look(name, params=None):
    s = get(name)
    return s.lookback(s.resolve(params))


# ---------------- 涨势上车、跌势空仓 ----------------

@pytest.mark.parametrize("name", NAMES)
def test_uptrend_goes_all_in(name):
    c = make_candles([100 * 1.01 ** i for i in range(_look(name) + 30)], TF)
    for pos in (0.0, 1.0):
        target, note = _signal(name, c, pos)
        assert target == 1.0 and note


@pytest.mark.parametrize("name", NAMES)
def test_downtrend_goes_flat(name):
    c = make_candles([100 * 0.99 ** i for i in range(_look(name) + 30)], TF)
    for pos in (0.0, 1.0):
        target, note = _signal(name, c, pos)
        assert target == 0.0 and note


def test_sma_cross_flips_on_the_cross():
    p = {"fast": 5, "slow": 20, "ma": "sma"}
    down_then_up = [100 - i for i in range(40)] + [61 + 3 * i for i in range(30)]
    targets = [_signal("sma_cross", make_candles(down_then_up[: k + 1], TF), 0.0, p)[0]
               for k in range(_look("sma_cross", p) - 1, len(down_then_up))]
    # 先空仓，金叉之后一直满仓，只翻一次
    assert targets[0] == 0.0 and targets[-1] == 1.0
    assert sum(a != b for a, b in zip(targets, targets[1:])) == 1
    gold = make_candles(down_then_up, TF)
    first_long = next(k for k in range(len(targets)) if targets[k] == 1.0)
    window = gold[: _look("sma_cross", p) + first_long][-_look("sma_cross", p):]
    assert "金叉" in _call("sma_cross", window, 0.0, p)[1]


# ---------------- 迟滞：只有海龟靠 position 维持现状 ----------------

def test_donchian_holds_inside_channel_only_if_already_in():
    p = {"entry": 20, "exit": 10}
    base = [100.0] * 40 + [100.0 + 2 * i for i in range(1, 11)]  # 一路突破到 120
    inside = base + [116.0, 118.0, 115.0, 117.0]  # 回落，但没破前 10 根最低
    c = make_candles(inside, TF)
    assert _signal("donchian", c, 1.0, p)[0] == 1.0  # 车上的人：拿住
    assert _signal("donchian", c, 0.0, p)[0] == 0.0  # 车下的人：等下一次突破
    for pos in (0.0, 1.0):
        assert _signal("donchian", make_candles(base + [125.0], TF), pos, p)[0] == 1.0  # 真突破
        assert _signal("donchian", make_candles(inside + [90.0], TF), pos, p)[0] == 0.0  # 破位


def test_donchian_breakout_uses_previous_bars_only():
    p = {"entry": 20, "exit": 10}
    flat = [Candle(T0 + i * STEP, 100.0, 110.0, 90.0, 100.0, 1.0) for i in range(20)]

    def with_last(close, high):
        return flat + [Candle(T0 + 20 * STEP, 100.0, high, 95.0, close, 1.0)]

    # 收盘只是追平前高，不算突破；最新这根自己的最高价（200）不进通道，否则永远突破不了
    assert _signal("donchian", with_last(110.0, 200.0), 0.0, p)[0] == 0.0
    assert _signal("donchian", with_last(110.5, 200.0), 0.0, p)[0] == 1.0


@pytest.mark.parametrize("name", ["sma_cross", "supertrend"])
def test_state_strategies_ignore_position(name):
    c = make_candles(_walk(700, seed=3), TF)
    look = _look(name)
    for t in range(look, len(c) + 1, 7):
        w = c[t - look : t]
        assert _call(name, w, 0.0)[0] == _call(name, w, 1.0)[0]


# ---------------- 预热够长：和用更长历史算出来的结论一致 ----------------

@pytest.mark.parametrize("name,params", [("sma_cross", {"fast": 10, "slow": 40, "ma": "sma"}),
                                         ("donchian", {"entry": 30, "exit": 12})])
def test_warmup_is_exact_for_finite_memory_rules(name, params):
    c = make_candles(_walk(500, seed=5), TF)
    look = _look(name, params)
    for t in range(2 * look, len(c) + 1, 3):
        for pos in (0.0, 1.0):
            assert _call(name, c[t - look : t], pos, params)[0] == _call(name, c[:t], pos, params)[0]


def _full_history_long(name, params, c):
    """用整段历史一口气算出每根 K 线的多空（参照系）。"""
    if name == "sma_cross":
        px = ind.closes(c)
        f, s = ind.ema(px, params["fast"]), ind.ema(px, params["slow"])
        return [a is not None and b is not None and a > b for a, b in zip(f, s)]
    return _tv_supertrend(c, params["period"], params["mult"])[0]


@pytest.mark.parametrize("name,params", [("sma_cross", {"fast": 20, "slow": 60, "ma": "ema"}),
                                         ("supertrend", {"period": 10, "mult": 3.0}),
                                         ("supertrend", {"period": 14, "mult": 5.0})])
def test_warmup_is_long_enough_for_path_dependent_rules(name, params):
    """EMA / Supertrend 有记忆：只看 warmup 根，和用整段历史算出来的多空，绝大多数时候一样。"""
    look = _look(name, params)
    for seed in (11, 12):
        c = make_candles(_walk(3 * look + 400, seed=seed), TF)
        ref = _full_history_long(name, params, c)
        ends = range(3 * look, len(c) + 1, 2)
        same = sum(_call(name, c[t - look : t], 0.0, params)[0] == float(ref[t - 1]) for t in ends)
        assert same / len(ends) >= 0.99, (name, params, seed, same, len(ends))


def test_warmup_limits_and_bad_params():
    for name in NAMES:
        assert _look(name) <= MAX_WARMUP
    bad = [("sma_cross", {"fast": 60, "slow": 20}), ("sma_cross", {"ma": "wma"}),
           ("sma_cross", {"fast": 50, "slow": 400, "ma": "ema"}),  # 3*400+10 > 990
           ("donchian", {"entry": 0}), ("donchian", {"entry": 1200}),
           ("supertrend", {"mult": 0}), ("supertrend", {"period": 0}), ("supertrend", {"period": 200}),
           ("supertrend", {"period": 30, "mult": 8.0})]  # 轨道太宽要预热 1050 根
    for name, params in bad:
        with pytest.raises(ValueError):
            _look(name, params)


# ---------------- 幂等、不改输入 ----------------

@pytest.mark.parametrize("name", NAMES)
def test_idempotent_and_does_not_touch_input(name):
    c = make_candles(_walk(_look(name) + 50, seed=8), TF)
    w = c[-_look(name):]
    before = list(w)
    for pos in (0.0, 0.3, 1.0):
        assert _call(name, w, pos) == _call(name, w, pos)
    assert w == before


# ---------------- Supertrend 和教科书写法逐根对账 ----------------

def _tv_supertrend(candles, n, mult):
    """照抄 TradingView ta.supertrend（direction 1 = 空头，-1 = 多头）。返回每根的 (是否多头列表, 那条线列表)。"""
    atr = ind.atr(candles, n)
    up_prev = dn_prev = st_prev = None
    direction = None
    bulls, lines = [], []
    for i, c in enumerate(candles):
        if atr[i] is None:
            bulls.append(False)
            lines.append(None)
            continue
        src = (c.high + c.low) / 2
        up, dn = src + mult * atr[i], src - mult * atr[i]
        if up_prev is not None:
            pc = candles[i - 1].close
            dn = dn if dn > dn_prev or pc < dn_prev else dn_prev
            up = up if up < up_prev or pc > up_prev else up_prev
        if st_prev is None:
            direction = 1
        elif st_prev == up_prev:
            direction = -1 if c.close > up else 1
        else:
            direction = 1 if c.close < dn else -1
        st_prev = dn if direction == -1 else up
        up_prev, dn_prev = up, dn
        bulls.append(direction == -1)
        lines.append(st_prev)
    return bulls, lines


def test_supertrend_matches_textbook_formulation():
    c = make_candles(_walk(900, seed=21, vol=0.03), TF)
    rng = random.Random(4)
    for _ in range(60):
        n, mult = rng.choice([5, 10, 14]), rng.choice([1.5, 3.0, 4.5])
        end = rng.randrange(60, len(c) + 1)
        w = c[max(0, end - rng.randrange(40, 300)) : end]
        bull, line, _ = supertrend_state(w, n, mult)
        ref_bull, ref_line = _tv_supertrend(w, n, mult)
        assert bull == ref_bull[-1] and line == pytest.approx(ref_line[-1], rel=1e-12)


# ---------------- 回测：吃到牛市、躲过崩盘 ----------------

@pytest.mark.parametrize("name", NAMES)
def test_backtest_rides_bull_and_dodges_crash(name):
    """横盘 → 涨 3 倍 → 跌 70%：囤币白忙一场，趋势派应该带着利润下车。"""
    closes = wave(300, amp=3, period=40) + [100 * 1.004 ** i for i in range(1, 301)]
    top = closes[-1]
    closes += [top * 0.996 ** i for i in range(1, 301)]
    c = make_candles(closes, "1d")
    r = run_backtest(name, c, timeframe="1d", fee=0.002, slippage=0.0005, min_cost=1)
    assert r.total_return > r.hodl_return + 0.5, (r.total_return, r.hodl_return)
    assert r.trades[-1]["side"] == "sell"  # 崩盘时已经空仓
