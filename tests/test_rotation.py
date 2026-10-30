"""轮动梭哈：决策函数、调仓（先卖后买）、多币回测，全部用合成 K 线。"""
import math

import pytest

from bot import rotation as R
from bot.candles import timeframe_ms
from bot.engine import OrderPlan, Risk, simulate_fill
from bot.strategies import Context, load_all
from bot.wallet import Fill, Wallet

from .helpers import T0, make_candles, wave

D = 86_400_000
P = R.resolve("rotation", {})
LOOK = R.get("rotation").lookback(P)
N = int(P["lookback"])


def ramp(total: float, n: int = LOOK, start: float = 100.0) -> list[float]:
    """n 个收盘价，几何均匀地从 start 走到 start * (1 + total)：近 n-1 根涨幅恰好是 total。"""
    g = (1 + total) ** (1 / (n - 1))
    return [start * g**i for i in range(n)]


def win(**rets) -> dict:
    """win(BTC=0.1, ETH=0.3) -> {"BTC/USDT": 31 根 1d K 线, ...}，近 lookback 根涨幅分别是 10%、30%。"""
    return {f"{k}/USDT": make_candles(ramp(r), "1d") for k, r in rets.items()}


def ctx_for(windows) -> Context:
    last = max(w[-1].ts for w in windows.values())
    return Context(timeframe="1d", asof_ms=last + D)


def pick(windows, holding=None, **params):
    p = R.resolve("rotation", params)
    return R.get("rotation").fn(windows, holding, p, ctx_for(windows))


# ---------------- 决策函数 ----------------

def test_uptrend_goes_all_in_on_the_strongest():
    target, note = pick(win(BTC=0.05, ETH=0.30, SOL=-0.10))
    assert target == "ETH/USDT" and "梭" in note


def test_downtrend_goes_to_cash():
    assert pick(win(BTC=-0.05, ETH=-0.20, SOL=-0.01))[0] is None
    # 拿着币的时候全篮子转跌：跑
    target, note = pick(win(BTC=-0.05, ETH=-0.20, SOL=-0.01), holding="SOL/USDT")
    assert target is None and "跑" in note


def test_hysteresis_only_switches_when_leader_is_clearly_stronger():
    margin = P["margin"]
    assert margin > 0
    # 新第一名只强一点点：拿住手里的
    assert pick(win(BTC=0.20, ETH=0.20 + margin / 2), holding="BTC/USDT")[0] == "BTC/USDT"
    # 强出门槛：换车
    assert pick(win(BTC=0.20, ETH=0.20 + margin * 1.5), holding="BTC/USDT")[0] == "ETH/USDT"
    # 同样的行情，空仓时直接买第一名（滞后只影响“换车”）
    assert pick(win(BTC=0.20, ETH=0.20 + margin / 2))[0] == "ETH/USDT"
    # 手里的动量没了（<= 0），就算新第一名没强出门槛也要换
    assert pick(win(BTC=-0.01, ETH=0.02), holding="BTC/USDT")[0] == "ETH/USDT"
    # margin = 0：谁第一就拿谁
    assert pick(win(BTC=0.20, ETH=0.21), holding="BTC/USDT", margin=0.0)[0] == "ETH/USDT"


def test_holding_outside_the_basket_is_not_kept():
    # 配置里把手里的币从篮子里删了：按空仓处理，直接换成第一名
    assert pick(win(BTC=0.10, ETH=0.05), holding="DOGE/USDT")[0] == "BTC/USDT"


def test_min_ret_raises_the_bar():
    assert pick(win(BTC=0.03, ETH=0.01), min_ret=0.05)[0] is None
    assert pick(win(BTC=0.08, ETH=0.01), min_ret=0.05)[0] == "BTC/USDT"


def test_exit_ret_adds_hysteresis_at_the_cash_boundary():
    w = win(BTC=-0.03, ETH=-0.04)
    # 默认下车线 = 上车线 = 0：跌了就跑
    assert pick(w, holding="BTC/USDT")[0] is None
    # 下车线 -5%：小跌拿住，空仓时同样的行情不上车
    assert pick(w, holding="BTC/USDT", exit_ret=-0.05)[0] == "BTC/USDT"
    assert pick(w, exit_ret=-0.05)[0] is None
    assert pick(win(BTC=-0.06, ETH=-0.04), holding="BTC/USDT", exit_ret=-0.05)[0] is None
    # 手里的跌破下车线、别的币还在涨：直接换过去
    assert pick(win(BTC=-0.06, ETH=0.02), holding="BTC/USDT", exit_ret=-0.05)[0] == "ETH/USDT"
    # 下车线写得比上车线还高：压回上车线
    assert pick(win(BTC=0.03, ETH=0.0), holding="BTC/USDT", exit_ret=0.05)[0] == "BTC/USDT"


def test_ties_break_by_basket_order():
    w = win(ETH=0.10, BTC=0.10)
    assert pick(w)[0] == "ETH/USDT"
    assert pick(dict(reversed(list(w.items()))))[0] == "BTC/USDT"


def test_sharpe_score_prefers_the_steady_climber():
    steady = ramp(0.30)
    wild = [x * (1.25 if i % 2 else 0.8) for i, x in enumerate(ramp(0.35))]
    wild[-1] = 135.0  # 涨幅更大但一路暴拉暴砸
    w = {"BTC/USDT": make_candles(steady, "1d"), "DOGE/USDT": make_candles(wild, "1d")}
    assert pick(w, margin=0.0)[0] == "DOGE/USDT"
    assert pick(w, margin=0.0, score="sharpe")[0] == "BTC/USDT"


def test_warmup_is_exactly_enough():
    for params in ({}, {"lookback": 7}, {"lookback": 60, "score": "sharpe"}):
        p = R.resolve("rotation", params)
        look = R.get("rotation").lookback(p)
        n = int(p["lookback"])
        assert look == n + 1 and look <= 990
        c = make_candles(wave(look + 50, period=17, drift=0.002), "1d")
        full = R.momentum(c, n, p["score"])
        assert R.momentum(c[-look:], n, p["score"]) == full  # 只看最后 warmup 根
        assert R.momentum(c[-look + 1 :], n, p["score"]) is None  # 少一根就算不出来
        # 决策只依赖最后 warmup 根：喂更长的窗口结果一样
        longer = {"A/USDT": c, "B/USDT": make_candles(wave(look + 50, period=23), "1d")}
        exact = {s: w[-look:] for s, w in longer.items()}
        fn = R.get("rotation").fn
        assert fn(exact, None, p, ctx_for(exact)) == fn(longer, None, p, ctx_for(longer))


def test_decisions_are_idempotent_and_stateless():
    a, b = win(BTC=0.1, ETH=0.3, SOL=0.2), win(BTC=-0.3, ETH=-0.1)
    for name, rot in R.ROTATIONS.items():
        p = R.resolve(name, {})
        first = rot.fn(a, "BTC/USDT", p, ctx_for(a))
        rot.fn(b, None, p, ctx_for(b))  # 中间插一次别的输入，不许留下状态
        assert rot.fn(a, "BTC/USDT", p, ctx_for(a)) == first, name


def test_resolve_rejects_typos():
    with pytest.raises(ValueError, match="不认识参数"):
        R.resolve("rotation", {"look_back": 30})
    with pytest.raises(ValueError, match="score"):
        R.resolve("rotation", {"score": "sortino"})
    with pytest.raises(KeyError):
        R.get("rotation_nope")


def test_rotation_is_not_in_the_single_symbol_registry():
    assert not set(R.ROTATIONS) & set(load_all())


def test_all_rotations_are_sane():
    """每个轮动策略：默认参数能跑、只选候选里的币或现金、同样输入同样输出、warmup 不超过一页 K 线。"""
    data = {f"C{i}/USDT": make_candles(wave(400, period=40 + 13 * i, drift=0.001 * (i - 1)), "1d") for i in range(4)}
    for name, rot in R.ROTATIONS.items():
        p = R.resolve(name, {})
        look = rot.lookback(p)
        assert look <= 990, name
        for end in range(look, 400, 37):
            w = {s: c[end - look : end] for s, c in data.items()}
            for holding in (None, "C0/USDT"):
                x, y = rot.fn(w, holding, p, ctx_for(w)), rot.fn(w, holding, p, ctx_for(w))
                assert x == y and (x[0] is None or x[0] in w), name
        r = R.run_backtest(rot, data, timeframe="1d", min_cost=1)
        assert math.isfinite(r.total_return), name


def test_align_drops_stale_symbols():
    w = win(BTC=0.1, ETH=0.2)
    w["SOL/USDT"] = w["ETH/USDT"][:-1]  # 少了最新一根
    keep, stale = R.align(w)
    assert set(keep) == {"BTC/USDT", "ETH/USDT"} and stale == ["SOL/USDT"]


# ---------------- 风控 ----------------

def test_decide_kill_switch_and_halt():
    rot = R.get("rotation")
    w = win(BTC=0.3, ETH=0.1)
    wallet = Wallet.new(100)
    wallet.holding = "ETH/USDT"
    wallet.apply(Fill("buy", 10.0, 10.0, 100.0))
    d = R.decide(rot, P, w, wallet, 4.0, risk=Risk(kill_switch=0.5), ctx=ctx_for(w))
    assert d.target is None and d.event == "kill_switch"
    wallet.halted = True
    assert R.decide(rot, P, w, wallet, 40.0, ctx=ctx_for(w)).event == "halted"
    with pytest.raises(ValueError):
        R.decide(rot, P, w, Wallet(budget=100, cash=0, qty=1, holding="ETH/USDT"), None, ctx=ctx_for(w))


# ---------------- 调仓：先卖后买 ----------------

def _paper(fee=0.002, slippage=0.0):
    log = []

    def fill(sym, plan, px):
        log.append((sym, plan.side))
        return simulate_fill(plan, px, fee=fee, slippage=slippage)

    return fill, log


def _holding(symbol="BTC/USDT", qty=1.0, price=100.0):
    w = Wallet.new(100)
    w.holding = symbol
    w.apply(Fill("buy", price, qty, qty * price))
    return w


def test_rebalance_switch_sells_everything_then_buys_with_all_cash():
    w = _holding()
    fill, log = _paper()
    seen = []
    R.rebalance(w, "ETH/USDT", {"BTC/USDT": 110.0, "ETH/USDT": 10.0}, min_cost=lambda s, p: 3.0, fill=fill,
                on_fill=lambda s, f, pnl: seen.append((s, f.side, pnl, w.holding)))
    assert log == [("BTC/USDT", "sell"), ("ETH/USDT", "buy")]
    assert w.holding == "ETH/USDT" and w.cash == pytest.approx(0)
    assert w.qty == pytest.approx(110 * 0.998 / 10 * 0.998)
    # 卖出那一笔记账时已经是空仓（零头已清），实盘就在这时存盘
    assert seen[0][0] == "BTC/USDT" and seen[0][2] == pytest.approx(110 * 0.998 - 100) and seen[0][3] is None
    # 再跑一次同样的目标：不动（幂等）
    log.clear()
    R.rebalance(w, "ETH/USDT", {"ETH/USDT": 10.0}, min_cost=lambda s, p: 3.0, fill=fill)
    assert log == []


def test_rebalance_failed_sell_blocks_the_buy():
    w = _holding()
    R.rebalance(w, "ETH/USDT", {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, min_cost=lambda s, p: 3.0, fill=lambda *a: None)
    assert w.holding == "BTC/USDT" and w.qty == 1.0 and w.cash == 0


def test_rebalance_failed_buy_leaves_cash():
    w = _holding()
    fill, _ = _paper()
    R.rebalance(w, "ETH/USDT", {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, min_cost=lambda s, p: 3.0,
                fill=lambda s, plan, px: fill(s, plan, px) if plan.side == "sell" else None)
    assert w.holding is None and w.qty == 0 and w.cash == pytest.approx(99.8)


def test_rebalance_writes_off_unsellable_leftovers():
    w = _holding(qty=1.0, price=100.0)
    fill, log = _paper()
    # 币跌到只值 2，低于最小下单额 3，卖不掉：记作损耗，现金照样梭新币（这里没现金就不买）
    notes = R.rebalance(w, "ETH/USDT", {"BTC/USDT": 2.0, "ETH/USDT": 10.0}, min_cost=lambda s, p: 3.0, fill=fill)
    assert log == [] and w.holding is None and w.qty == 0 and "损耗" in notes[0]


def test_rebalance_to_cash_and_top_up():
    w = _holding()
    fill, log = _paper()
    R.rebalance(w, None, {"BTC/USDT": 100.0}, min_cost=lambda s, p: 3.0, fill=fill)
    assert log == [("BTC/USDT", "sell")] and w.holding is None and w.qty == 0
    w = _holding()
    w.rebudget(150)  # 追加预算：同一个币补仓
    log.clear()
    R.rebalance(w, "BTC/USDT", {"BTC/USDT": 100.0}, min_cost=lambda s, p: 3.0, fill=fill)
    assert log == [("BTC/USDT", "buy")] and w.cash == pytest.approx(0)


def test_rebalance_refuses_wallet_without_holding():
    with pytest.raises(ValueError):
        R.rebalance(Wallet(budget=100, cash=0, qty=1.0), "BTC/USDT", {"BTC/USDT": 1.0}, min_cost=lambda s, p: 1.0,
                    fill=lambda *a: None)


# ---------------- 回测 ----------------

def _market(n=300):
    """A 横盘震荡；B 前 120 根横着、之后一路涨；C 一路阴跌。"""
    a = wave(n, base=100, amp=5, period=37)
    b = [100.0] * 120 + [100.0 * 1.01**i for i in range(1, n - 119)]
    c = [100.0 * 0.997**i for i in range(n)]
    return {s: make_candles(x, "1d") for s, x in (("A/USDT", a), ("B/USDT", b), ("C/USDT", c))}


def test_backtest_rides_the_trend_and_trades_at_next_open():
    data = _market()
    r = R.run_backtest("rotation", data, timeframe="1d", fee=0.002, slippage=0.0, min_cost=1)
    assert r.symbol == "A+B+C" and r.total_return > 1.0 and r.total_return > r.hodl_return
    assert r.trades[-1]["symbol"] == "B/USDT" and r.trades[-1]["side"] == "buy"
    by_ts = {s: {c.ts: c for c in cs} for s, cs in data.items()}
    for t in r.trades:  # 第 i 根收盘决策、第 i+1 根开盘成交
        assert t["price"] == pytest.approx(by_ts[t["symbol"]][t["ts"]].open)
    assert not any(t["symbol"] == "C/USDT" for t in r.trades)
    again = R.run_backtest("rotation", data, timeframe="1d", fee=0.002, slippage=0.0, min_cost=1)
    assert again.summary() == r.summary()


def test_backtest_new_listing_waits_for_enough_history():
    data = _market()
    late = data["B/USDT"][150:]  # B 第 150 根才“上市”，而且一上市就在涨
    data["B/USDT"] = late
    r = R.run_backtest("rotation", data, timeframe="1d", min_cost=1)
    first_b = min(t["ts"] for t in r.trades if t["symbol"] == "B/USDT")
    assert first_b >= late[LOOK - 1].ts + D  # 攒够 warmup 根，收盘决策，下一根才买
    assert r.hodl_return == pytest.approx(R.run_backtest(
        "rotation", {s: data[s] for s in ("A/USDT", "C/USDT")}, timeframe="1d", min_cost=1).hodl_return)


def test_backtest_hodl_is_equal_weight_buy_and_hold():
    up = [100 * 1.01**i for i in range(200)]
    flat = [50.0] * 200
    data = {"U/USDT": make_candles(up, "1d"), "F/USDT": make_candles(flat, "1d")}
    r = R.run_backtest("rotation", data, timeframe="1d", fee=0.002, slippage=0.001, min_cost=1)
    first = LOOK - 1
    u = up[-1] / (data["U/USDT"][first + 1].open * 1.001)
    f = flat[-1] / (data["F/USDT"][first + 1].open * 1.001)
    assert r.hodl_return == pytest.approx((u + f) / 2 * 0.998 - 1)


def test_backtest_split_segments_and_kill_switch():
    data = _market()
    cut = data["A/USDT"][200].ts
    ins = R.run_backtest("rotation", data, timeframe="1d", end_ms=cut, min_cost=1)
    out = R.run_backtest("rotation", data, timeframe="1d", start_ms=cut, min_cost=1)
    assert ins.end_ms <= cut + D and out.start_ms == cut + D
    # 一路涨，某天一根大阴线砸到成本的六成多（净值跌破 7 成），之后又涨回来
    path = [100.0 * 1.01**i for i in range(60)] + [90.0] + [90.0 * 1.02**i for i in range(1, 40)]
    crash = {s: make_candles([x * k for x in path], "1d") for s, k in (("X/USDT", 1.0), ("Y/USDT", 0.5))}
    r = R.run_backtest("rotation", crash, timeframe="1d", risk=Risk(kill_switch=0.3), min_cost=1)
    assert r.trades[-1]["event"] == "kill_switch" and r.trades[-1]["side"] == "sell"
    assert sum(t["side"] == "buy" for t in r.trades) == 1  # 熔断后就算涨回来也不再上车
    assert 60 < r.final_equity < 70
    with pytest.raises(ValueError):
        R.run_backtest("rotation", crash, timeframe="1d", risk=Risk(stop_loss=0.1))


def test_monkey_is_deterministic_and_trades():
    data = _market()
    a = R.run_backtest("rotation_monkey", data, timeframe="1d", min_cost=1)
    b = R.run_backtest("rotation_monkey", data, timeframe="1d", min_cost=1)
    c = R.run_backtest("rotation_monkey", data, timeframe="1d", params={"seed": 7}, min_cost=1)
    assert a.summary() == b.summary() and a.fills > 10 and a.trades != c.trades
    assert timeframe_ms("1d") == D
