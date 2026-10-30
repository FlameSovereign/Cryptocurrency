"""策略排行榜：所有注册策略用默认参数、同一套成本，在加密篮子和股票代币篮子上各跑一遍。

    python -m research.leaderboard                 # 写 research/leaderboard.md
    python -m research.leaderboard --only sma_cross,donchian --tf 4h

口径：
- 全样本（full）+ 样本外（out：最后 40%，之前没调过参的那段）
- “赢囤币”= 收益高于同期买入持有；“赢猴子”= 收益高于 30 只随机猴子的 90 分位
- 中位数而不是平均数：一个币暴涨不该把整个策略抬上天
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from bot.strategies import load_all

from . import harness as H

OUT = Path(__file__).resolve().parent / "leaderboard.md"
BASKETS = {"加密": H.CRYPTO, "股票代币": H.STOCKS}


def _one(job):
    name, sym, tf, fee = job
    out = {}
    try:
        full = H.backtest(name, sym, tf, fee=fee)
        cut = H.split_ts(sym, tf, 0.6)
        oos = H.backtest(name, sym, tf, start_ms=cut, fee=fee)
    except (ValueError, FileNotFoundError) as e:
        return job, {"error": str(e)}
    for seg, r in (("full", full), ("out", oos)):
        out[seg] = {"ret": r.total_return, "hodl": r.hodl_return, "mdd": r.max_drawdown, "hodl_mdd": r.hodl_max_drawdown,
                    "sharpe": r.sharpe, "fills": r.fills, "exposure": r.exposure, "fees": r.fees_paid}
    return job[:3], out


def _monkey(job):
    """猴子和策略从同一根 K 线起跑：full 段从策略 warmup 结束处开始，out 段从切分点开始。"""
    sym, tf, start_ms, fee = job
    try:
        _, p90 = H.monkey_band(sym, tf, start_ms=start_ms, fee=fee)
        cut = H.split_ts(sym, tf, 0.6)
        _, p90_out = H.monkey_band(sym, tf, start_ms=max(cut, start_ms), fee=fee)
    except (ValueError, FileNotFoundError):
        return job[:3], None
    return job[:3], {"full": p90, "out": p90_out}


def _start_ms(name: str, sym: str, tf: str) -> int | None:
    return H.first_decision_ms(name, sym, tf)


def build(names: list[str], timeframes: list[str], workers: int, fee: float = H.FEE) -> dict:
    syms = H.CRYPTO + H.STOCKS
    jobs = [(n, s, tf) for n in names for tf in timeframes for s in syms]
    starts = {j: _start_ms(*j) for j in jobs}
    monkey_jobs = sorted({(s, tf, st, fee) for (n, s, tf), st in starts.items() if st is not None})
    with ProcessPoolExecutor(max_workers=workers) as pool:
        bands = dict(pool.map(_monkey, monkey_jobs, chunksize=2))
        results = dict(pool.map(_one, [j + (fee,) for j in jobs], chunksize=4))
    monkeys = {j: bands.get((j[1], j[2], st)) for j, st in starts.items() if st is not None}
    return {"results": results, "monkeys": monkeys}


def _agg(name, rows, seg, monkeys, tf):
    ok = [(s, r[seg]) for s, r in rows if seg in r]
    if not ok:
        return None
    med = statistics.median
    beat_hodl = sum(x["ret"] > x["hodl"] + 1e-6 for _, x in ok)
    band = {s: monkeys.get((name, s, tf)) for s, _ in ok}
    beat_monkey = sum(1 for s, x in ok if band[s] and x["ret"] > band[s][seg])
    return {"n": len(ok), "ret": med(x["ret"] for _, x in ok), "hodl": med(x["hodl"] for _, x in ok),
            "mdd": med(x["mdd"] for _, x in ok), "hodl_mdd": med(x["hodl_mdd"] for _, x in ok),
            "sharpe": med(x["sharpe"] for _, x in ok), "fills": med(x["fills"] for _, x in ok),
            "exposure": med(x["exposure"] for _, x in ok), "beat_hodl": beat_hodl, "beat_monkey": beat_monkey}


def render(data: dict, names: list[str], timeframes: list[str], fee: float = H.FEE) -> str:
    reg = load_all()
    res, monkeys = data["results"], data["monkeys"]
    lines = ["# 策略排行榜", "",
             f"成本：手续费 {fee:.1%}/边，滑点 {H.DEFAULT_SLIPPAGE:.2%}（GT {H.SLIPPAGE['GT/USDT']:.1%}，股票代币 0.1%），"
             f"本金 {H.BUDGET:.0f} USDT，最小下单 {H.MIN_COST:.0f} USDT。数据：Gate 现货 K 线。",
             "全部用策略默认参数；“样本外”= 每个标的最后 40% 的时间段。中位数统计。", ""]
    for basket, syms in BASKETS.items():
        for tf in timeframes:
            for seg, seg_name in (("full", "全样本"), ("out", "样本外")):
                rows = []
                for n in names:
                    a = _agg(n, [(s, res.get((n, s, tf), {})) for s in syms], seg, monkeys, tf)
                    if a:
                        rows.append((n, a))
                if not rows:
                    continue
                rows.sort(key=lambda x: (-x[1]["sharpe"], -x[1]["ret"]))
                lines += [f"## {basket} · {tf} · {seg_name}", "",
                          "| 策略 | 流派 | 收益中位数 | 囤币 | 最大回撤 | 囤币回撤 | 夏普 | 成交笔数 | 持仓时间 | 赢囤币 | 赢猴子p90 |",
                          "|---|---|---|---|---|---|---|---|---|---|---|"]
                for n, a in rows:
                    s = reg[n]
                    lines.append(f"| {n} {s.title} | {s.school} | {a['ret']:+.0%} | {a['hodl']:+.0%} | {a['mdd']:.0%} | {a['hodl_mdd']:.0%} "
                                 f"| {a['sharpe']:.2f} | {a['fills']:.0f} | {a['exposure']:.0%} | {a['beat_hodl']}/{a['n']} | {a['beat_monkey']}/{a['n']} |")
                lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--tf", default="1h,4h,1d")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--json")
    ap.add_argument("--fee", type=float, default=H.FEE, help="手续费率（默认 0.002；Gate 帮助中心写 VIP0 是 0.001）")
    a = ap.parse_args(argv)
    reg = load_all()
    names = [n for n in (a.only.split(",") if a.only else sorted(reg)) if not n.startswith("_")]
    tfs = a.tf.split(",")
    data = build(names, tfs, a.workers, a.fee)
    md = render(data, names, tfs, a.fee)
    Path(a.out).write_text(md + "\n")
    if a.json:
        Path(a.json).write_text(json.dumps({f"{k[0]}|{k[1]}|{k[2]}": v for k, v in data["results"].items()}, ensure_ascii=False))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
