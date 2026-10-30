"""策略注册表。

一个策略就是一个函数：

    fn(candles, position, params, ctx) -> float | tuple[float, str]

- candles：已收盘 K 线（旧 → 新），长度恰好是 warmup(params)
- position：当前仓位 0~1（已经按梭哈系数归一化，1 = 满仓）
- params：配置里的参数（已和 defaults 合并）
- ctx：Context，带周期、决策时点、恐惧贪婪指数等外部数据
- 返回：目标仓位 0~1（1 = 梭哈，0 = 空仓），可以顺带一句人话说明

硬性要求：同样的输入必须给出同样的输出（幂等）。GitHub Actions 会延迟、会重跑、会漏跑，
幂等的“目标仓位”写法保证不管跑几次都不会重复下单。

新增策略：在本目录新建一个 .py 文件，用 @register(...) 装饰即可，会被自动发现。
"""
from __future__ import annotations

import bisect
import importlib
import pkgutil
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping


class FearGreed:
    """恐惧贪婪指数时间序列（alternative.me），按 UTC 日期存储。"""

    def __init__(self, by_day_ms: Mapping[int, int]):
        self._ts = sorted(by_day_ms)
        self._val = [int(by_day_ms[t]) for t in self._ts]

    def __len__(self) -> int:
        return len(self._ts)

    def at(self, asof_ms: int, lag_ms: int = 3_600_000) -> int | None:
        """决策时点 asof_ms 之前“已经发布”的最新值。

        指数每天 UTC 0 点左右更新；默认认为发布 1 小时后才可用，回测和实盘用同一个规则，避免偷看未来。
        """
        i = bisect.bisect_right(self._ts, asof_ms - lag_ms) - 1
        return self._val[i] if i >= 0 else None


@dataclass(frozen=True)
class Context:
    timeframe: str
    asof_ms: int  # 决策时点 = 最后一根已收盘 K 线的收盘时间（不是墙上时间，保证幂等）
    symbol: str = ""
    fear_greed: FearGreed | None = None


@dataclass(frozen=True)
class Strategy:
    name: str
    title: str  # 中文名
    school: str  # 流派
    summary: str  # 一句话说明
    defaults: Mapping[str, object]
    warmup: Callable[[Mapping[str, object]], int]  # 需要多少根已收盘 K 线
    fn: Callable
    needs: tuple[str, ...] = ()  # 外部数据依赖，比如 ("fear_greed",)
    timeframes: tuple[str, ...] = field(default=())  # 推荐周期，仅作说明

    def resolve(self, params: Mapping[str, object] | None) -> dict:
        """合并默认参数；写错参数名直接报错，省得配置里打错字还以为生效了。"""
        params = dict(params or {})
        unknown = sorted(set(params) - set(self.defaults))
        if unknown:
            raise ValueError(f"策略 {self.name} 不认识参数 {unknown}，可用参数: {sorted(self.defaults)}")
        merged = {**self.defaults, **params}
        for k, v in self.defaults.items():
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (int, float)) and not isinstance(merged[k], (int, float)):
                raise ValueError(f"策略 {self.name} 参数 {k} 应该是数字，收到 {merged[k]!r}")
        return merged

    def lookback(self, params: Mapping[str, object]) -> int:
        n = int(self.warmup(params))
        if n < 1:
            raise ValueError(f"策略 {self.name} warmup 必须 >= 1")
        return n


REGISTRY: dict[str, Strategy] = {}
LOAD_ERRORS: dict[str, str] = {}  # 导入失败的策略文件：不让一个坏文件拖垮所有机器人，但 get() 会把原因带出来
_loaded = False


def register(
    name: str,
    *,
    title: str,
    school: str,
    summary: str,
    defaults: Mapping[str, object],
    warmup: Callable[[Mapping[str, object]], int],
    needs: tuple[str, ...] = (),
    timeframes: tuple[str, ...] = (),
):
    def deco(fn):
        if name in REGISTRY:
            raise ValueError(f"策略重名: {name}")
        REGISTRY[name] = Strategy(name, title, school, summary, dict(defaults), warmup, fn, tuple(needs), tuple(timeframes))
        return fn

    return deco


def load_all() -> dict[str, Strategy]:
    global _loaded
    if not _loaded:
        for m in pkgutil.iter_modules(__path__):
            if m.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{__name__}.{m.name}")
            except Exception as e:
                LOAD_ERRORS[m.name] = f"{type(e).__name__}: {e}"
                print(f"⚠️ 策略文件 {m.name}.py 导入失败，里面的策略暂时不可用：{LOAD_ERRORS[m.name]}", file=sys.stderr)
        _loaded = True
    return REGISTRY


def get(name: str) -> Strategy:
    load_all()
    try:
        return REGISTRY[name]
    except KeyError:
        broken = "".join(f"\n  {m}.py 导入失败：{err}" for m, err in LOAD_ERRORS.items())
        raise KeyError(f"没有叫 {name!r} 的策略，可选: {', '.join(sorted(REGISTRY))}{broken}") from None


def normalize(result) -> tuple[float, str]:
    """把策略返回值统一成 (0~1 的目标仓位, 说明)。NaN/None 一律当 0。"""
    note = ""
    if isinstance(result, tuple):
        result, note = result[0], str(result[1])
    try:
        x = float(result)
    except (TypeError, ValueError):
        x = 0.0
    if x != x:  # NaN
        x = 0.0
    return min(1.0, max(0.0, x)), note
