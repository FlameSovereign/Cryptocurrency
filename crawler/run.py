"""爬虫入口（GitHub Actions 每隔几小时跑一次，见 .github/workflows/crawl.yml）。

    python -m crawler.run                              # 跑 config.toml [crawler] 里启用的全部数据源
    python -m crawler.run --only fear_greed,trending   # 只跑这几个
    python -m crawler.run --data-dir /tmp/data         # 换个存放目录（默认 data/）

数据源互相独立，一个挂了不影响别的；至少一个成功（或者刚爬过跳过）就返回 0，全挂才返回 1。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import tomllib
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from . import fear_greed, gate_movers, stock_tokens, trending
from .common import Ctx, Gate, Http, Skip, last_row, parse_time, utc_now

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {m.NAME: m for m in (fear_greed, trending, gate_movers, stock_tokens)}
DEFAULTS = {
    "sources": list(SOURCES),
    "min_interval_minutes": 20,  # 快照类数据离上一份不到这么久就不重复记（手动重跑不会刷出重复行）
    "timeout": 20,
    "retries": 1,
}
ICONS = {"ok": "✅", "skip": "⏭️", "fail": "❌"}
WORDS = {"ok": "成功", "skip": "跳过", "fail": "失败"}


@dataclass
class Outcome:
    name: str
    status: str  # ok / skip / fail
    message: str
    seconds: float = 0.0


def load_settings(path: str | Path) -> tuple[dict, list[str]]:
    """读 config.toml 的 [crawler] 表。文件没有或者写坏了就全用默认值继续爬，只打个警告。"""
    p = Path(path)
    if not p.exists():
        return {}, []
    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return {}, [f"{p.name} 读不了（{e}），全部用默认参数"]
    table = raw.get("crawler", {})
    if not isinstance(table, dict):
        return {}, ["[crawler] 应该是一个表，忽略"]
    return table, []


def _same_type(value, default) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, type(default))


def _merge(defaults: Mapping, given: Mapping, where: str, warnings: list[str], extra: set[str] = frozenset()) -> dict:
    out = dict(defaults)
    for k, v in given.items():
        if k in extra:
            continue
        if k not in defaults:
            warnings.append(f"{where} 里有不认识的参数 {k!r}，忽略")
        elif not _same_type(v, defaults[k]):
            warnings.append(f"{where}.{k} = {v!r} 类型不对，用默认值 {defaults[k]!r}")
        else:
            out[k] = v
    return out


def resolve(settings: Mapping) -> tuple[dict, dict[str, dict], list[str]]:
    """合并默认参数 → (全局参数, {数据源: 参数}, 警告)。写错的参数只警告不报错：爬虫不该因为配置小毛病罢工。"""
    warnings: list[str] = []
    top = _merge(DEFAULTS, settings, "[crawler]", warnings, extra=set(SOURCES))
    per = {}
    for name, mod in SOURCES.items():
        sub = settings.get(name, {})
        if not isinstance(sub, dict):
            warnings.append(f"[crawler.{name}] 应该是一个表，忽略")
            sub = {}
        per[name] = _merge(mod.DEFAULTS, sub, f"[crawler.{name}]", warnings)
    return top, per, warnings


def run_one(name: str, ctx: Ctx, opts: dict, min_interval: float) -> Outcome:
    mod = SOURCES[name]
    t0 = time.monotonic()
    try:
        if mod.SNAPSHOT and min_interval > 0:
            prev = last_row(ctx.data_dir / mod.FILE) or {}
            prev_t = parse_time(prev.get("time_utc"))
            if prev_t and timedelta(0) <= ctx.now - prev_t < timedelta(minutes=min_interval):
                raise Skip(f"上一份快照是 {prev['time_utc']} UTC，不到 {min_interval:g} 分钟，这次不重复记")
        status, msg = "ok", mod.crawl(ctx, opts)
    except Skip as e:
        status, msg = "skip", str(e)
    except Exception as e:  # noqa: BLE001  一个数据源挂了不能拖累别的
        traceback.print_exc(file=sys.stderr)
        status, msg = "fail", f"{type(e).__name__}: {e}"
    return Outcome(name, status, msg, time.monotonic() - t0)


def _counts(outcomes: list[Outcome]) -> str:
    return " · ".join(f"{WORDS[s]} {n}" for s in WORDS if (n := sum(o.status == s for o in outcomes)))


def render_text(outcomes: list[Outcome], ctx: Ctx) -> str:
    lines = [f"🕷️ 爬虫 · {ctx.stamp} UTC · 存到 {ctx.data_dir}"]
    lines += [f"{ICONS[o.status]} {SOURCES[o.name].TITLE}（{o.name}，{o.seconds:.1f}s）：{o.message}" for o in outcomes]
    lines.append(_counts(outcomes))
    return "\n".join(lines)


def render_markdown(outcomes: list[Outcome], ctx: Ctx) -> str:
    def cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ")

    lines = [f"## 🕷️ 爬虫 · {ctx.stamp} UTC", "", "| 数据源 | 结果 | 说明 | 耗时 |", "|---|---|---|---|"]
    lines += [f"| {SOURCES[o.name].TITLE} `{o.name}` | {ICONS[o.status]} {WORDS[o.status]} | {cell(o.message)} | {o.seconds:.1f}s |"
              for o in outcomes]
    return "\n".join(lines + ["", _counts(outcomes), ""]) + "\n"


def main(argv: list[str] | None = None, *, http: Http | None = None, gate: Gate | None = None,
         now: datetime | None = None) -> int:
    from bot.run import utf8_stdout

    utf8_stdout()  # 中文和 emoji 在 Windows 默认的 GBK 控制台上别崩
    ap = argparse.ArgumentParser(description="爬虫：恐惧贪婪指数 / CoinGecko 热搜 / Gate 涨跌榜 / 美股代币溢价")
    ap.add_argument("--only", help=f"只跑这些数据源（逗号分隔）：{', '.join(SOURCES)}")
    ap.add_argument("--data-dir", default="data", help="CSV 存放目录（默认 data）")
    ap.add_argument("--config", default=str(ROOT / "config.toml"), help="配置文件，读里面的 [crawler] 表")
    args = ap.parse_args(argv)

    settings, warnings = load_settings(args.config)
    top, per, more = resolve(settings)
    warnings += more
    if args.only:
        names = [s.strip() for s in args.only.split(",") if s.strip()]
        bad = [n for n in names if n not in SOURCES]
        if bad:
            ap.error(f"没有这些数据源: {bad}，可选: {', '.join(SOURCES)}")
    else:
        names = [n for n in top["sources"] if n in SOURCES]
        bad = [n for n in top["sources"] if n not in SOURCES]
        if bad:
            warnings.append(f"[crawler].sources 里有不认识的数据源 {bad}，忽略（可选: {', '.join(SOURCES)}）")
    names = list(dict.fromkeys(names))
    for w in warnings:
        print(f"⚠️ {w}", file=sys.stderr)
    if not names:
        print("没有要跑的数据源，下班。")
        return 0

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(data_dir=data_dir, now=now or utc_now(),
              http=http or Http(timeout=top["timeout"], retries=top["retries"]),
              gate=gate or Gate(timeout=top["timeout"], retries=top["retries"]))
    outcomes = [run_one(n, ctx, per[n], float(top["min_interval_minutes"])) for n in names]

    print(render_text(outcomes, ctx))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(render_markdown(outcomes, ctx))
        except OSError as e:
            print(f"⚠️ 写 GITHUB_STEP_SUMMARY 失败：{e}", file=sys.stderr)
    return 0 if any(o.status != "fail" for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
