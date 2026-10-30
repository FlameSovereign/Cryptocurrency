"""情绪派策略：用合成的 K 线 + 合成的恐惧贪婪指数测行为，不联网。"""
import pytest

from bot import indicators as ind
from bot.backtest import run_backtest
from bot.candles import timeframe_ms
from bot.strategies import Context, FearGreed, get, normalize
from bot.strategies.sentiment import last_extreme

from .helpers import T0, make_candles, wave

DAY = 86_400_000


def fg_series(values, t0=T0):
    """第 i 天（UTC 0 点）的指数 = values[i]。"""
    return FearGreed({t0 + i * DAY: int(v) for i, v in enumerate(values)})


def ramp(a, b, n):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def call(name, candles, position, fg, tf="1d", **params):
    """按引擎的方式调用：只给最近 warmup 根已收盘 K 线，决策时点 = 最后一根的收盘时间。"""
    s = get(name)
    p = s.resolve(params)
    window = candles[-s.lookback(p):]
    ctx = Context(timeframe=tf, asof_ms=window[-1].ts + timeframe_ms(tf), symbol="BTC/USDT", fear_greed=fg)
    return normalize(s.fn(window, position, p, ctx))


def flat_index(v, days=800):
    return fg_series([v] * days)


# ---------------- 别人恐惧我贪婪 ----------------

def test_fear_greed_buys_extreme_fear_and_holds():
    c = make_candles([100.0] * 30, "1d")
    assert call("fear_greed", c, 0.0, flat_index(10))[0] == 1.0  # 极度恐惧：梭
    assert call("fear_greed", c, 1.0, flat_index(10))[0] == 1.0
    # 中间地带：维持现状（迟滞只靠 position）
    assert call("fear_greed", c, 0.0, flat_index(50))[0] == 0.0
    assert call("fear_greed", c, 1.0, flat_index(50))[0] == 1.0
    # 默认 sell_above=101：再贪婪也不卖
    v, note = call("fear_greed", c, 1.0, flat_index(95))
    assert v == 1.0 and "拿住" in note


def test_fear_greed_sells_extreme_greed_when_enabled():
    c = make_candles([100.0] * 30, "1d")
    v, note = call("fear_greed", c, 1.0, flat_index(80), sell_above=75)
    assert v == 0.0 and "跑" in note
    assert call("fear_greed", c, 0.0, flat_index(80), sell_above=75)[0] == 0.0
    assert call("fear_greed", c, 1.0, flat_index(74), sell_above=75)[0] == 1.0


def test_fear_greed_rejects_crossed_thresholds():
    with pytest.raises(ValueError):
        get("fear_greed").fn(make_candles([1.0]), 0.0, {"buy_below": 60, "sell_above": 40},
                             Context(timeframe="1d", asof_ms=T0, fear_greed=flat_index(50)))


@pytest.mark.parametrize("name", ["fear_greed", "fear_greed_trend", "sentiment_momentum"])
def test_missing_index_keeps_position(name):
    c = make_candles(wave(400, period=60, drift=0.002), "1d")
    for pos in (0.0, 0.37, 1.0):
        late = fg_series([10] * 30, t0=c[-1].ts + 30 * DAY)  # 数据从未来才开始 = 这个时点还没数据
        for fg in (None, late):  # None = 没接数据源
            v, note = call(name, c, pos, fg)
            if v != pytest.approx(pos):
                # 只有一种例外：fear_greed_trend 持仓时跌破均线是价格信号，指数缺了也照样跑
                assert name == "fear_greed_trend" and pos == 1.0 and v == 0.0 and "跌破" in note
            else:
                assert "原样不动" in note


def test_fear_greed_trend_exits_on_trend_break_without_index():
    rows = [100.0] * 200 + [80.0] * 5  # 横盘后急跌：收盘在 EMA 下方
    c = make_candles(rows, "1d")
    v, note = call("fear_greed_trend", c[-get("fear_greed_trend").lookback(get("fear_greed_trend").resolve({})):], 1.0, None)
    assert v == 0.0 and "跌破" in note


# ---------------- 恐慌见底再上车 ----------------

def crash_then_rally():
    """200 天阴跌（指数从 50 跌到 10），再 200 天反弹（指数从 10 涨到 65）。"""
    px = ramp(100, 50, 200) + ramp(50, 150, 200)
    fg = fg_series(ramp(50, 10, 200) + ramp(10, 65, 200))
    return make_candles(px, "1d"), fg


def test_fear_greed_trend_no_falling_knife_then_rides_recovery():
    c, fg = crash_then_rally()
    # 阴跌中、指数极度恐惧，但价格在均线下方：不接飞刀
    v, note = call("fear_greed_trend", c[:200], 0.0, fg)
    assert v == 0.0 and "不接飞刀" in note
    # 反弹站上均线，而且一年内恐慌过：梭
    v, note = call("fear_greed_trend", c, 0.0, fg)
    assert v == 1.0 and "梭" in note
    # 跌破均线：不管情绪，跑
    broken = c + make_candles([150.0] + [110.0] * 3, "1d", t0=c[-1].ts + DAY)[1:]
    assert call("fear_greed_trend", broken, 1.0, fg)[0] == 0.0


def test_fear_greed_trend_needs_a_recent_panic():
    c = make_candles(ramp(50, 150, 400), "1d")  # 一路上涨
    calm = flat_index(60)
    v, note = call("fear_greed_trend", c, 0.0, calm)
    assert v == 0.0 and "没出现过极度恐惧" in note
    # 迟滞：同样的输入，已经在车上就继续拿着（只要没跌破均线）
    assert call("fear_greed_trend", c, 1.0, calm)[0] == 1.0
    # 恐慌在 fear_days 窗口外也不算
    old_panic = fg_series([10] * 20 + [60] * 380)
    assert call("fear_greed_trend", c, 0.0, old_panic, fear_days=100)[0] == 0.0
    assert call("fear_greed_trend", c, 0.0, old_panic, fear_days=365)[0] == 0.0  # 第 20 天的恐慌离现在 380 天
    recent_panic = fg_series([60] * 300 + [10] * 20 + [60] * 80)
    assert call("fear_greed_trend", c, 0.0, recent_panic, fear_days=100)[0] == 1.0


def test_fear_greed_trend_greed_regime_blocks_reentry():
    c = make_candles(ramp(50, 150, 400), "1d")
    fg = fg_series([60] * 300 + [10] * 10 + [60] * 40 + [85] * 5 + [60] * 45)  # 先恐慌后贪婪
    v, note = call("fear_greed_trend", c, 0.0, fg, sell_above=80)
    assert v == 0.0 and "极度贪婪" in note
    assert call("fear_greed_trend", c, 0.0, fg)[0] == 1.0  # 默认不看贪婪：最近的极端读数还是那次恐慌
    # 在车上遇到极度贪婪：开了 sell_above 就跑
    hot = fg_series([60] * 399 + [90])
    assert call("fear_greed_trend", c, 1.0, hot, sell_above=80)[0] == 0.0


def test_last_extreme_never_peeks_into_the_future():
    c = make_candles([100.0] * 200, "1d")
    asof = c[-1].ts + DAY
    ctx = Context(timeframe="1d", asof_ms=asof)
    days = {T0 + i * DAY: 60 for i in range(200)}
    base = last_extreme(Context(timeframe="1d", asof_ms=asof, fear_greed=FearGreed(days)), 25, 101, 365)
    future = dict(days)
    future[asof] = 5  # 决策时点当天 0 点才发布、还没满 1 小时
    future[asof + DAY] = 5
    got = last_extreme(Context(timeframe="1d", asof_ms=asof, fear_greed=FearGreed(future)), 25, 101, 365)
    assert base == got == (None, None, None)
    assert last_extreme(ctx, 25, 101, 365) == (None, None, None)  # 没数据源


def test_fear_greed_trend_warmup_is_enough():
    s = get("fear_greed_trend")
    p = s.resolve({})
    look = s.lookback(p)
    assert look <= 990
    c = make_candles(wave(1500, period=90, amp=15, drift=0.0005), "1d")
    fg = fg_series([(i * 37) % 100 for i in range(1600)])
    agree = checked = 0
    for end in range(5 * look, len(c), 7):
        short, long_ = c[end - look:end], c[end - 5 * look:end]
        e_short = ind.ema(ind.closes(short), p["ema"])[-1]
        e_long = ind.ema(ind.closes(long_), p["ema"])[-1]
        assert e_short == pytest.approx(e_long, rel=0.005)
        if abs(short[-1].close / e_long - 1) < 0.005:
            continue  # 贴着均线的那几根，种子误差可能让方向翻转，不算
        ctx = Context(timeframe="1d", asof_ms=short[-1].ts + DAY, fear_greed=fg)
        for pos in (0.0, 1.0):
            checked += 1
            agree += s.fn(short, pos, p, ctx)[0] == s.fn(long_, pos, p, ctx)[0]
    assert checked > 100 and agree == checked


def test_fear_greed_trend_param_validation():
    s = get("fear_greed_trend")
    with pytest.raises(ValueError):
        s.lookback(s.resolve({"ema": 330}))  # 3*330+10 > 990
    with pytest.raises(ValueError):
        s.lookback(s.resolve({"fear_days": 400}))  # 实盘只拉 400 天指数


# ---------------- 跟着情绪走 ----------------

def test_sentiment_momentum_follows_the_crowd_with_hysteresis():
    c = make_candles([100.0] * 10, "1d")
    assert call("sentiment_momentum", c, 0.0, flat_index(75))[0] == 1.0
    assert call("sentiment_momentum", c, 1.0, flat_index(35))[0] == 0.0
    for v in (41, 55, 69):  # 中间地带：维持现状
        assert call("sentiment_momentum", c, 0.0, flat_index(v))[0] == 0.0
        assert call("sentiment_momentum", c, 1.0, flat_index(v))[0] == 1.0
    with pytest.raises(ValueError):
        call("sentiment_momentum", c, 0.0, flat_index(50), enter=40, exit=40)


# ---------------- 回测：牛市在车上、熊市在车下 ----------------

def _bt(name, candles, fg, **params):
    return run_backtest(name, candles, timeframe="1d", fear_greed=fg, params=params or None, min_cost=1)


def test_backtests_uptrend_long_downtrend_flat():
    up = make_candles(ramp(100, 50, 200) + ramp(50, 150, 200), "1d")
    fg_up = fg_series(ramp(50, 10, 200) + ramp(10, 85, 200))
    down = make_candles(ramp(100, 160, 200) + ramp(160, 60, 200), "1d")
    fg_down = fg_series(ramp(50, 85, 200) + ramp(85, 20, 200))
    for name in ("fear_greed_trend", "sentiment_momentum"):
        assert _bt(name, up, fg_up).trades[-1]["side"] == "buy", name  # 牛市收尾时在车上
        r = _bt(name, down, fg_down)
        assert not r.trades or r.trades[-1]["side"] == "sell", name  # 熊市收尾时在车下
    # 抄底派是故意反着来的：熊市末端极度恐惧会上车，极度贪婪（开了 sell_above）会下车
    assert _bt("fear_greed", down, fg_series(ramp(50, 85, 200) + ramp(85, 10, 200))).trades[-1]["side"] == "buy"
    r = _bt("fear_greed", up, fg_up, sell_above=80)
    assert [t["side"] for t in r.trades] == ["buy", "sell"]


def test_sentiment_momentum_does_not_churn_in_the_middle_zone():
    c = make_candles(wave(300, period=40), "1d")
    zigzag = fg_series([75] + [41, 69] * 200)  # 进车后在 41~69 来回晃
    r = _bt("sentiment_momentum", c, zigzag)
    assert r.fills == 1


@pytest.mark.parametrize("name", ["fear_greed", "fear_greed_trend", "sentiment_momentum"])
def test_idempotent(name):
    c = make_candles(wave(600, period=70, drift=0.001), "1d")
    fg = fg_series([(i * 53) % 100 for i in range(700)])
    for pos in (0.0, 0.5, 1.0):
        a = call(name, c, pos, fg)
        b = call(name, c, pos, fg)
        assert a == b and 0.0 <= a[0] <= 1.0 and a[1]
    r1 = _bt(name, c, fg)
    r2 = _bt(name, c, fg)
    assert r1.summary() == r2.summary()
