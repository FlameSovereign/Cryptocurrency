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
