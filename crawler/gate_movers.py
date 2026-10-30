"""Gate 现货 24h 涨跌榜：只看 USDT 交易对、24h 成交额够大的币，记涨幅前 N 和跌幅前 N。

默认排除 BTC3L / ETH5S 这类杠杆代币：它们的涨跌是标的的好几倍，会把榜单刷满。
"""
from __future__ import annotations

import re

from .common import Ctx, append_csv, num, pct, to_float, wan, whole

NAME = "gate_movers"
TITLE = "Gate 涨跌榜"
FILE = "gate_movers.csv"
HEADER = ["time_utc", "kind", "rank", "symbol", "last", "change_24h_pct", "quote_volume"]
SNAPSHOT = True
DEFAULTS = {"min_quote_volume": 500_000, "top_n": 10, "exclude_leveraged": True, "quote": "USDT"}
LEVERAGED = re.compile(r"\d+[LS]$")  # Gate 杠杆代币的币名都是 数字 + L/S 结尾


def pick(tickers: dict, markets: dict, opts: dict) -> tuple[list[tuple], list[tuple], int]:
    """→ (涨幅榜, 跌幅榜, 达标的交易对个数)；榜单每项是 (symbol, 涨跌幅%, 成交额, 最新价)。"""
    min_qv, top_n, quote = float(opts["min_quote_volume"]), int(opts["top_n"]), str(opts["quote"]).upper()
    pool = []
    for sym, t in tickers.items():
        base, _, q = sym.partition("/")
        if q != quote:  # 顺带排除了合约（BTC/USDT:USDT）
            continue
        m = markets.get(sym) or {}
        if m.get("spot") is False or m.get("active") is False:
            continue
        if opts["exclude_leveraged"] and LEVERAGED.search(base):
            continue
        change, qv, last = to_float(t.get("percentage")), to_float(t.get("quoteVolume")), to_float(t.get("last"))
        if change is None or qv is None or last is None or qv < min_qv:
            continue
        pool.append((sym, change, qv, last))
    gainers = sorted((p for p in pool if p[1] > 0), key=lambda p: (-p[1], p[0]))[:top_n]
    losers = sorted((p for p in pool if p[1] < 0), key=lambda p: (p[1], p[0]))[:top_n]
    return gainers, losers, len(pool)


def crawl(ctx: Ctx, opts: dict) -> str:
    tickers = ctx.gate.tickers()
    gainers, losers, n = pick(tickers, ctx.gate.markets(), opts)
    if not gainers and not losers:
        raise ValueError(f"Gate 返回 {len(tickers)} 个 ticker，但没有一个达标（成交额门槛 {wan(float(opts['min_quote_volume']))} USDT）")
    rows = [
        {"time_utc": ctx.stamp, "kind": kind, "rank": i, "symbol": sym, "last": num(last),
         "change_24h_pct": pct(change), "quote_volume": whole(qv)}
        for kind, board in (("gainer", gainers), ("loser", losers))
        for i, (sym, change, qv, last) in enumerate(board, 1)
    ]
    append_csv(ctx.data_dir / FILE, HEADER, rows)
    parts = [f"{n} 个交易对 24h 成交额 ≥ {wan(float(opts['min_quote_volume']))} USDT"]
    if gainers:
        parts.append(f"领涨 {gainers[0][0]} {gainers[0][1]:+.2f}%")
    if losers:
        parts.append(f"领跌 {losers[0][0]} {losers[0][1]:+.2f}%")
    return "，".join(parts)
