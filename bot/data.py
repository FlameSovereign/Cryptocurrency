"""行情数据：交易所 K 线（ccxt 公共接口，无需 API key）+ 恐惧贪婪指数。

回测要的历史数据会缓存到 .cache/，重复回测不用反复下载。
"""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

import requests

from .candles import Candle, drop_unclosed, from_ccxt, merge, now_ms, timeframe_ms
from .strategies import FearGreed

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("KURUMI_CACHE_DIR", ROOT / ".cache"))
FNG_URL = "https://api.alternative.me/fng/"
PAGE = 1000

# Gate 的 K 线接口只允许查“最近 10000 根”，再往前会报 Candlestick too long ago。
# 1h 大约 416 天，4h 大约 4.5 年，1d 基本不受限。
POINTS_BACK = {"gate": 10_000}


def make_exchange(exchange_id: str, *, auth: bool = False, testnet: bool = False):
    """创建 ccxt 交易所对象。auth=True 时从环境变量读 API key（GitHub Secrets 注入）。

    testnet=True 读测试网专用的 EXCHANGE_TESTNET_API_KEY / EXCHANGE_TESTNET_API_SECRET（Gate 测试网的 key 是单独建的）。
    """
    import ccxt

    cls = getattr(ccxt, exchange_id, None)
    if cls is None:
        raise ValueError(f"ccxt 里没有交易所 {exchange_id!r}")
    cfg = {"enableRateLimit": True, "timeout": 20_000}
    if auth:
        prefix = "EXCHANGE_TESTNET_API" if testnet else "EXCHANGE_API"
        key = os.environ.get(f"{prefix}_KEY", "").strip()
        secret = os.environ.get(f"{prefix}_SECRET", "").strip()
        if not key or not secret:
            raise RuntimeError(f"{'测试网' if testnet else '实盘'}模式需要设置 {prefix}_KEY 和 {prefix}_SECRET")
        cfg.update(apiKey=key, secret=secret)
        password = os.environ.get("EXCHANGE_API_PASSWORD", "").strip()
        if password:
            cfg["password"] = password
    return cls(cfg)


def history_floor_ms(exchange_id: str, timeframe: str, at_ms: int) -> int:
    pts = POINTS_BACK.get(exchange_id)
    if not pts:
        return 0
    return at_ms - (pts - 10) * timeframe_ms(timeframe)


# ---------------- K 线缓存 ----------------

def _cache_path(exchange_id: str, symbol: str, timeframe: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return CACHE_DIR / "ohlcv" / exchange_id / f"{safe}-{timeframe}.csv"


def read_cache(exchange_id: str, symbol: str, timeframe: str) -> list[Candle]:
    p = _cache_path(exchange_id, symbol, timeframe)
    if not p.exists():
        return []
    with open(p, newline="") as f:
        rows = list(csv.reader(f))
    return from_ccxt(r for r in rows[1:] if r)


def write_cache(exchange_id: str, symbol: str, timeframe: str, candles: list[Candle]) -> None:
    p = _cache_path(exchange_id, symbol, timeframe)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        w.writerows(candles)
    os.replace(tmp, p)


def _download(ex, symbol: str, timeframe: str, since_ms: int, until_ms: int) -> list[Candle]:
    tf = timeframe_ms(timeframe)
    out: list[Candle] = []
    cursor = since_ms
    empty_pages = 0
    while cursor < until_ms:
        rows = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=PAGE)
        batch = [c for c in from_ccxt(rows) if cursor <= c.ts < until_ms]
        if not batch:
            # 这段时间还没上市（或者交易所这段没数据）就往后跳
            empty_pages += 1
            if empty_pages > 400:
                break
            cursor += PAGE * tf
            continue
        empty_pages = 0
        out.extend(batch)
        nxt = batch[-1].ts + tf
        if nxt <= cursor:
            break
        cursor = nxt
    return out


def fetch_history(
    ex,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int | None = None,
    *,
    use_cache: bool = True,
    offline: bool = False,
) -> list[Candle]:
    """取 [since_ms, until_ms) 的已收盘 K 线；有缓存先用缓存，只补缺的部分。

    ex 可以是 ccxt 交易所对象，也可以是交易所 id 字符串（offline=True 时不会联网）。
    """
    exchange_id = ex if isinstance(ex, str) else ex.id
    now = now_ms()
    until = min(until_ms or now, now)
    since = max(since_ms, history_floor_ms(exchange_id, timeframe, now))
    tf = timeframe_ms(timeframe)

    cached = read_cache(exchange_id, symbol, timeframe) if use_cache else []
    if cached and not offline and cached[-1].ts + tf < history_floor_ms(exchange_id, timeframe, now):
        # 缓存太久没更新，断档的那段交易所已经不给查了：旧缓存挪到一边，从交易所允许的最早处重新下
        # （不能硬接：回测按位置走 K 线，中间缺一段会被悄悄连起来）
        old = _cache_path(exchange_id, symbol, timeframe)
        os.replace(old, old.with_suffix(f".stale-{cached[-1].ts}.csv"))
        cached = []
    if offline:
        if not cached:
            raise FileNotFoundError(f"离线模式下没有 {exchange_id} {symbol} {timeframe} 的缓存")
        return [c for c in drop_unclosed(cached, timeframe, now) if since_ms <= c.ts < until]

    if isinstance(ex, str):
        ex = make_exchange(ex)
    meta_path = _cache_path(exchange_id, symbol, timeframe).with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if use_cache and meta_path.exists() else {}
    parts = [cached]
    if not cached:
        parts.append(_download(ex, symbol, timeframe, since, until))
    else:
        # 缓存开头之前还没查过（或者上次查过但确实没数据 = 还没上市），才往前补
        if since < cached[0].ts and meta.get("listed_from") != cached[0].ts:
            older = _download(ex, symbol, timeframe, since, cached[0].ts)
            parts.append(older)
            if not older:
                meta["listed_from"] = cached[0].ts
        if cached[-1].ts + tf < until:
            parts.append(_download(ex, symbol, timeframe, cached[-1].ts + tf, until))
    candles = drop_unclosed(merge(*parts), timeframe, now)
    if use_cache and candles:
        write_cache(exchange_id, symbol, timeframe, candles)
        if meta:
            meta_path.write_text(json.dumps(meta))
    return [c for c in candles if since_ms <= c.ts < until]


def fetch_recent(ex, symbol: str, timeframe: str, bars: int) -> list[Candle]:
    """实盘用：最近 bars 根已收盘 K 线（不走缓存）。"""
    tf = timeframe_ms(timeframe)
    now = now_ms()
    since = now - (bars + 2) * tf
    candles = fetch_history(ex, symbol, timeframe, since, now, use_cache=False)
    return candles[-bars:]


# ---------------- 恐惧贪婪指数 ----------------

def read_fear_greed_csv(path: Path) -> FearGreed:
    """爬虫存的 data/fear_greed.csv（date,value,classification）。"""
    from datetime import datetime, timezone

    data = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            day = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            data[int(day.timestamp() * 1000)] = int(row["value"])
    return FearGreed(data)


def fetch_fear_greed(limit: int = 0, *, use_cache: bool = False, offline: bool = False, max_age_s: int = 6 * 3600,
                     fallback_csv: Path | None = None) -> FearGreed:
    """alternative.me 恐惧贪婪指数。limit=0 表示全部历史（2018-02 起）。

    接口挂了而 fallback_csv（爬虫存的那份）存在时，用 CSV 顶上。
    """
    try:
        return _fetch_fear_greed(limit, use_cache=use_cache, offline=offline, max_age_s=max_age_s)
    except Exception:
        if fallback_csv is not None and Path(fallback_csv).exists():
            return read_fear_greed_csv(Path(fallback_csv))
        raise


def _fetch_fear_greed(limit: int, *, use_cache: bool, offline: bool, max_age_s: int) -> FearGreed:
    cache = CACHE_DIR / "fear_greed.json"
    if (use_cache or offline) and cache.exists():
        blob = json.loads(cache.read_text())
        if offline or time.time() - blob.get("fetched", 0) < max_age_s:
            return FearGreed({int(k): v for k, v in blob["data"].items()})
    if offline:
        raise FileNotFoundError("离线模式下没有恐惧贪婪指数缓存")
    r = requests.get(FNG_URL, params={"limit": limit, "format": "json"}, timeout=20)
    r.raise_for_status()
    rows = r.json()["data"]
    data = {int(d["timestamp"]) * 1000: int(d["value"]) for d in rows}
    if use_cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fetched": time.time(), "data": data}))
    return FearGreed(data)
