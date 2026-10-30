"""美股代币溢价：Gate 上的美股代币 vs 美股本尊。

xStocks（<代码>X/USDT）和 Ondo（<代码>ON/USDT）代币 7x24 小时交易，美股夜里和周末休市，
两边价格会拉开（周末溢价/折价）。每次快照记下：代币价格和盘口、美股最新价（Yahoo：盘中价 + 盘前盘后价）、
溢价 premium_pct = 代币 / 美股最新价 - 1（%），以及当时美股是盘前 / 盘中 / 盘后 / 休市。

- 代币价用盘口中间价（买一卖一价差 ≤ 1% 时），否则用最新成交价：夜里代币盘口薄，最新成交可能是
  很久以前的（实测出现过成交价比买一还低 0.4%），中间价更接近“现在能成交的价”。原始的成交价和买卖一也都记了。
- 美股最新价：有比收盘价更新的盘前/盘后价就用它，否则用盘中价/收盘价。
- 美股价是美元、代币是 USDT，两者之间还有零点几个千分点的汇差，看溢价时心里有数。
"""
from __future__ import annotations

from .common import Ctx, Http, append_csv, fmt_time, num, pct, whole, to_float
from .nyse import STATES, market_state

NAME = "stock_tokens"
TITLE = "美股代币溢价"
FILE = "stock_tokens.csv"
HEADER = ["time_utc", "market_state", "ticker", "issuer", "symbol", "token_last", "token_bid", "token_ask",
          "token_quote_volume", "stock_price", "stock_time_utc", "stock_ext_price", "stock_ext_time_utc", "premium_pct"]
SNAPSHOT = True
DEFAULTS = {"tickers": ["SPY", "QQQ", "NVDA", "TSLA", "AAPL"], "suffixes": ["X", "ON"]}
ISSUERS = {"X": "xstocks", "ON": "ondo"}
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_GIVE_UP = 2  # Yahoo 连续失败这么多次就不再请求了，省得每个代码都白等超时
MAX_SPREAD = 0.01  # 盘口价差超过 1% 就不信中间价，退回最新成交价


def token_price(t: dict) -> float | None:
    """算溢价用的代币价：盘口正常时用买一卖一中间价，否则用最新成交价。"""
    bid, ask, last = to_float(t.get("bid")), to_float(t.get("ask")), to_float(t.get("last"))
    if bid and ask and 0 < bid <= ask and (ask - bid) * 2 / (ask + bid) <= MAX_SPREAD:
        return (bid + ask) / 2
    return last


def _regular_periods(meta: dict) -> list[tuple[int, int]]:
    """Yahoo meta 里的盘中时段 [(开始, 结束)]（UTC 秒）。"""
    tp = meta.get("tradingPeriods")
    days = tp.get("regular") if isinstance(tp, dict) else tp  # includePrePost=true 时是 {pre, regular, post}
    periods = []
    for day in days or []:
        for p in day if isinstance(day, list) else [day]:
            if isinstance(p, dict) and p.get("start") is not None and p.get("end") is not None:
                periods.append((int(p["start"]), int(p["end"])))
    cur = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    if cur.get("start") is not None and cur.get("end") is not None:
        periods.append((int(cur["start"]), int(cur["end"])))
    return periods


def parse_chart(payload: dict) -> dict:
    """Yahoo chart 接口 → {price, time, ext_price, ext_time}（时间是 UTC 秒）。

    price/time 是 meta.regularMarketPrice/regularMarketTime（盘中价，收盘后就是收盘价）；
    最后一根有效 1 分钟 K 线如果比它新、又不在盘中时段，就是盘前/盘后价，记成 ext。
    """
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise ValueError(f"Yahoo 报错：{err.get('description') if isinstance(err, dict) else err}")
    result = (chart.get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    price, at = to_float(meta.get("regularMarketPrice")), to_float(meta.get("regularMarketTime"))
    if price is None or at is None:
        raise ValueError("Yahoo 没给 regularMarketPrice")
    out = {"price": price, "time": int(at), "ext_price": None, "ext_time": None}
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    regular = _regular_periods(meta)
    for ts, close in reversed(list(zip(result.get("timestamp") or [], quote.get("close") or []))):
        close = to_float(close)
        if close is None:
            continue
        if ts > at and not any(s <= ts < e for s, e in regular):
            out.update(ext_price=close, ext_time=int(ts))
        break  # 只看最后一个有效价格
    return out


def yahoo_quote(http: Http, ticker: str) -> dict:
    """先试 query1，不行换 query2（每个各请求一次，不再叠加重试）。"""
    err: Exception | None = None
    for host in YAHOO_HOSTS:
        try:
            payload = http.get_json(f"https://{host}/v8/finance/chart/{ticker}",
                                    params={"interval": "1m", "range": "1d", "includePrePost": "true"}, retries=0)
            return parse_chart(payload)
        except Exception as e:  # noqa: BLE001  换个域名再试一次
            err = e
    raise err  # type: ignore[misc]


def _issuer(suffix: str) -> str:
    return ISSUERS.get(suffix, suffix.lower())


def crawl(ctx: Ctx, opts: dict) -> str:
    tickers = [str(t).strip().upper() for t in opts["tickers"] if str(t).strip()]
    suffixes = [str(s).strip().upper() for s in opts["suffixes"] if str(s).strip()]
    book = ctx.gate.tickers()
    state = market_state(ctx.now)
    rows, unlisted, no_stock = [], [], []
    first_error = ""
    yahoo_fails = 0
    for ticker in tickers:
        tokens = [(s, f"{ticker}{s}/USDT") for s in suffixes if f"{ticker}{s}/USDT" in book]
        if not tokens:
            unlisted.append(ticker)
            continue
        quote = None
        if yahoo_fails < YAHOO_GIVE_UP:
            try:
                quote = yahoo_quote(ctx.http, ticker)
                yahoo_fails = 0
            except Exception as e:  # noqa: BLE001  美股价拿不到也照样记代币价
                yahoo_fails += 1
                first_error = first_error or str(e)
                no_stock.append(ticker)
        else:
            no_stock.append(ticker)
        for suffix, sym in tokens:
            t = book[sym]
            price = token_price(t)
            ref = None if quote is None else (quote["ext_price"] if quote["ext_price"] is not None else quote["price"])
            premium = (price / ref - 1) * 100 if price and ref else None
            rows.append({
                "time_utc": ctx.stamp, "market_state": state, "ticker": ticker, "issuer": _issuer(suffix), "symbol": sym,
                "token_last": num(t.get("last")), "token_bid": num(t.get("bid")), "token_ask": num(t.get("ask")),
                "token_quote_volume": whole(t.get("quoteVolume")),
                "stock_price": num(quote and quote["price"]), "stock_time_utc": fmt_time(quote and quote["time"]),
                "stock_ext_price": num(quote and quote["ext_price"]), "stock_ext_time_utc": fmt_time(quote and quote["ext_time"]),
                "premium_pct": pct(premium),
            })
    if not rows:
        raise ValueError(f"Gate 上一个美股代币都没找到（找的是 {', '.join(tickers)} + 后缀 {', '.join(suffixes)}）")
    append_csv(ctx.data_dir / FILE, HEADER, rows)

    msg = [f"{len(rows)} 个代币，美股{STATES[state]}"]
    priced = [(float(r["premium_pct"]), r["symbol"]) for r in rows if r["premium_pct"]]
    if priced:
        lo, hi = min(priced), max(priced)
        msg.append(f"溢价 {lo[0]:+.2f}%（{lo[1]}）~ {hi[0]:+.2f}%（{hi[1]}）")
    if no_stock:
        msg.append(f"美股价没拿到：{'、'.join(no_stock)}（{first_error or 'Yahoo 连续失败，没再请求'}），这几行溢价留空")
    if unlisted:
        msg.append(f"Gate 没有代币：{'、'.join(unlisted)}")
    return "；".join(msg)
