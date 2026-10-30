"""读取 config.toml。写错的地方尽量在启动时就报出来，别等到下单那一刻。"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import rotation
from .candles import timeframe_ms
from .engine import Risk
from .strategies import get as get_strategy

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.toml"
MODES = ("paper", "live", "testnet")
BOT_KEYS = {"name", "symbol", "symbols", "timeframe", "strategy", "budget", "params", "size", "risk", "slippage", "enabled",
            "paper_only", "reset"}


@dataclass(frozen=True)
class BotConfig:
    name: str
    symbol: str  # 单币机器人的交易对；轮动机器人为空字符串
    timeframe: str
    strategy: str
    budget: float
    params: dict = field(default_factory=dict)
    size: float = 1.0  # 梭哈系数
    risk: Risk = Risk()
    slippage: float | None = None  # 覆盖全局滑点（股票代币盘口薄，可以设大点）
    enabled: bool = True
    symbols: tuple[str, ...] = ()  # 轮动机器人的篮子（单币机器人为空）
    paper_only: bool = False  # 只在模拟盘跑（对照组用）：切到实盘/测试网时自动跳过，永远不碰真钱
    reset: str | None = None  # 改成一个新值 = 下次运行清空账本重新开始（熔断后复活也用它）

    @property
    def is_rotation(self) -> bool:
        return rotation.is_rotation(self.strategy)

    @property
    def label(self) -> str:
        """报告里显示的标的：单币就是交易对，轮动是篮子。"""
        if self.is_rotation:
            return "轮动 " + "/".join(rotation.base(s) for s in self.symbols)
        return self.symbol

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote(self) -> str:
        return (self.symbol or self.symbols[0]).split("/")[1].split(":")[0]


@dataclass(frozen=True)
class Config:
    exchange: str
    mode: str
    fee: float
    slippage: float
    rebalance_threshold: float
    max_order: float
    bots: tuple[BotConfig, ...]
    research: dict
    crawler: dict
    notify: dict
    path: Path
    state_root: Path = ROOT / "state"

    def state_dir(self, mode: str | None = None) -> Path:
        return self.state_root / (mode or self.mode)


def _num(d: Mapping, key: str, default: float, where: str) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{where}.{key} 应该是数字，收到 {v!r}")
    return float(v)


def _reset(d: Mapping, where: str) -> str | None:
    v = d.get("reset")
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int)) or not str(v).strip():
        raise ValueError(f'{where}.reset 写一个字符串，比如 reset = "2026-11-01"（每次想重置就换个新值）')
    return str(v).strip()


def _flag(d: Mapping, key: str, default: bool, where: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(f"{where}.{key} 要写 true 或 false（不加引号），收到 {v!r}")
    return v


def _symbols(value, where: str) -> tuple[str, ...]:
    """轮动篮子：至少两个交易对、不重复、计价货币一致（钱包里的现金只有一种）。"""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f'{where}.symbols 要写成列表，比如 ["BTC/USDT", "ETH/USDT", "SOL/USDT"]')
    syms = tuple(str(s).strip().upper() for s in value)
    if len(syms) < 2:
        raise ValueError(f"{where}.symbols 至少要两个交易对，一个币没法轮动")
    bad = [s for s in syms if "/" not in s]
    if bad:
        raise ValueError(f"{where}.symbols 里的 {bad} 要写成 BTC/USDT 这种格式")
    dupes = sorted({s for s in syms if syms.count(s) > 1})
    if dupes:
        raise ValueError(f"{where}.symbols 有重复的交易对 {dupes}")
    quotes = sorted({s.split("/")[1].split(":")[0] for s in syms})
    if len(quotes) > 1:
        raise ValueError(f"{where}.symbols 的计价货币要一致（钱包里只有一种现金），收到 {quotes}")
    return syms


def _bot(raw: Mapping, i: int) -> BotConfig:
    where = f"bots[{i}]"
    rotating = rotation.is_rotation(str(raw.get("strategy", "")))
    for key in ("name", "symbols" if rotating else "symbol", "timeframe", "strategy", "budget"):
        if key not in raw:
            raise ValueError(f"{where} 缺少 {key}")
    unknown = sorted(set(raw) - BOT_KEYS)
    if unknown:
        raise ValueError(f"{where} 有不认识的字段 {unknown}")
    if rotating and "symbol" in raw:
        raise ValueError(f"{where} 是轮动机器人，交易对都写进 symbols 列表，不要再写 symbol")
    if not rotating and "symbols" in raw:
        raise ValueError(f"{where}.symbols 只有轮动策略（{', '.join(sorted(rotation.ROTATIONS))}）才用，单币机器人写 symbol")
    name = str(raw["name"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
        raise ValueError(f"{where}.name={name!r} 只能用小写字母、数字、- 和 _，最长 32 位")
    timeframe = str(raw["timeframe"])
    if rotating:
        symbol, symbols = "", _symbols(raw["symbols"], where)
        timeframe_ms(timeframe)
        strategy = str(raw["strategy"])
        params = rotation.resolve(strategy, raw.get("params"))
    else:
        symbol, symbols = str(raw["symbol"]).upper(), ()
        if "/" not in symbol:
            raise ValueError(f"{where}.symbol={symbol!r} 要写成 BTC/USDT 这种格式")
        timeframe_ms(timeframe)
        strat = get_strategy(str(raw["strategy"]))
        strategy, params = strat.name, strat.resolve(raw.get("params"))
    budget = _num(raw, "budget", 0, where)
    if budget <= 0:
        raise ValueError(f"{where}.budget 必须大于 0")
    size = _num(raw, "size", 1.0, where)
    if not 0 < size <= 1:
        raise ValueError(f"{where}.size（梭哈系数）必须在 (0, 1] 之间")
    risk_raw = dict(raw.get("risk") or {})
    bad = sorted(set(risk_raw) - {"stop_loss", "take_profit", "trailing_stop", "kill_switch"})
    if bad:
        raise ValueError(f"{where}.risk 有不认识的字段 {bad}")
    risk = Risk(**{k: _num(risk_raw, k, 0.0, where + ".risk") for k in risk_raw})
    if rotating and (risk.stop_loss or risk.take_profit or risk.trailing_stop):
        raise ValueError(f"{where}.risk：轮动机器人目前只支持 kill_switch（止损/止盈/移动止损在换车之后不好定义，先不做）")
    slippage = raw.get("slippage")
    if slippage is not None:
        slippage = _num(raw, "slippage", 0.0, where)
    return BotConfig(
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        budget=budget,
        params=params,
        size=size,
        risk=risk,
        slippage=slippage,
        enabled=_flag(raw, "enabled", True, where),
        symbols=symbols,
        paper_only=_flag(raw, "paper_only", False, where),
        reset=_reset(raw, where),
    )


TOP_KEYS = {"exchange", "live", "bots", "crawler", "notify", "research"}
EXCHANGE_KEYS = {"id", "mode", "fee", "slippage", "rebalance_threshold"}
LIVE_KEYS = {"max_order"}


def _known(d: Mapping, allowed: set, where: str) -> None:
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise ValueError(f"{where} 有不认识的字段 {unknown}（可用：{sorted(allowed)}）——是不是写错了？写错的字段会被悄悄忽略，所以直接报错")


def _between(value: float, lo: float, hi: float, name: str, hint: str) -> float:
    if not lo <= value < hi:
        raise ValueError(f"{name} = {value:g} 超出范围 [{lo:g}, {hi:g})：{hint}")
    return value


def load_config(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = Path(path) if path else DEFAULT_PATH
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    _known(raw, TOP_KEYS, "config.toml")
    ex = raw.get("exchange") or {}
    _known(ex, EXCHANGE_KEYS, "[exchange]")
    _known(raw.get("live") or {}, LIVE_KEYS, "[live]")
    mode = (env.get("BOT_MODE") or ex.get("mode") or "paper").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode 只能是 paper / live / testnet，收到 {mode!r}")

    bots = tuple(_bot(b, i) for i, b in enumerate(raw.get("bots") or []))
    names = [b.name for b in bots]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"机器人重名: {dupes}")

    live = raw.get("live") or {}
    pct = "比例写小数，0.002 = 0.2%"
    max_order = _num(live, "max_order", 500.0, "live")
    if max_order <= 0:
        raise ValueError("[live] max_order 必须大于 0")
    return Config(
        exchange=str(ex.get("id", "gate")),
        mode=mode,
        fee=_between(_num(ex, "fee", 0.002, "exchange"), 0, 0.05, "[exchange] fee", pct),
        slippage=_between(_num(ex, "slippage", 0.0005, "exchange"), 0, 0.05, "[exchange] slippage", pct),
        rebalance_threshold=_between(_num(ex, "rebalance_threshold", 0.02, "exchange"), 0, 0.5,
                                     "[exchange] rebalance_threshold", "比例写小数，0.02 = 2%"),
        max_order=max_order,
        bots=bots,
        research=dict(raw.get("research") or {}),
        crawler=dict(raw.get("crawler") or {}),
        notify=dict(raw.get("notify") or {}),
        path=path,
        state_root=Path(env.get("KURUMI_STATE_DIR") or ROOT / "state"),
    )
