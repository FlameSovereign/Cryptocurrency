"""爬虫：全部用离线 fixture（从真实接口返回裁剪而来），不联网。"""
import csv
from datetime import datetime, timedelta, timezone

import pytest
import requests

from crawler import fear_greed, gate_movers, nyse, run, stock_tokens, trending
from crawler.common import Ctx, Gate, Http, HttpError, append_csv, last_row, read_csv, user_agent

UTC = timezone.utc
NOW = datetime(2026, 10, 30, 6, 30, tzinfo=UTC)  # 周五凌晨 2:30（纽约），美股休市


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """兜底：哪个测试忘了打桩，真请求一律拦下。"""
    def blocked(*a, **k):
        raise AssertionError("测试里不许真的联网")

    monkeypatch.setattr(requests.Session, "request", blocked)

# ---------------- fixtures（真实返回裁剪） ----------------

FNG = {
    "name": "Fear and Greed Index",
    "data": [
        {"value": "30", "value_classification": "Fear", "timestamp": "1793318400", "time_until_update": "64361"},
        {"value": "51", "value_classification": "Neutral", "timestamp": "1793232000"},
        {"value": "48", "value_classification": "Neutral", "timestamp": "1793145600"},
        {"value": "oops", "value_classification": "?", "timestamp": "1793059200"},  # 坏数据跳过
        {"value": "120", "value_classification": "?", "timestamp": "1792972800"},  # 超出 0-100 跳过
    ],
    "metadata": {"error": None},
}

TRENDING = {
    "coins": [
        {"item": {"id": "hyperliquid", "coin_id": 50882, "name": "Hyperliquid", "symbol": "HYPE", "market_cap_rank": 10,
                  "score": 0, "data": {"price": 92.5321940477189, "price_change_percentage_24h": {"usd": 16.7182909902513}}}},
        {"item": {"id": "bitcoin", "name": "Bitcoin", "symbol": "BTC", "market_cap_rank": 1, "score": 1,
                  "data": {"price": 78518.4514135295, "price_change_percentage_24h": {"usd": -2.0830452440317933}}}},
        {"item": {"id": "old-style", "name": "Old, Style", "symbol": "old", "market_cap_rank": None, "score": 2,
                  "data": {"price": "$1,234.50", "price_change_percentage_24h": 3.21}}},  # 老格式：字符串价格
        {"item": {"id": "no-data", "name": "No Data", "symbol": "ND"}},  # 缺字段
        {"item": {}},  # 没有 id，跳过
    ],
    "nfts": [], "categories": [],
}


def _t(last, pct, qv, bid=None, ask=None):
    return {"last": last, "percentage": pct, "quoteVolume": qv, "bid": bid, "ask": ask}


TICKERS = {
    "PARTI/USDT": _t(0.05505, 43.28, 1_803_184),
    "BLESS/USDT": _t(0.012863, 30.52, 1_548_996),
    "HYPE/USDT": _t(92.5, 17.63, 69_066_627),
    "BTC3L/USDT": _t(1.2, 60.0, 9_000_000),  # 杠杆代币
    "TINY/USDT": _t(0.001, 80.0, 1_000),  # 成交额太小
    "ETH/BTC": _t(0.03, 50.0, 9_000_000),  # 不是 USDT 计价
    "BTC/USDT:USDT": _t(78600, 20.0, 9_000_000),  # 合约
    "DEAD/USDT": _t(1.0, 70.0, 9_000_000),  # 下架
    "FLAT/USDT": _t(1.0, 0.0, 9_000_000),
    "NOPCT/USDT": _t(1.0, None, 9_000_000),
    "INTCG/USDT": _t(30.1, -23.7, 1_916_308),
    "RSR/USDT": _t(0.004, -18.15, 1_635_251),
    "SPYX/USDT": _t(764.48, -0.24, 825_280, 764.61, 764.67),
    "SPYON/USDT": _t(763.97, -0.57, 250_500, 762.9, 764.59),
    "QQQX/USDT": _t(703.31, -0.44, 252_001, 703.25, 703.31),
    "NVDAON/USDT": _t(208.64, -0.77, 153_062, 208.52, 208.54),
}
MARKETS = {s: {"symbol": s, "spot": ":" not in s, "active": s != "DEAD/USDT"} for s in TICKERS}

# Yahoo SPY 收盘后（盘后最后一笔 19:59 纽约时间），裁剪自真实返回
YAHOO_SPY = {"chart": {"error": None, "result": [{
    "meta": {
        "symbol": "SPY", "regularMarketPrice": 769.36, "regularMarketTime": 1793304000,
        "currentTradingPeriod": {"pre": {"start": 1793347200, "end": 1793367000},
                                 "regular": {"start": 1793367000, "end": 1793390400},
                                 "post": {"start": 1793390400, "end": 1793404800}},
        "tradingPeriods": {"pre": [[{"start": 1793260800, "end": 1793280600}]],
                           "regular": [[{"start": 1793280600, "end": 1793304000}]],
                           "post": [[{"start": 1793304000, "end": 1793318400}]]},
    },
    "timestamp": [1793280600, 1793303940, 1793304000, 1793318340, 1793318389],
    "indicators": {"quote": [{"close": [768.78, 769.3, 769.36, 764.73, None]}]},
}]}}


def yahoo_intraday(at: int, bar_ts: int):
    """盘中：最后一根 K 线比 regularMarketTime 还新，但在盘中时段里 → 不算盘前盘后价。"""
    return {"chart": {"error": None, "result": [{
        "meta": {"regularMarketPrice": 770.0, "regularMarketTime": at,
                 "tradingPeriods": {"regular": [[{"start": 1793367000, "end": 1793390400}]]}},
        "timestamp": [bar_ts], "indicators": {"quote": [{"close": [770.1]}]}}]}}


class FakeHttp:
    """按 URL 关键字返回 fixture；值是异常就抛出来。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None, *, retries=None):
        self.calls.append((url, params))
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise HttpError(f"没有 fixture: {url}")


class FakeExchange:
    def __init__(self, tickers=TICKERS, markets=MARKETS, fail=None):
        self.tickers, self.markets, self.fail, self.calls = tickers, markets, list(fail or []), []

    def fetch_tickers(self, symbols=None, params=None):
        self.calls.append(params)
        if self.fail:
            raise self.fail.pop(0)
        return self.tickers


ROUTES = {"alternative.me": FNG, "coingecko": TRENDING, "/chart/SPY": YAHOO_SPY,
          "/chart/QQQ": HttpError("query1.finance.yahoo.com HTTP 429")}


def make_ctx(tmp_path, routes=ROUTES, now=NOW, exchange=None):
    return Ctx(data_dir=tmp_path, now=now, http=FakeHttp(routes), gate=Gate(exchange or FakeExchange(), sleep=lambda s: None))


def rows_of(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------- 恐惧贪婪 ----------------

def test_fear_greed_upsert_sorted_and_idempotent(tmp_path):
    ctx = make_ctx(tmp_path)
    msg = fear_greed.crawl(ctx, {})
    rows = rows_of(tmp_path / "fear_greed.csv")
    assert [r["date"] for r in rows] == ["2026-10-28", "2026-10-29", "2026-10-30"]
    assert rows[-1] == {"date": "2026-10-30", "value": "30", "classification": "Fear"}
    assert "新增 3" in msg and "2026-10-30 = 30" in msg

    before = (tmp_path / "fear_greed.csv").read_bytes()
    assert "新增 0，修正 0" in fear_greed.crawl(ctx, {})
    assert (tmp_path / "fear_greed.csv").read_bytes() == before  # 没变化就不重写

    # 第二天：今天的值被修正 + 多了一天；旧历史保留
    revised = {"data": [{"value": "25", "value_classification": "Extreme Fear", "timestamp": str(1793318400 + 86400)},
                        {"value": "33", "value_classification": "Fear", "timestamp": "1793318400"}], "metadata": {"error": None}}
    msg = fear_greed.crawl(make_ctx(tmp_path, routes={"alternative.me": revised}), {})
    rows = rows_of(tmp_path / "fear_greed.csv")
    assert [r["date"] for r in rows] == ["2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"]
    assert rows[2]["value"] == "33" and "新增 1，修正 1" in msg


def test_fear_greed_api_error_raises(tmp_path):
    ctx = make_ctx(tmp_path, routes={"alternative.me": {"data": [], "metadata": {"error": "rate limited"}}})
    with pytest.raises(ValueError, match="rate limited"):
        fear_greed.crawl(ctx, {})


# ---------------- CoinGecko 热搜 ----------------

def test_trending_rows_tolerate_missing_fields(tmp_path):
    msg = trending.crawl(make_ctx(tmp_path), {"top_n": 10})
    rows = rows_of(tmp_path / "trending.csv")
    assert [r["rank"] for r in rows] == ["1", "2", "3", "4"]
    assert rows[0] == {"time_utc": "2026-10-30 06:30", "rank": "1", "id": "hyperliquid", "symbol": "HYPE", "name": "Hyperliquid",
                       "market_cap_rank": "10", "price_usd": "92.53219405", "change_24h_pct": "16.72"}
    assert rows[2]["price_usd"] == "1234.5" and rows[2]["change_24h_pct"] == "3.21" and rows[2]["market_cap_rank"] == ""
    assert rows[2]["name"] == "Old, Style" and rows[2]["symbol"] == "OLD"  # 带逗号的名字也能正确存取
    assert rows[3]["price_usd"] == "" and rows[3]["change_24h_pct"] == ""
    assert "HYPE、BTC" in msg


def test_trending_top_n_and_append(tmp_path):
    trending.crawl(make_ctx(tmp_path), {"top_n": 2})
    trending.crawl(make_ctx(tmp_path, now=NOW + timedelta(hours=3)), {"top_n": 2})
    rows = rows_of(tmp_path / "trending.csv")
    assert len(rows) == 4 and rows[-1]["time_utc"] == "2026-10-30 09:30"
    assert (tmp_path / "trending.csv").read_text(encoding="utf-8").count("time_utc") == 1  # 表头只写一次


# ---------------- Gate 涨跌榜 ----------------

def test_gate_movers_filters_and_ranks(tmp_path):
    ex = FakeExchange()
    ctx = make_ctx(tmp_path, exchange=ex)
    msg = gate_movers.crawl(ctx, dict(gate_movers.DEFAULTS))
    assert ex.calls == [{"timezone": "all"}]  # 要滚动 24h 涨跌，不要 ccxt 默认的 UTC0 起算
    rows = rows_of(tmp_path / "gate_movers.csv")
    gainers = [r["symbol"] for r in rows if r["kind"] == "gainer"]
    losers = [r["symbol"] for r in rows if r["kind"] == "loser"]
    assert gainers[:3] == ["PARTI/USDT", "BLESS/USDT", "HYPE/USDT"]
    assert losers[:2] == ["INTCG/USDT", "RSR/USDT"]
    for excluded in ("BTC3L/USDT", "TINY/USDT", "ETH/BTC", "BTC/USDT:USDT", "DEAD/USDT", "FLAT/USDT", "NOPCT/USDT"):
        assert excluded not in gainers + losers
    assert rows[0] == {"time_utc": "2026-10-30 06:30", "kind": "gainer", "rank": "1", "symbol": "PARTI/USDT",
                       "last": "0.05505", "change_24h_pct": "43.28", "quote_volume": "1803184"}
    assert "领涨 PARTI/USDT +43.28%" in msg and "领跌 INTCG/USDT -23.70%" in msg


def test_gate_movers_top_n_and_leveraged_switch():
    gainers, losers, n = gate_movers.pick(TICKERS, MARKETS, {**gate_movers.DEFAULTS, "top_n": 1, "exclude_leveraged": False})
    assert [g[0] for g in gainers] == ["BTC3L/USDT"] and [x[0] for x in losers] == ["INTCG/USDT"]
    _, _, n_strict = gate_movers.pick(TICKERS, MARKETS, {**gate_movers.DEFAULTS, "min_quote_volume": 10_000_000})
    assert n_strict == 1  # 只剩 HYPE


def test_gate_tickers_fetched_once_and_retried_on_network_error():
    import ccxt

    ex = FakeExchange(fail=[ccxt.RequestTimeout("slow")])
    sleeps = []
    gate = Gate(ex, retries=1, sleep=sleeps.append)
    assert gate.tickers() is TICKERS and gate.tickers() is TICKERS
    assert len(ex.calls) == 2 and len(sleeps) == 1  # 失败一次重试一次，之后用缓存
    with pytest.raises(ValueError):
        Gate(FakeExchange(fail=[ValueError("bad symbol")]), sleep=lambda s: None).tickers()  # 非网络错误不重试


# ---------------- 美股代币 ----------------

def test_parse_chart_regular_and_post():
    q = stock_tokens.parse_chart(YAHOO_SPY)
    assert q == {"price": 769.36, "time": 1793304000, "ext_price": 764.73, "ext_time": 1793318340}


def test_parse_chart_intraday_has_no_ext():
    assert stock_tokens.parse_chart(yahoo_intraday(1793370000, 1793369940))["ext_price"] is None
    assert stock_tokens.parse_chart(yahoo_intraday(1793370000, 1793370060))["ext_price"] is None  # 比报价新但在盘中


def test_parse_chart_premarket_is_ext():
    pre = yahoo_intraday(1793304000, 1793350000)  # 昨天收盘价 + 今天盘前的 K 线
    q = stock_tokens.parse_chart(pre)
    assert q["price"] == 770.0 and q["ext_price"] == 770.1 and q["ext_time"] == 1793350000


def test_parse_chart_error():
    with pytest.raises(ValueError, match="delisted"):
        stock_tokens.parse_chart({"chart": {"result": None, "error": {"code": "Not Found", "description": "maybe delisted"}}})


def test_stock_tokens_rows_premium_and_partial_failures(tmp_path):
    ctx = make_ctx(tmp_path)
    msg = stock_tokens.crawl(ctx, {"tickers": ["SPY", "QQQ", "NVDA", "AAPL"], "suffixes": ["X", "ON"]})
    rows = rows_of(tmp_path / "stock_tokens.csv")
    assert [(r["ticker"], r["issuer"], r["symbol"]) for r in rows] == [
        ("SPY", "xstocks", "SPYX/USDT"), ("SPY", "ondo", "SPYON/USDT"), ("QQQ", "xstocks", "QQQX/USDT"),
        ("NVDA", "ondo", "NVDAON/USDT")]
    spyx = rows[0]
    assert spyx["market_state"] == "closed" and spyx["stock_price"] == "769.36" and spyx["stock_time_utc"] == "2026-10-29 20:00"
    assert spyx["stock_ext_price"] == "764.73" and spyx["stock_ext_time_utc"] == "2026-10-29 23:59"
    # 代币用盘口中间价 (764.61+764.67)/2，美股用更新的盘后价 764.73
    assert float(spyx["premium_pct"]) == pytest.approx((764.64 / 764.73 - 1) * 100, abs=1e-4)
    assert spyx["token_last"] == "764.48"
    assert spyx["token_bid"] == "764.61" and spyx["token_quote_volume"] == "825280"
    qqq = rows[2]
    assert qqq["token_last"] == "703.31" and qqq["stock_price"] == "" and qqq["premium_pct"] == ""  # Yahoo 挂了照样记代币价
    assert "AAPL" in msg and "Gate 没有代币" in msg and "美股价没拿到：QQQ" in msg and "美股休市" in msg


def test_token_price_prefers_sane_mid():
    assert stock_tokens.token_price(_t(208.73, 0, 1, 209.51, 209.54)) == pytest.approx(209.525)  # 成交价过时了
    assert stock_tokens.token_price(_t(100.0, 0, 1, 95.0, 105.0)) == 100.0  # 价差 10%：不信盘口
    assert stock_tokens.token_price(_t(100.0, 0, 1, 101.0, 99.0)) == 100.0  # 买一高于卖一：数据坏了
    assert stock_tokens.token_price(_t(100.0, 0, 1)) == 100.0  # 没盘口
    assert stock_tokens.token_price(_t(None, 0, 1)) is None


def test_stock_tokens_gives_up_on_yahoo_after_consecutive_failures(tmp_path):
    down = HttpError("query1.finance.yahoo.com HTTP 429")
    ctx = make_ctx(tmp_path, routes={"finance.yahoo.com": down})
    stock_tokens.crawl(ctx, {"tickers": ["SPY", "QQQ", "NVDA"], "suffixes": ["X", "ON"]})
    tried = {url.rsplit("/", 1)[-1] for url, _ in ctx.http.calls}
    assert tried == {"SPY", "QQQ"}  # 连续两个失败后 NVDA 就不再请求
    assert len(rows_of(tmp_path / "stock_tokens.csv")) == 4


def test_stock_tokens_nothing_listed_is_a_failure(tmp_path):
    with pytest.raises(ValueError, match="一个美股代币都没找到"):
        stock_tokens.crawl(make_ctx(tmp_path), {"tickers": ["ZZZZ"], "suffixes": ["X"]})


# ---------------- 美股时段 ----------------

MARKET_CASES = [
    ("2026-10-30 07:59", "closed"),  # 03:59 EDT
    ("2026-10-30 08:00", "pre"),  # 04:00
    ("2026-10-30 13:29", "pre"),
    ("2026-10-30 13:30", "regular"),  # 09:30
    ("2026-10-30 19:59", "regular"),
    ("2026-10-30 20:00", "post"),  # 16:00
    ("2026-10-30 23:59", "post"),
    ("2026-10-31 00:00", "closed"),  # UTC 已经周六，纽约还是周五 20:00
    ("2026-10-31 15:00", "closed"),  # 周六
    ("2026-11-02 14:29", "pre"),  # 夏令时结束后是 UTC-5
    ("2026-11-02 14:30", "regular"),
    ("2026-11-26 15:00", "closed"),  # 感恩节
    ("2026-11-27 17:59", "regular"),  # 提前收盘日 12:59 EST
    ("2026-11-27 18:00", "post"),  # 13:00 收盘
    ("2026-11-27 22:00", "closed"),  # 盘后到 17:00
]


@pytest.mark.parametrize("no_tzdata", [False, True])
def test_market_state(monkeypatch, no_tzdata):
    if no_tzdata:
        monkeypatch.setattr(nyse, "NY", None)  # Windows 没装 tzdata：走手算夏令时
    for utc, state in MARKET_CASES:
        at = datetime.strptime(utc, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        assert nyse.market_state(at) == state, (utc, no_tzdata)


def test_dst_fallback_matches_zoneinfo():
    if nyse.NY is None:
        pytest.skip("没有 tzdata")
    t = datetime(2026, 1, 1, tzinfo=UTC)
    while t < datetime(2029, 1, 1, tzinfo=UTC):
        offset = timedelta(hours=-4 if nyse._us_dst(t) else -5)
        assert t.astimezone(nyse.NY).utcoffset() == offset, t
        t += timedelta(hours=1, minutes=7)


# ---------------- CSV / HTTP 零件 ----------------

def test_append_csv_migrates_header_and_repairs_partial_line(tmp_path):
    p = tmp_path / "x.csv"
    append_csv(p, ["a", "b"], [{"a": 1, "b": 2}])
    with open(p, "a", encoding="utf-8") as f:
        f.write("9,9")  # 模拟上次写到一半被杀（没有换行）
    append_csv(p, ["a", "b", "c"], [{"a": 3, "b": 4, "c": 5}])  # 新增一列
    header, rows = read_csv(p)
    assert header == ["a", "b", "c"]
    assert rows == [{"a": "1", "b": "2", "c": ""}, {"a": "9", "b": "9", "c": ""}, {"a": "3", "b": "4", "c": "5"}]
    append_csv(p, ["a", "c"], [{"a": 6, "c": 7}])  # 少写一列：保留文件里的旧列
    assert read_csv(p)[0] == ["a", "b", "c"] and last_row(p) == {"a": "6", "b": "", "c": "7"}
    assert b"\r\n" not in p.read_bytes()


def test_last_row_header_only_and_missing(tmp_path):
    assert last_row(tmp_path / "nope.csv") is None
    p = tmp_path / "h.csv"
    p.write_text("time_utc,x\n", encoding="utf-8")
    assert last_row(p) is None


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = list(responses), {}, 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_http_retries_429_then_succeeds_and_sends_honest_ua():
    sleeps = []
    s = FakeSession([FakeResponse(429), FakeResponse(200, {"ok": 1})])
    http = Http(session=s, sleep=sleeps.append)
    assert http.get_json("https://api.example.com/x") == {"ok": 1}
    assert s.calls == 2 and len(sleeps) == 1
    assert s.headers["User-Agent"] == user_agent() and "kurumi-crawler" in user_agent()


def test_http_errors():
    http = Http(session=FakeSession([FakeResponse(404)]), sleep=lambda s: None)
    with pytest.raises(HttpError, match="api.example.com HTTP 404"):
        http.get_json("https://api.example.com/x")  # 4xx 不重试
    s = FakeSession([requests.ConnectionError("boom"), requests.Timeout("slow")])
    with pytest.raises(HttpError, match="连不上"):
        Http(session=s, sleep=lambda s: None).get_json("https://api.example.com/x")
    assert s.calls == 2
    with pytest.raises(HttpError, match="不是 JSON"):
        Http(session=FakeSession([FakeResponse(200)]), sleep=lambda s: None).get_json("https://api.example.com/x")


# ---------------- 入口 ----------------

def _main(tmp_path, *args, routes=ROUTES, exchange=None, now=NOW, config=None):
    cfg = tmp_path / "config.toml"
    cfg.write_text(config or "", encoding="utf-8")
    argv = ["--data-dir", str(tmp_path / "data"), "--config", str(cfg), *args]
    http = FakeHttp(routes)
    code = run.main(argv, http=http, gate=Gate(exchange or FakeExchange(), sleep=lambda s: None), now=now)
    return code, http


def test_main_all_sources_and_step_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code, _ = _main(tmp_path)
    assert code == 0
    for name in ("fear_greed", "trending", "gate_movers", "stock_tokens"):
        assert (tmp_path / "data" / f"{name}.csv").exists()
    out = capsys.readouterr().out
    assert "爬虫" in out and "成功 4" in out
    md = summary.read_text(encoding="utf-8")
    assert "| 数据源 | 结果 |" in md and "`gate_movers`" in md


def test_main_one_source_failing_does_not_stop_others(tmp_path, capsys):
    routes = {**ROUTES, "coingecko": HttpError("api.coingecko.com HTTP 429")}
    code, _ = _main(tmp_path, routes=routes)
    assert code == 0
    out = capsys.readouterr().out
    assert "❌ CoinGecko 热搜" in out and "成功 3 · 失败 1" in out
    assert not (tmp_path / "data" / "trending.csv").exists() and (tmp_path / "data" / "gate_movers.csv").exists()


def test_main_all_failing_exits_1(tmp_path):
    import ccxt

    routes = {"alternative.me": HttpError("x"), "coingecko": HttpError("y")}
    ex = FakeExchange(fail=[ccxt.ExchangeNotAvailable("down")] * 4)
    assert _main(tmp_path, routes=routes, exchange=ex)[0] == 1


def test_main_only_and_unknown(tmp_path):
    code, http = _main(tmp_path, "--only", "trending")
    assert code == 0 and [u for u, _ in http.calls] == [trending.URL]
    with pytest.raises(SystemExit):
        _main(tmp_path, "--only", "trending,nope")


def test_main_skips_fresh_snapshots(tmp_path, capsys):
    _main(tmp_path, "--only", "trending")
    code, http = _main(tmp_path, "--only", "trending", now=NOW + timedelta(minutes=5))
    assert code == 0 and http.calls == [] and "跳过" in capsys.readouterr().out
    _main(tmp_path, "--only", "trending", now=NOW + timedelta(minutes=25))
    assert len(rows_of(tmp_path / "data" / "trending.csv")) == 8


def test_main_reads_crawler_config(tmp_path, capsys):
    config = """
[crawler]
sources = ["trending", "gate_movers"]
min_interval_minutes = 0
bogus = 1

[crawler.trending]
top_n = 1

[crawler.gate_movers]
top_n = "lots"
min_quote_volume = 1500000
"""
    code, http = _main(tmp_path, config=config)
    assert code == 0 and [u for u, _ in http.calls] == [trending.URL]
    assert len(rows_of(tmp_path / "data" / "trending.csv")) == 1
    movers = rows_of(tmp_path / "data" / "gate_movers.csv")
    assert {r["symbol"] for r in movers} == {"PARTI/USDT", "BLESS/USDT", "HYPE/USDT", "INTCG/USDT", "RSR/USDT"}
    err = capsys.readouterr().err
    assert "bogus" in err and "top_n" in err  # 写错的参数只警告，用默认值


def test_main_survives_broken_config(tmp_path, capsys):
    code, _ = _main(tmp_path, "--only", "fear_greed", config="[crawler\nsources = ")
    assert code == 0 and "读不了" in capsys.readouterr().err
