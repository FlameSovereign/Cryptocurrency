"""读取 config.toml。写错的地方尽量在启动时就报出来，别等到下单那一刻。"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .candles import timeframe_ms
from .engine import Risk
from .strategies import get as get_strategy

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.toml"
MODES = ("paper", "live")


@dataclass(frozen=True)
class BotConfig:
    name: str
    symbol: str
    timeframe: str
    strategy: str
    budget: float
    params: dict = field(default_factory=dict)
    size: float = 1.0  # 梭哈系数
    risk: Risk = Risk()
    slippage: float | None = None  # 覆盖全局滑点（股票代币盘口薄，可以设大点）
    enabled: bool = True

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote(self) -> str:
        return self.symbol.split("/")[1].split(":")[0]


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


def _bot(raw: Mapping, i: int) -> BotConfig:
    where = f"bots[{i}]"
    for key in ("name", "symbol", "timeframe", "strategy", "budget"):
        if key not in raw:
            raise ValueError(f"{where} 缺少 {key}")
    unknown = sorted(set(raw) - {"name", "symbol", "timeframe", "strategy", "budget", "params", "size", "risk", "slippage", "enabled"})
    if unknown:
        raise ValueError(f"{where} 有不认识的字段 {unknown}")
    name = str(raw["name"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
        raise ValueError(f"{where}.name={name!r} 只能用小写字母、数字、- 和 _，最长 32 位")
    symbol = str(raw["symbol"]).upper()
    if "/" not in symbol:
        raise ValueError(f"{where}.symbol={symbol!r} 要写成 BTC/USDT 这种格式")
    timeframe = str(raw["timeframe"])
    timeframe_ms(timeframe)
    strat = get_strategy(str(raw["strategy"]))
    params = strat.resolve(raw.get("params"))
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
    slippage = raw.get("slippage")
    if slippage is not None:
        slippage = _num(raw, "slippage", 0.0, where)
    return BotConfig(
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        strategy=strat.name,
        budget=budget,
        params=params,
        size=size,
        risk=risk,
        slippage=slippage,
        enabled=bool(raw.get("enabled", True)),
    )


def load_config(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = Path(path) if path else DEFAULT_PATH
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    ex = raw.get("exchange") or {}
    mode = (env.get("BOT_MODE") or ex.get("mode") or "paper").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode 只能是 paper 或 live，收到 {mode!r}")

    bots = tuple(_bot(b, i) for i, b in enumerate(raw.get("bots") or []))
    names = [b.name for b in bots]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"机器人重名: {dupes}")

    live = raw.get("live") or {}
    return Config(
        exchange=str(ex.get("id", "gate")),
        mode=mode,
        fee=_num(ex, "fee", 0.002, "exchange"),
        slippage=_num(ex, "slippage", 0.0005, "exchange"),
        rebalance_threshold=_num(ex, "rebalance_threshold", 0.02, "exchange"),
        max_order=_num(live, "max_order", 1000.0, "live"),
        bots=bots,
        research=dict(raw.get("research") or {}),
        crawler=dict(raw.get("crawler") or {}),
        notify=dict(raw.get("notify") or {}),
        path=path,
        state_root=Path(env.get("KURUMI_STATE_DIR") or ROOT / "state"),
    )
