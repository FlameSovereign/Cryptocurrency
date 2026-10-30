"""run.py 端到端：假交易所 + 模拟盘，验证下单、幂等、存盘、报告。"""
import csv
import json

import pytest

from bot import run as run_mod
from bot.brokers import PaperBroker
from bot.candles import now_ms
from bot.config import load_config
from bot.run import render, tick

from .helpers import FakeExchange, make_candles

H = 3_600_000

CONFIG = """
[exchange]
id = "gate"
mode = "paper"
fee = 0.002

[[bots]]
name = "t-hodl"
symbol = "BTC/USDT"
timeframe = "1h"
strategy = "hodl"
budget = 100
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("KURUMI_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    cfg = load_config(path, env={"KURUMI_STATE_DIR": str(tmp_path / "state")})
    t0 = (now_ms() // H - 60) * H
    ex = FakeExchange(make_candles([100.0 + i for i in range(60)], "1h", t0=t0))
    return cfg, ex, tmp_path


def test_paper_tick_buys_once_then_holds(setup):
    cfg, ex, tmp = setup
    bot = cfg.bots[0]
    broker = PaperBroker(ex, fee=cfg.fee)
    r1 = tick(bot, cfg, ex=ex, broker=broker, fear_greed=None)
    assert r1["status"] == "ok" and r1["fill"]["side"] == "buy"
    assert r1["fill"]["quote"] == pytest.approx(100)
    r2 = tick(bot, cfg, ex=ex, broker=broker, fear_greed=None)
    assert r2["fill"] is None and r2["exposure"] > 0.99

    sd = cfg.state_dir("paper")
    st = json.loads((sd / "bots" / "t-hodl.json").read_text())
    assert st["runs"] == 2 and st["symbol"] == "BTC/USDT" and len(st["orders"]) == 1
    assert len(list(csv.DictReader(open(sd / "trades.csv")))) == 1
    assert len(list(csv.DictReader(open(sd / "equity.csv")))) == 2


def test_dry_run_writes_nothing(setup):
    cfg, ex, tmp = setup
    rep = tick(cfg.bots[0], cfg, ex=ex, broker=PaperBroker(ex, fee=0.002), fear_greed=None, dry_run=True)
    assert rep["plan"]["side"] == "buy" and rep["fill"] is None
    assert not cfg.state_dir("paper").exists()


def test_symbol_change_with_open_position_is_refused(setup):
    cfg, ex, tmp = setup
    broker = PaperBroker(ex, fee=0.002)
    tick(cfg.bots[0], cfg, ex=ex, broker=broker, fear_greed=None)
    p = cfg.state_dir("paper") / "bots" / "t-hodl.json"
    st = json.loads(p.read_text())
    st["symbol"] = "ETH/USDT"
    p.write_text(json.dumps(st))
    with pytest.raises(RuntimeError, match="换了交易对"):
        tick(cfg.bots[0], cfg, ex=ex, broker=broker, fear_greed=None)


def test_not_enough_candles_is_a_skip(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.replace('strategy = "hodl"', 'strategy = "monkey"'))
    cfg = load_config(path, env={"KURUMI_STATE_DIR": str(tmp_path)})
    ex = FakeExchange(make_candles([1.0], "1h", t0=(now_ms() // H - 5000) * H))  # 只有一根很老的 K 线
    rep = tick(cfg.bots[0], cfg, ex=ex, broker=PaperBroker(ex, fee=0.002), fear_greed=None)
    assert rep["status"] == "skip"


def test_main_end_to_end_and_summary(setup, monkeypatch, capsys):
    cfg, ex, tmp = setup
    summary = tmp / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(run_mod, "make_exchange", lambda *a, **k: ex)
    assert run_mod.main(["--config", str(cfg.path)]) == 0
    text = summary.read_text()
    assert "t-hodl" in text and "买入" in text and "模拟盘" in text


def test_main_unknown_bot(setup, monkeypatch):
    cfg, ex, tmp = setup
    monkeypatch.setattr(run_mod, "make_exchange", lambda *a, **k: ex)
    with pytest.raises(SystemExit):
        run_mod.main(["--config", str(cfg.path), "--only", "nope"])


def test_render_handles_errors():
    text = render([{"bot": "x", "symbol": "BTC/USDT", "title": "t", "status": "error", "reason": "boom", "notes": []}], "paper")
    assert "boom" in text and "x" in text


# ---------------- 轮动机器人 ----------------

import copy
import math

from bot.brokers import LiveBroker, tag_prefix
from bot.config import load_config as _load
from bot.run import rotation_tick
from bot.wallet import Fill, Wallet

DAY = 86_400_000
ROT_CONFIG = """
[exchange]
id = "gate"
mode = "paper"
fee = 0.002

[[bots]]
name = "t-hodl"
symbol = "BTC/USDT"
timeframe = "1h"
strategy = "hodl"
budget = 100

[[bots]]
name = "t-rot"
strategy = "rotation"
symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
timeframe = "1d"
budget = 100
risk = { kill_switch = 0.5 }
"""


def _ramp(total, n=40, start=100.0):
    g = (1 + total) ** (1 / (n - 1))
    return [start * g**i for i in range(n)]


def _daily(start=None, **totals):
    """{"BTC/USDT": 最近 40 根已收盘日 K}，每个币 40 天涨幅给定；start = {币: 起点价}，默认 100。"""
    t0 = (now_ms() // DAY - 40) * DAY
    start = start or {}
    return {f"{k}/USDT": make_candles(_ramp(v, start=start.get(k, 100.0)), "1d", t0=t0) for k, v in totals.items()}


class MultiExchange:
    """多币版假交易所（公共行情）：每个交易对一套 K 线和最新价。"""

    id = "gate"

    def __init__(self, candles, *, min_cost=3.0, broken=()):
        self.markets = {}
        self.broken = set(broken)
        self.set(candles, min_cost=min_cost)

    def set(self, candles, *, min_cost=3.0):
        self.candles = dict(candles)
        self.last = {s: c[-1].close for s, c in self.candles.items()}
        for s in self.candles:
            b, q = s.split("/")
            self.markets[s] = {"symbol": s, "base": b, "quote": q, "spot": True,
                               "limits": {"cost": {"min": min_cost}, "amount": {"min": 1e-6}}, "precision": {"amount": 1e-6}}

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        if symbol in self.broken:
            raise ConnectionError("接口挂了")
        return [list(c) for c in self.candles[symbol] if since is None or c.ts >= since][:limit]

    def fetch_ticker(self, symbol):
        return {"last": self.last[symbol], "close": self.last[symbol]}


class FakeGate(MultiExchange):
    """够 LiveBroker 用的假 Gate：市价单按最新价立刻成交，买入手续费扣币、卖出扣钱。"""

    def __init__(self, candles, balances, fee=0.002):
        super().__init__(candles)
        self.balances, self.fee, self.orders = dict(balances), fee, []

    def fetch_balance(self, params=None):
        return {"free": dict(self.balances), "total": dict(self.balances)}

    def amount_to_precision(self, symbol, amount):
        return f"{math.floor(amount * 1e6) / 1e6:.6f}"

    def _fill(self, symbol, side, base_amt, quote_amt, text):
        m = self.markets[symbol]
        b, q = m["base"], m["quote"]
        if side == "buy":
            fee = {"currency": b, "cost": base_amt * self.fee}
            self.balances[q] -= quote_amt
            self.balances[b] = self.balances.get(b, 0.0) + base_amt - fee["cost"]
        else:
            fee = {"currency": q, "cost": quote_amt * self.fee}
            self.balances[b] -= base_amt
            self.balances[q] += quote_amt - fee["cost"]
        o = {"id": str(len(self.orders) + 1), "symbol": symbol, "side": side, "status": "closed", "timestamp": now_ms(),
             "clientOrderId": text, "fees": [fee],
             "info": {"text": text, "side": side, "filled_total": str(quote_amt), "filled_amount": str(base_amt),
                      "avg_deal_price": str(quote_amt / base_amt)}}
        self.orders.append(o)
        return o

    def create_market_buy_order_with_cost(self, symbol, cost, params):
        return self._fill(symbol, "buy", cost / self.last[symbol], cost, params.get("text", ""))

    def create_order(self, symbol, type, side, amount, price, params):
        return self._fill(symbol, side, amount, amount * self.last[symbol], params.get("text", ""))

    def fetch_order(self, oid, symbol):
        return next(o for o in self.orders if o["id"] == oid)

    def fetch_closed_orders(self, symbol, since=None, limit=100, params=None):
        page = int((params or {}).get("page", 1))
        mine = [o for o in self.orders if o["symbol"] == symbol and (since is None or o["timestamp"] >= since)]
        return mine[(page - 1) * limit : page * limit]


@pytest.fixture
def rot(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(ROT_CONFIG)
    cfg = _load(path, env={"KURUMI_STATE_DIR": str(tmp_path / "state")})
    bot = next(b for b in cfg.bots if b.name == "t-rot")
    return cfg, bot, tmp_path


def _state(cfg, mode="paper"):
    return json.loads((cfg.state_dir(mode) / "bots" / "t-rot.json").read_text())


def _rows(cfg, name, mode="paper"):
    p = cfg.state_dir(mode) / name
    return list(csv.DictReader(open(p))) if p.exists() else []


def test_rotation_config_shape(rot):
    cfg, bot, _ = rot
    assert bot.is_rotation and bot.symbol == "" and bot.symbols == ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    assert bot.quote == "USDT" and bot.label == "轮动 BTC/ETH/SOL" and bot.params["lookback"] == 30
    single = next(b for b in cfg.bots if b.name == "t-hodl")
    assert not single.is_rotation and single.label == "BTC/USDT" and single.symbols == ()


@pytest.mark.parametrize("patch, msg", [
    ('symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]', "缺少 symbols"),
    ('symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]\nsymbol = "BTC/USDT"', "不要再写 symbol"),
    ('symbols = ["BTC/USDT"]', "至少要两个"),
    ('symbols = "BTC/USDT,ETH/USDT"', "列表"),
    ('symbols = ["BTC/USDT", "btc/usdt"]', "重复"),
    ('symbols = ["BTC/USDT", "ETH/BTC"]', "计价货币"),
    ('symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]\nparams = { lookbak = 3 }', "不认识参数"),
    ('symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]\nparams = { score = "magic" }', "score"),
])
def test_rotation_config_validation(tmp_path, patch, msg):
    text = ROT_CONFIG.replace('symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]', patch)
    if msg == "缺少 symbols":
        text = ROT_CONFIG.replace('symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]\n', "")
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match=msg):
        _load(p, env={})


def test_rotation_config_rejects_stops_and_single_bot_symbols(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(ROT_CONFIG.replace("risk = { kill_switch = 0.5 }", "risk = { stop_loss = 0.1 }"))
    with pytest.raises(ValueError, match="kill_switch"):
        _load(p, env={})
    p.write_text(CONFIG + 'symbols = ["BTC/USDT", "ETH/USDT"]\n')
    with pytest.raises(ValueError, match="单币机器人写 symbol"):
        _load(p, env={})


def test_rotation_paper_buys_leader_then_holds_then_switches(rot):
    cfg, bot, _ = rot
    ex = MultiExchange(_daily(BTC=0.0, ETH=0.50, SOL=0.10))
    broker = PaperBroker(ex, fee=cfg.fee)
    r1 = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert r1["status"] == "ok" and r1["holding"] == "ETH/USDT" and [f["side"] for f in r1["fills"]] == ["buy"]
    assert r1["fills"][0]["quote"] == pytest.approx(100)
    r2 = rotation_tick(bot, cfg, ex=ex, broker=broker)  # 同一根 K 线再跑一次：不动
    assert r2["fills"] == [] and r2["holding"] == "ETH/USDT" and r2["exposure"] > 0.99
    st = _state(cfg)
    assert st["runs"] == 2 and st["symbol"] == "ETH/USDT" and st["wallet"]["holding"] == "ETH/USDT"
    assert st["symbols"] == ["BTC/USDT", "ETH/USDT", "SOL/USDT"] and len(st["orders"]) == 1

    ex.set(_daily(BTC=0.0, ETH=0.50, SOL=1.50))  # SOL 后来居上，强出一大截
    r3 = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert [(f["symbol"], f["side"]) for f in r3["fills"]] == [("ETH/USDT", "sell"), ("SOL/USDT", "buy")]
    assert r3["holding"] == "SOL/USDT" and r3["fill"]["symbol"] == "SOL/USDT"
    sell, buy = r3["fills"]
    assert buy["quote"] == pytest.approx(sell["quote"])  # 卖出所得全梭进新币
    st = _state(cfg)
    assert st["symbol"] == "SOL/USDT" and len(st["orders"]) == 3 and len(set(st["orders"])) == 3
    trades = _rows(cfg, "trades.csv")
    assert [(t["symbol"], t["side"]) for t in trades] == [("ETH/USDT", "buy"), ("ETH/USDT", "sell"), ("SOL/USDT", "buy")]
    assert len(_rows(cfg, "equity.csv")) == 3

    # 全篮子转跌（价位还在高处，不是熔断）：清仓拿现金
    ex.set(_daily(start={"SOL": 280.0}, BTC=-0.2, ETH=-0.3, SOL=-0.08))
    r4 = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert r4["event"] is None and "跑" in r4["reason"]
    assert [f["side"] for f in r4["fills"]] == ["sell"] and r4["holding"] is None and r4["price"] is None
    st = _state(cfg)
    assert st["symbol"] == "" and st["wallet"]["qty"] == 0 and st["wallet"]["holding"] is None
    r5 = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert r5["fills"] == [] and r5["plan"] is None
    text = render([r1, r3, r4, r5], "paper")
    assert "买入 ETH" in text and "卖出" in text and "买入 SOL" in text and "3 币轮动" in text and "空仓观望" in text


def test_rotation_dry_run_writes_nothing(rot):
    cfg, bot, _ = rot
    ex = MultiExchange(_daily(BTC=0.0, ETH=0.50, SOL=0.10))
    rep = rotation_tick(bot, cfg, ex=ex, broker=PaperBroker(ex, fee=0.002), dry_run=True)
    assert rep["plan"][0]["symbol"] == "ETH/USDT" and rep["plan"][0]["side"] == "buy" and rep["fills"] == []
    assert rep["holding"] is None and not cfg.state_dir("paper").exists()
    assert "dry-run" in render([rep], "paper")


def test_rotation_kill_switch_sells_and_halts(rot):
    cfg, bot, _ = rot
    ex = MultiExchange(_daily(BTC=0.0, ETH=0.50, SOL=0.10))
    broker = PaperBroker(ex, fee=0.002)
    rotation_tick(bot, cfg, ex=ex, broker=broker)
    ex.last["ETH/USDT"] *= 0.4  # 实时价腰斩还多：净值跌破预算一半
    rep = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert rep["event"] == "kill_switch" and rep["fills"][0]["side"] == "sell" and rep["halted"]
    ex.last["ETH/USDT"] /= 0.4
    rep = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert rep["event"] == "halted" and rep["fills"] == [] and _state(cfg)["wallet"]["halted"]


def test_rotation_skips_when_held_symbol_has_no_fresh_data(rot):
    cfg, bot, _ = rot
    ex = MultiExchange(_daily(BTC=0.0, ETH=0.50, SOL=0.10))
    broker = PaperBroker(ex, fee=0.002)
    rotation_tick(bot, cfg, ex=ex, broker=broker)
    stamp = _state(cfg)["updated_ms"]
    ex.broken = {"ETH/USDT"}
    rep = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert rep["status"] == "skip" and "ETH/USDT" in rep["reason"]
    assert len(_rows(cfg, "trades.csv")) == 1 and _state(cfg)["updated_ms"] == stamp  # 啥也没变就别存盘
    # 没拿着的币挂了不要紧：照常排名（跳过它）
    ex.broken = {"BTC/USDT"}
    rep = rotation_tick(bot, cfg, ex=ex, broker=broker)
    assert rep["status"] == "ok" and rep["holding"] == "ETH/USDT" and any("BTC/USDT" in n for n in rep["notes"])


def test_rotation_excludes_new_listing_until_it_has_history(rot):
    cfg, bot, _ = rot
    data = _daily(BTC=0.0, ETH=0.20, SOL=3.0)
    data["SOL/USDT"] = data["SOL/USDT"][-10:]  # 刚上市 10 天，涨疯了也先不碰
    ex = MultiExchange(data)
    rep = rotation_tick(bot, cfg, ex=ex, broker=PaperBroker(ex, fee=0.002))
    assert rep["holding"] == "ETH/USDT" and any("历史不够" in n and "SOL/USDT" in n for n in rep["notes"])


def test_single_bot_state_is_adopted_by_rotation(rot):
    cfg, bot, _ = rot
    ex = MultiExchange(_daily(BTC=0.0, ETH=0.50, SOL=0.10))
    # 原来是同名的单币 BTC 机器人，满仓 BTC
    w = Wallet.new(100)
    w.apply(Fill("buy", 100.0, 0.99, 100.0))
    sd = cfg.state_dir("paper")
    (sd / "bots").mkdir(parents=True)
    (sd / "bots" / "t-rot.json").write_text(json.dumps({"bot": "t-rot", "symbol": "BTC/USDT", "runs": 5, "orders": [],
                                                         "wallet": w.to_dict()}))
    rep = rotation_tick(bot, cfg, ex=ex, broker=PaperBroker(ex, fee=0.002))
    assert [(f["symbol"], f["side"]) for f in rep["fills"]] == [("BTC/USDT", "sell"), ("ETH/USDT", "buy")]
    assert _state(cfg)["runs"] == 6


def test_main_runs_single_and_rotation_bots(rot, monkeypatch):
    cfg, bot, tmp = rot
    ex = MultiExchange(_daily(BTC=0.0, ETH=0.50, SOL=0.10))
    hourly = make_candles([100.0 + i for i in range(60)], "1h", t0=(now_ms() // H - 60) * H)
    orig = ex.fetch_ohlcv
    ex.fetch_ohlcv = lambda s, tf, since=None, limit=1000: (
        [list(c) for c in hourly if since is None or c.ts >= since][:limit] if tf == "1h" else orig(s, tf, since, limit))
    summary = tmp / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("KURUMI_STATE_DIR", str(tmp / "state"))
    monkeypatch.setattr(run_mod, "make_exchange", lambda *a, **k: ex)
    assert run_mod.main(["--config", str(cfg.path)]) == 0
    text = summary.read_text()
    assert "t-hodl" in text and "t-rot" in text and "买入 ETH" in text and "轮动梭哈" in text


def test_rotation_live_switch_and_crash_recovery(rot, monkeypatch):
    """实盘：先买 ETH；换车时卖 ETH 买 SOL；假装买完 SOL 还没存盘就崩了，下一次要靠订单备注把 SOL 认领回来，不能重复买。"""
    cfg, bot, _ = rot
    gate = FakeGate(_daily(BTC=0.0, ETH=0.50, SOL=0.10), {"USDT": 1000.0})
    broker = LiveBroker(gate, max_order=1000)
    r1 = rotation_tick(bot, cfg, ex=gate, broker=broker)
    assert r1["holding"] == "ETH/USDT" and gate.balances["ETH"] > 0 and gate.balances["USDT"] == pytest.approx(900)

    snapshots = []
    real_save = run_mod.save_bot
    monkeypatch.setattr(run_mod, "save_bot", lambda d, n, st: (snapshots.append(copy.deepcopy(st)), real_save(d, n, st)))
    gate.set(_daily(BTC=0.0, ETH=0.50, SOL=1.50))
    r2 = rotation_tick(bot, cfg, ex=gate, broker=broker)
    assert [(f["symbol"], f["side"]) for f in r2["fills"]] == [("ETH/USDT", "sell"), ("SOL/USDT", "buy")]
    assert gate.balances["ETH"] < 1e-6 and gate.balances["SOL"] > 0
    after_sell = snapshots[0]  # 卖完 ETH 那一刻存的盘：现金、空仓、还没有 SOL 的订单号
    assert after_sell["wallet"]["holding"] is None and after_sell["wallet"]["qty"] == 0
    real_save(cfg.state_dir("live"), "t-rot", after_sell)  # 模拟：买单成交了但没来得及存盘

    n_orders = len(gate.orders)
    r3 = rotation_tick(bot, cfg, ex=gate, broker=broker)
    assert len(gate.orders) == n_orders  # 没有重复下单
    assert r3["holding"] == "SOL/USDT" and r3["fills"] == [] and any("找回" in n for n in r3["notes"])
    w = _state(cfg, "live")["wallet"]
    assert w["holding"] == "SOL/USDT" and w["qty"] == pytest.approx(gate.balances["SOL"])
    assert w["cash"] == pytest.approx(0, abs=1e-6)
    assert all(o["clientOrderId"].startswith(tag_prefix("t-rot")) for o in gate.orders)


class OrphanBroker:
    """对账时给某个币“找回”一笔漏记的买单（模拟 LiveBroker.sync 的行为）。"""

    mode = "live"

    def __init__(self, symbol, base, quote):
        self.symbol, self.base, self.quote, self.calls = symbol, base, quote, []

    def sync(self, wallet, symbol, price, prefix, known, since_ms):
        self.calls.append((symbol, wallet.holding, wallet.qty))
        if symbol == self.symbol and "o-1" not in known:
            known.append("o-1")
            wallet.apply(Fill("buy", self.quote / self.base, self.base, self.quote, order_id="o-1"))
            return [f"找回漏记的订单 o-1"]
        return []


def test_sync_rotation_merges_orphan_buy_and_refuses_conflicts():
    from bot.run import sync_rotation

    prices = {"BTC/USDT": 100.0, "ETH/USDT": 10.0, "SOL/USDT": 50.0}
    w, known = Wallet.new(100), []
    b = OrphanBroker("SOL/USDT", base=1.99, quote=100.0)
    notes = sync_rotation(b, w, list(prices), prices.get, "t-ks-x", known, 0)
    assert w.holding == "SOL/USDT" and w.qty == pytest.approx(1.99) and w.cash == pytest.approx(0) and known == ["o-1"]
    assert notes == ["找回漏记的订单 o-1"] and all(q == 0 for s, h, q in b.calls)  # 没拿着的币都用 qty=0 的影子钱包对
    assert sync_rotation(b, w, list(prices), prices.get, "t-ks-x", known, 0) == []  # 再对一次：不重复记
    assert b.calls[3] == ("SOL/USDT", "SOL/USDT", pytest.approx(1.99))  # 先对手里的币
    # 拿不到 SOL 的价格也照样对账：找回的仓位按成本价估，不会被当成零头丢掉
    w = Wallet.new(100)
    sync_rotation(OrphanBroker("SOL/USDT", base=1.99, quote=100.0), w, list(prices),
                  lambda s: None if s == "SOL/USDT" else prices[s], "t-ks-x", [], 0)
    assert w.holding == "SOL/USDT" and w.qty == pytest.approx(1.99) and w.cash == pytest.approx(0)
    # 手里已经有 BTC，又冒出一笔 ETH 买单：账本对不上，宁可报错也不乱记
    w = Wallet.new(100)
    w.holding = "BTC/USDT"
    w.apply(Fill("buy", 100.0, 0.5, 50.0))
    with pytest.raises(RuntimeError, match="对不上"):
        sync_rotation(OrphanBroker("ETH/USDT", 4.99, 50.0), w, list(prices), prices.get, "t-ks-x", [], 0)


# ---------------- 模式与安全闸 ----------------

PAPER_ONLY = CONFIG + """
[[bots]]
name = "t-monkey"
symbol = "BTC/USDT"
timeframe = "1h"
strategy = "monkey"
budget = 100
paper_only = true
"""


def test_paper_only_flag_parses_and_rejects_strings(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(PAPER_ONLY)
    cfg = load_config(p, env={})
    assert [b.paper_only for b in cfg.bots] == [False, True]
    p.write_text(PAPER_ONLY.replace("paper_only = true", 'paper_only = "true"'))
    with pytest.raises(ValueError, match="true 或 false"):
        load_config(p, env={})


def test_live_refused_on_github_hosted_runner(tmp_path, monkeypatch, capsys):
    p = tmp_path / "c.toml"
    p.write_text(PAPER_ONLY)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(run_mod, "make_exchange", lambda *a, **k: pytest.fail("不该连交易所"))
    for mode in ("live", "testnet"):
        assert run_mod.main(["--config", str(p), "--mode", mode]) == 2
    assert "self-hosted" in summary.read_text()


def test_live_skips_paper_only_bots(tmp_path, monkeypatch, capsys):
    p = tmp_path / "c.toml"
    p.write_text(PAPER_ONLY)
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    monkeypatch.setenv("KURUMI_STATE_DIR", str(tmp_path / "state"))
    seen = []

    class Broker:
        mode = "live"

        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(run_mod, "make_exchange", lambda *a, **k: object())
    monkeypatch.setattr(run_mod, "LiveBroker", Broker)
    monkeypatch.setattr(run_mod, "tick", lambda bot, *a, **k: seen.append(bot.name) or
                        {"bot": bot.name, "symbol": bot.symbol, "title": "t", "status": "skip", "reason": "", "notes": []})
    assert run_mod.main(["--config", str(p), "--mode", "live"]) == 0
    assert seen == ["t-hodl"] and "t-monkey" in capsys.readouterr().out


def test_paper_broker_respects_max_order():
    from bot.engine import OrderPlan

    ex = FakeExchange(make_candles([100.0] * 3))
    fill = PaperBroker(ex, fee=0.0, max_order=50).execute(OrderPlan("buy", cost=100), "BTC/USDT", 100.0, slippage=0, tag="t")
    assert fill.quote == pytest.approx(50)
