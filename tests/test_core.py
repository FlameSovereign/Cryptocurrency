import math

import pytest

from bot import indicators as ind
from bot.backtest import max_drawdown, run_backtest
from bot.candles import drop_unclosed, from_ccxt, merge, timeframe_ms
from bot.engine import OrderPlan, Risk, apply_events, decide, plan_order, simulate_fill
from bot.strategies import Context, FearGreed, get, load_all, normalize, register
from bot.wallet import Fill, Wallet

from .helpers import T0, make_candles, wave


# ---------------- 指标 ----------------

def test_sma_ema_basic():
    v = [1, 2, 3, 4, 5]
    assert ind.sma(v, 3) == [None, None, 2.0, 3.0, 4.0]
    e = ind.ema(v, 3)
    assert e[:2] == [None, None] and e[2] == 2.0 and e[3] == pytest.approx(3.0) and e[4] == pytest.approx(4.0)


def test_rsi_extremes():
    up = list(range(1, 40))
    assert ind.rsi(up, 14)[-1] == 100.0
    down = list(range(40, 1, -1))
    assert ind.rsi(down, 14)[-1] == pytest.approx(0.0)
    flat = [5.0] * 30
    assert ind.rsi(flat, 14)[-1] == 50.0


def test_highest_lowest_match_naive():
    v = wave(200, period=17)
    for n in (1, 5, 20):
        hi, lo = ind.highest(v, n), ind.lowest(v, n)
        for i in range(n - 1, len(v)):
            assert hi[i] == max(v[i - n + 1 : i + 1])
            assert lo[i] == min(v[i - n + 1 : i + 1])
        assert hi[: n - 1] == [None] * (n - 1)


def test_atr_and_bollinger_shapes():
    c = make_candles(wave(60))
    a = ind.atr(c, 14)
    assert a[12] is None and a[13] > 0
    mid, up, lo = ind.bollinger([x.close for x in c], 20, 2)
    assert up[19] > mid[19] > lo[19]


# ---------------- K 线 ----------------

def test_timeframe_and_closed_filter():
    assert timeframe_ms("4h") == 4 * 3_600_000
    with pytest.raises(ValueError):
        timeframe_ms("4hours")
    c = make_candles([1, 2, 3], "1h")
    # 第三根 K 线收盘时间 = T0 + 3h；在 T0 + 3h - 1ms 时它还没收盘
    assert len(drop_unclosed(c, "1h", T0 + 3 * 3_600_000 - 1)) == 2
    assert len(drop_unclosed(c, "1h", T0 + 3 * 3_600_000)) == 3


def test_from_ccxt_sorts_and_dedupes():
    rows = [[2, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, None], [2, 9, 9, 9, 9, 9]]
    c = from_ccxt(rows)
    assert [x.ts for x in c] == [1, 2] and c[1].close == 9 and c[0].volume == 0
    assert [x.ts for x in merge(c[:1], c)] == [1, 2]


# ---------------- 钱包 ----------------

def test_wallet_buy_sell_roundtrip_accounting():
    w = Wallet.new(100)
    f = simulate_fill(OrderPlan("buy", cost=100), 10.0, fee=0.002, slippage=0.0)
    w.apply(f)
    assert w.cash == pytest.approx(0) and w.qty == pytest.approx(10 * 0.998)
    assert w.entry_price == pytest.approx(100 / (10 * 0.998))
    s = simulate_fill(OrderPlan("sell", amount=w.qty), 12.0, fee=0.002, slippage=0.0)
    pnl = w.apply(s)
    assert w.qty == pytest.approx(0) and not w.in_position
    expect = 10 * 0.998 * 12 * 0.998
    assert w.cash == pytest.approx(expect)
    assert pnl == pytest.approx(expect - 100)
    assert w.realized_pnl == pytest.approx(expect - 100)
    assert w.fees_paid == pytest.approx(10 * 0.002 * 10 + 10 * 0.998 * 12 * 0.002)


def test_wallet_dust_counts_as_flat():
    w = Wallet.new(100)
    w.apply(Fill("buy", 10.0, 10.0, 100.0))
    w.apply(Fill("sell", 10.0, 9.9999, 99.999))
    assert not w.in_position and w.qty == pytest.approx(0.0001)


def test_rebudget():
    w = Wallet.new(100)
    assert "追加" in w.rebudget(150) and w.cash == 150 and w.budget == 150
    w.apply(Fill("buy", 10.0, 10.0, 120.0))
    msg = w.rebudget(50)
    assert w.cash == 0 and w.budget == pytest.approx(120) and "只够" in msg


def test_wallet_roundtrip_dict():
    w = Wallet.new(100)
    w.apply(Fill("buy", 10.0, 5.0, 50.0))
    w2 = Wallet.from_dict({**w.to_dict(), "unknown_field": 1})
    assert w2 == w


# ---------------- 决策 & 下单 ----------------

STRAT_CACHE = {}


def _const_strategy(value):
    name = f"_const_{value}"
    if name not in STRAT_CACHE:
        register(name, title=name, school="test", summary="", defaults={}, warmup=lambda p: 1)(lambda *a: value)
        STRAT_CACHE[name] = get(name)
    return STRAT_CACHE[name]


CTX = Context(timeframe="1h", asof_ms=T0)


def test_plan_order_all_in_and_out():
    w = Wallet.new(100)
    p = plan_order(1.0, w, 10.0, min_cost=3)
    assert p.side == "buy" and p.cost == 100
    w.apply(simulate_fill(p, 10.0, fee=0.002, slippage=0))
    assert plan_order(1.0, w, 10.0, min_cost=3) is None  # 已经满仓，幂等
    p = plan_order(0.0, w, 10.0, min_cost=3)
    assert p.side == "sell" and p.amount == w.qty


def test_plan_order_threshold_and_min_cost():
    w = Wallet.new(100)
    assert plan_order(0.01, w, 10.0, threshold=0.02, min_cost=1) is None
    assert plan_order(0.5, w, 10.0, min_cost=60) is None
    p = plan_order(0.5, w, 10.0, min_cost=3)
    assert p.side == "buy" and p.cost == pytest.approx(50)
    w = Wallet.new(100)
    w.apply(Fill("buy", 10.0, 1.0, 10.0))  # 价值 10 的仓位
    assert plan_order(0.0, w, 2.0, min_cost=3) is None  # 跌到只值 2，低于最小下单，卖不掉


def test_stop_loss_then_lock_until_signal_resets():
    s1, s0 = _const_strategy(1.0), _const_strategy(0.0)
    risk = Risk(stop_loss=0.1)
    w = Wallet.new(100)
    w.apply(simulate_fill(OrderPlan("buy", cost=100), 10.0, fee=0, slippage=0))
    d = decide(s1, {}, [], w, 8.9, risk=risk, ctx=CTX)
    assert d.event == "stop_loss" and d.target == 0
    apply_events(w, d)
    w.apply(simulate_fill(plan_order(d.target, w, 8.9), 8.9, fee=0, slippage=0))
    d = decide(s1, {}, [], w, 9.5, risk=risk, ctx=CTX)
    assert d.event == "locked" and d.target == 0
    d = decide(s0, {}, [], w, 9.5, risk=risk, ctx=CTX)
    assert d.event == "unlocked"
    apply_events(w, d)
    d = decide(s1, {}, [], w, 9.5, risk=risk, ctx=CTX)
    assert d.event is None and d.target == 1


def test_take_profit_and_trailing():
    s1 = _const_strategy(1.0)
    w = Wallet.new(100)
    w.apply(Fill("buy", 10.0, 10.0, 100.0))
    assert decide(s1, {}, [], w, 15.1, risk=Risk(take_profit=0.5), ctx=CTX).event == "take_profit"
    w.mark(20.0)
    assert decide(s1, {}, [], w, 15.9, risk=Risk(trailing_stop=0.2), ctx=CTX).event == "trailing_stop"
    assert decide(s1, {}, [], w, 16.1, risk=Risk(trailing_stop=0.2), ctx=CTX).event is None


def test_kill_switch_halts_forever():
    s1 = _const_strategy(1.0)
    w = Wallet.new(100)
    w.apply(Fill("buy", 10.0, 10.0, 100.0))
    d = decide(s1, {}, [], w, 4.9, risk=Risk(kill_switch=0.5), ctx=CTX)
    assert d.event == "kill_switch"
    apply_events(w, d)
    assert w.halted
    assert decide(s1, {}, [], w, 50, risk=Risk(), ctx=CTX).event == "halted"


def test_size_scales_target():
    s1 = _const_strategy(1.0)
    d = decide(s1, {}, [], Wallet.new(100), 10, size=0.3, ctx=CTX)
    assert d.target == pytest.approx(0.3)


def test_normalize():
    assert normalize(2) == (1.0, "")
    assert normalize((-1, "x")) == (0.0, "x")
    assert normalize(float("nan"))[0] == 0.0
    assert normalize(None)[0] == 0.0


def test_risk_validation():
    with pytest.raises(ValueError):
        Risk(stop_loss=15)
    with pytest.raises(ValueError):
        Risk(kill_switch=1.0)


# ---------------- 恐惧贪婪 ----------------

def test_fear_greed_publication_lag():
    day = 86_400_000
    fg = FearGreed({T0: 10, T0 + day: 90})
    assert fg.at(T0 + day) == 10  # 刚到 0 点，新值还没“发布满 1 小时”
    assert fg.at(T0 + day + 3_600_000) == 90
    assert fg.at(T0 - 1) is None


# ---------------- 回测 ----------------

def test_backtest_hodl_matches_buy_and_hold():
    closes = [100 * 1.001 ** i for i in range(500)]
    c = make_candles(closes, "1h")
    r = run_backtest("hodl", c, timeframe="1h", fee=0.002, slippage=0.0005, min_cost=1)
    assert r.fills == 1
    assert r.total_return == pytest.approx(r.hodl_return, rel=1e-9, abs=1e-9)
    assert r.max_drawdown < 0.003  # 只有进场那一下的手续费 + 滑点


def test_backtest_no_lookahead_on_entry_bar():
    # 策略在第 i 根收盘决策，只能在第 i+1 根开盘成交
    c = make_candles([100.0] * 10 + [200.0] * 10, "1h")
    r = run_backtest("hodl", c, timeframe="1h", fee=0, slippage=0, min_cost=1)
    assert r.trades[0]["ts"] == c[1].ts and r.trades[0]["price"] == c[1].open


def test_backtest_monkey_is_deterministic():
    c = make_candles(wave(2000, period=97), "1h")
    a = run_backtest("monkey", c, timeframe="1h", min_cost=1)
    b = run_backtest("monkey", c, timeframe="1h", min_cost=1)
    assert a.summary() == b.summary() and a.fills > 10


def test_backtest_not_enough_candles():
    with pytest.raises(ValueError):
        run_backtest("hodl", make_candles([1.0]), timeframe="1h")


def test_max_drawdown():
    assert max_drawdown([1, 2, 1, 3, 1.5]) == pytest.approx(0.5)


def test_all_registered_strategies_are_sane():
    """每个注册的策略：默认参数能跑、输出在 0~1、同样输入同样输出。"""
    c = make_candles(wave(1500, period=120, drift=0.0003), "4h")
    fg = FearGreed({T0 + i * 86_400_000: (i * 37) % 100 for i in range(400)})
    for name, s in load_all().items():
        if name.startswith("_"):
            continue
        params = s.resolve({})
        look = s.lookback(params)
        assert look <= 990, f"{name} warmup 太大，Gate 一页只有 1000 根"
        window = c[len(c) - look :]
        ctx = Context(timeframe="4h", asof_ms=window[-1].ts + timeframe_ms("4h"), fear_greed=fg)
        for pos in (0.0, 1.0):
            a = normalize(s.fn(window, pos, params, ctx))
            b = normalize(s.fn(window, pos, params, ctx))
            assert a == b and 0.0 <= a[0] <= 1.0, name
        r = run_backtest(s, c, timeframe="4h", fear_greed=fg, min_cost=1)
        assert math.isfinite(r.total_return), name


def test_fear_greed_staleness_and_range():
    day = 86_400_000
    fg = FearGreed({T0: 10, T0 + day: 20})
    assert fg.first_ms == T0 and fg.last_ms == T0 + day
    assert fg.at(T0 + 5 * day) == 20  # 4 天前的值还能用
    assert fg.at(T0 + 5 * day + 1) is None  # 断更超过 4 天：当作没数据


def test_dust_below_min_cost_counts_as_flat():
    """卖完剩的零头低于交易所最小下单额：卖不掉，当空仓，不能再触发止损、卡冷静期。"""
    w = Wallet.new(100)
    w.apply(Fill("buy", 2000.0, 0.05, 100.0))
    w.apply(Fill("sell", 2000.0, 0.0499, 99.8), flat_below=3.0)  # 剩 0.0001 ≈ 0.2 USDT
    assert not w.in_position and w.qty == pytest.approx(0.0001)
    w2 = Wallet.new(100)
    w2.apply(Fill("buy", 2000.0, 0.05, 100.0))
    w2.qty = 0.0012  # 比如账户里被手动卖得只剩 2.4 USDT
    assert w2.settle_dust(2000.0, flat_below=3.0) and not w2.in_position
    assert decide(_const_strategy(1.0), {}, [], w2, 1000.0, risk=Risk(stop_loss=0.1), ctx=CTX).event is None


def test_no_strategy_module_failed_to_import():
    from bot.strategies import LOAD_ERRORS

    load_all()
    assert not LOAD_ERRORS
