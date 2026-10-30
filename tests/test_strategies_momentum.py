"""动量派（tsmom / macd / momentum_vol）的合成行情测试：涨势上车、跌势空仓、迟滞、预热够长、幂等。"""
import math
import random

import pytest

from bot.backtest import run_backtest
from bot.candles import timeframe_ms
from bot.strategies import Context, get, normalize
from bot.strategies.momentum import MAX_WARMUP, macd_lines, realized_vol

from .helpers import make_candles, wave

NAMES = ("tsmom", "macd", "momentum_vol")


def _ctx(window, tf="1d"):
    return Context(timeframe=tf, asof_ms=window[-1].ts + timeframe_ms(tf), symbol="TEST/USDT")


def _window(name, closes, tf="1d", **params):
    s = get(name)
    p = s.resolve(params)
    c = make_candles(closes, tf)
    n = s.lookback(p)
    assert len(c) >= n, "测试数据比 warmup 短"
    return s, p, c[-n:]


def _decide(name, closes, position=0.0, tf="1d", **params):
    """恰好喂 warmup 根 K 线（取末尾），和实盘、回测的喂法一样。"""
    s, p, w = _window(name, closes, tf, **params)
    return normalize(s.fn(w, position, p, _ctx(w, tf)))


def _walk(n, seed=7, drift=0.0003, vol=0.03):
    rng = random.Random(seed)
    px = [100.0]
    for _ in range(n - 1):
        px.append(px[-1] * math.exp(rng.gauss(drift, vol)))
    return px


UP = [100 * 1.01 ** i + 2 * math.sin(i / 2) for i in range(400)]  # 稳步上涨带点抖动
CRASH = [100 + 0.5 * math.sin(i / 3) for i in range(300)] + [100 * 0.98 ** k for k in range(1, 11)]  # 横盘后急跌
DOWN = [100 * 0.99 ** i for i in range(400)]  # 阴跌


# ---------------- 涨势上车、跌势空仓 ----------------

@pytest.mark.parametrize("name", NAMES)
def test_uptrend_goes_long(name):
    for pos in (0.0, 1.0):
        x, note = _decide(name, UP, pos)
        assert x > 0.3 and note, (name, pos, x, note)
    assert _decide("tsmom", UP)[0] == 1.0 and _decide("macd", UP)[0] == 1.0


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("series", [CRASH, DOWN], ids=["crash", "slow_bleed"])
def test_downtrend_goes_flat(name, series):
    for pos in (0.0, 1.0):
        assert _decide(name, series, pos)[0] == 0.0, (name, pos)


def test_macd_zero_filter_blocks_golden_cross_below_zero():
    # 阴跌里 DIF 会慢慢收敛到零轴，从下方“金叉”DEA：经典 MACD 会上车，零轴过滤会挡住
    assert _decide("macd", DOWN, zero_filter=False)[0] == 1.0
    assert _decide("macd", DOWN, zero_filter=True)[0] == 0.0
    # 跌久了反弹几根，零轴下方金叉
    bounce = [200 * 0.995 ** i for i in range(300)] + [200 * 0.995 ** 299 * 1.01 ** k for k in range(1, 9)]
    assert _decide("macd", bounce, zero_filter=False)[0] == 1.0
    x, note = _decide("macd", bounce, zero_filter=True)
    assert x == 0.0 and "零轴" in note
    # 急跌时不管开不开过滤都是死叉
    for zf in (False, True):
        assert _decide("macd", CRASH, 1.0, zero_filter=zf)[0] == 0.0
    # 反弹继续，DIF 站上零轴时早就在 DEA 上方了（不是“刚金叉”）：空仓的这时才上车，拿着的继续拿
    s, p, _ = _window("macd", bounce)
    long_bounce = bounce + [bounce[-1] * 1.01 ** k for k in range(1, 80)]
    for end in range(len(bounce), len(long_bounce)):
        w = make_candles(long_bounce[:end], "1d")[-s.lookback(p) :]
        dif, dea = macd_lines([c.close for c in w], p["fast"], p["slow"], p["signal"])
        if dif[-1] > 0 and dif[-1] > dea[-1] and dif[-2] > dea[-2]:
            break
    else:
        pytest.fail("构造的反弹没让 DIF 站上零轴")
    x, note = normalize(s.fn(w, 0.0, p, _ctx(w)))
    assert x == 1.0 and "梭" in note
    assert normalize(s.fn(w, 1.0, p, _ctx(w)))[0] == 1.0


# ---------------- 迟滞：进场线和离场线之间维持现状 ----------------

def _with_return(r, n=14, bars=60):
    """最后一根相对 n 根之前涨 r，其余横盘。"""
    px = [100.0] * bars
    px[-1] = 100.0 * (1 + r)
    return px


def test_tsmom_hysteresis_band():
    band = dict(lookback=14, enter=0.05, exit=0.0)
    assert _decide("tsmom", _with_return(0.02), 0.0, **band)[0] == 0.0  # 涨 2%：没过进场线，空仓的继续空仓
    assert _decide("tsmom", _with_return(0.02), 1.0, **band)[0] == 1.0  # 涨 2%：没破离场线，拿着的继续拿
    assert _decide("tsmom", _with_return(0.06), 0.0, **band)[0] == 1.0  # 过进场线，梭
    assert _decide("tsmom", _with_return(-0.01), 1.0, **band)[0] == 0.0  # 破离场线，跑
    assert _decide("tsmom", _with_return(0.0), 1.0, **band)[0] == 1.0  # 正好压线不算破
    # 半仓以下算空仓（卖剩的零头不影响判断）
    assert _decide("tsmom", _with_return(0.02), 0.3, **band)[0] == 0.0


def test_tsmom_band_cuts_churn():
    c = make_candles(wave(1500, period=40, amp=8, drift=0.0003), "4h")
    plain = run_backtest("tsmom", c, timeframe="4h", params={"lookback": 20, "enter": 0.0, "exit": 0.0}, min_cost=1)
    banded = run_backtest("tsmom", c, timeframe="4h", params={"lookback": 20, "enter": 0.05, "exit": -0.05}, min_cost=1)
    assert 0 < banded.fills < plain.fills


def _vol_series():
    # 每根交替 +4.6% / -3.6%：动量为正，年化波动约 80%，目标 40% -> 半仓左右
    px = [100.0]
    for i in range(80):
        px.append(px[-1] * (1.046 if i % 2 == 0 else 0.964))
    return px


def test_momentum_vol_sizes_by_volatility_and_band():
    px = _vol_series()
    s, p, w = _window("momentum_vol", px)
    want = min(1.0, p["target_vol"] / realized_vol(w, p["vol_window"], "1d"))
    assert 0.2 < want < 0.8
    run = lambda pos: normalize(s.fn(w, pos, p, _ctx(w)))[0]
    assert run(0.0) == pytest.approx(want)  # 空仓上车直接到目标仓位
    near = want + p["band"] / 2
    assert run(near) == pytest.approx(near)  # 差不到 band：不调，省手续费
    assert run(want + p["band"] + 0.05) == pytest.approx(want)  # 差太多：调回去
    # 低波动的币仓位封顶 1
    calm = [100 * 1.004 ** i * (1 + 0.001 * (-1) ** i) for i in range(80)]
    assert _decide("momentum_vol", calm)[0] == 1.0


def test_momentum_vol_enters_and_exits_like_tsmom():
    # 拿着 0.4 仓也算在车上（不能用 HELD=0.5 判断），动量破离场线就清仓
    assert _decide("momentum_vol", CRASH, 0.4)[0] == 0.0
    assert _decide("momentum_vol", _with_return(0.02), 0.0)[0] == 0.0  # 没过进场线，不上车
    assert _decide("momentum_vol", _with_return(0.02), 0.4)[0] > 0.0  # 没破离场线，继续拿


# ---------------- 预热 ----------------

def test_warmup_formulas_and_limits():
    assert get("tsmom").lookback(get("tsmom").resolve({"lookback": 84})) == 85
    m = get("macd")
    assert m.lookback(m.resolve({"fast": 12, "slow": 26, "signal": 9})) == 3 * (26 + 9) + 10
    v = get("momentum_vol")
    assert v.lookback(v.resolve({"lookback": 14, "vol_window": 30})) == 31
    for name in NAMES:
        s = get(name)
        assert s.lookback(s.resolve({})) <= MAX_WARMUP
    with pytest.raises(ValueError, match="上限"):
        get("tsmom").lookback(get("tsmom").resolve({"lookback": 2000}))
    with pytest.raises(ValueError, match="上限"):
        m.lookback(m.resolve({"fast": 100, "slow": 300, "signal": 50}))


def test_tsmom_only_needs_lookback_plus_one():
    px = _walk(500)
    s, p, w = _window("tsmom", px)
    long_c = make_candles(px, "1d")
    for pos in (0.0, 1.0):
        assert normalize(s.fn(w, pos, p, _ctx(w))) == normalize(s.fn(long_c, pos, p, _ctx(long_c)))


@pytest.mark.parametrize("periods", [(12, 26, 9), (18, 39, 14)])
def test_macd_warmup_converges(periods):
    """warmup 根算出来的 DIF/DEA 和用长得多的历史算的几乎一样，金叉死叉判断基本不变。"""
    f, s, g = periods
    n = get("macd").lookback(get("macd").resolve({"fast": f, "slow": s, "signal": g}))
    px = _walk(1300)
    dl, el = macd_lines(px, f, s, g)  # EMA 只看过去，长历史算一遍就行
    off = s + g - 2  # macd_lines 输出相对输入裁掉的开头
    flips = 0
    for end in range(1000, 1300):
        d_long, e_long = dl[end - 1 - off], el[end - 1 - off]
        ds, es = macd_lines(px[end - n : end], f, s, g)
        assert abs(d_long - ds[-1]) < 1e-3 * px[end - 1] and abs(e_long - es[-1]) < 1e-3 * px[end - 1]
        flips += (d_long > e_long) != (ds[-1] > es[-1])
    assert flips <= 3  # 只可能在 DIF≈DEA 的平手处不一致


def test_realized_vol_annualizes_by_timeframe():
    px = _vol_series()
    c = make_candles(px, "1d")
    v1d, v4h = realized_vol(c, 30, "1d"), realized_vol(c, 30, "4h")
    assert v4h == pytest.approx(v1d * math.sqrt(6))
    assert realized_vol(make_candles([100.0] * 40), 30, "1d") == 0.0


# ---------------- 参数校验 ----------------

def test_bad_params_fail_loudly():
    with pytest.raises(ValueError, match="exit"):
        get("tsmom").lookback({"lookback": 14, "enter": 0.0, "exit": 0.05})
    with pytest.raises(ValueError, match="lookback"):
        get("tsmom").lookback({"lookback": 0, "enter": 0.0, "exit": 0.0})
    with pytest.raises(ValueError, match="fast"):
        get("macd").lookback({"fast": 26, "slow": 12, "signal": 9, "zero_filter": False})
    with pytest.raises(ValueError, match="zero_filter"):  # 带引号的 "false" 会被当成真，直接拒绝
        get("macd").lookback(get("macd").resolve({"zero_filter": "false"}))
    v = get("momentum_vol")
    with pytest.raises(ValueError, match="band"):
        v.lookback(v.resolve({"band": 1.5}))
    with pytest.raises(ValueError, match="target_vol"):
        v.lookback(v.resolve({"target_vol": 0}))


# ---------------- 幂等 ----------------

@pytest.mark.parametrize("name", NAMES)
def test_idempotent_and_does_not_mutate_input(name):
    px = _walk(600, seed=3, drift=0.001)
    s, p, w = _window(name, px)
    snapshot = list(w)
    for pos in (0.0, 0.37, 1.0):
        a = normalize(s.fn(w, pos, p, _ctx(w)))
        b = normalize(s.fn(w, pos, p, _ctx(w)))
        assert a == b and 0.0 <= a[0] <= 1.0
    assert w == snapshot


@pytest.mark.parametrize("name", NAMES)
def test_backtest_smoke(name):
    c = make_candles(wave(1200, period=90, amp=15, drift=0.0008), "1d")
    r = run_backtest(name, c, timeframe="1d", min_cost=1)
    assert math.isfinite(r.total_return) and r.fills > 0
    assert 0.0 < r.exposure < 1.0
