"""爬虫公用零件：HTTP（超时 + 轻量重试）、Gate 公共行情、CSV 读写、时间和数字格式。"""
from __future__ import annotations

import csv
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import requests

TIMEOUT = 20  # 秒
TIME_FMT = "%Y-%m-%d %H:%M"  # 快照时间统一用 UTC，精确到分钟


class Skip(Exception):
    """这次不用爬（比如刚爬过），不算失败。"""


class HttpError(RuntimeError):
    pass


def user_agent() -> str:
    # 老实报上名号，不冒充浏览器。Yahoo 会拒掉 python-requests 这种默认 UA，自报家门的爬虫 UA 可以正常访问
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"kurumi-crawler/0.1 (+https://github.com/{repo})" if repo else "kurumi-crawler/0.1 (personal research)"


class Http:
    """requests 包一层：统一 UA 和超时；网络错误 / 429 / 5xx 歇几秒再试（默认重试 1 次），其它 4xx 直接报错。"""

    def __init__(self, timeout: float = TIMEOUT, retries: int = 1, pause: float = 3.0, *, session=None,
                 sleep: Callable[[float], None] = time.sleep):
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.pause = pause
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": user_agent(), "Accept": "application/json"})
        self._sleep = sleep

    def get_json(self, url: str, params: Mapping[str, Any] | None = None, *, retries: int | None = None) -> Any:
        host = urlsplit(url).netloc
        err: Exception = HttpError(f"{host} 没有响应")
        for i in range((self.retries if retries is None else retries) + 1):
            if i:
                self._sleep(self.pause * i)
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                err = HttpError(f"{host} 连不上（{type(e).__name__}）")
                continue
            if r.status_code == 429 or r.status_code >= 500:
                err = HttpError(f"{host} HTTP {r.status_code}")
                continue
            if r.status_code != 200:
                raise HttpError(f"{host} HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError:
                raise HttpError(f"{host} 返回的不是 JSON") from None
        raise err


class Gate:
    """Gate 现货公共行情（ccxt，不需要 API key）。一次运行只拉一次全市场 ticker，几个数据源共用。"""

    def __init__(self, exchange=None, *, timeout: float = TIMEOUT, retries: int = 1, pause: float = 3.0,
                 sleep: Callable[[float], None] = time.sleep):
        self._ex = exchange
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.pause = pause
        self._sleep = sleep
        self._tickers: dict | None = None

    @property
    def ex(self):
        if self._ex is None:
            import ccxt

            # 只加载现货市场：连合约、期权一起加载要多花十几秒，这里用不上
            self._ex = ccxt.gate({"enableRateLimit": True, "timeout": int(self.timeout * 1000),
                                  "options": {"fetchMarkets": {"types": ["spot"]}}})
        return self._ex

    def tickers(self) -> dict[str, dict]:
        if self._tickers is None:
            # timezone=all：change_percentage 才是滚动 24 小时涨跌幅。
            # ccxt 默认传 utc0，拿到的是“UTC 0 点以来”的涨跌，和网页上的 24h 涨跌对不上
            self._tickers = self._retry(lambda: self.ex.fetch_tickers(params={"timezone": "all"}))
        return self._tickers

    def markets(self) -> dict[str, dict]:
        self.tickers()  # fetch_tickers 会顺带加载市场列表
        return getattr(self.ex, "markets", None) or {}

    def _retry(self, fn):
        for i in range(self.retries + 1):
            try:
                return fn()
            except Exception as e:
                if i == self.retries or not _is_network_error(e):
                    raise
                self._sleep(self.pause * (i + 1))


def _is_network_error(e: Exception) -> bool:
    import ccxt

    return isinstance(e, ccxt.NetworkError)  # 超时、限频、交易所维护都算，值得再试一次


@dataclass
class Ctx:
    """一次运行的上下文。同一次运行写的所有快照行用同一个时间，方便按批次对齐。"""

    data_dir: Path
    now: datetime  # UTC
    http: Http
    gate: Gate

    @property
    def stamp(self) -> str:
        return fmt_time(self.now)


# ---------------- 时间 ----------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_time(t: datetime | int | float | None) -> str:
    """datetime 或 UTC 秒 → 'YYYY-MM-DD HH:MM'（UTC）。"""
    if t is None:
        return ""
    if not isinstance(t, datetime):
        t = datetime.fromtimestamp(float(t), timezone.utc)
    return t.astimezone(timezone.utc).strftime(TIME_FMT)


def parse_time(s: str | None) -> datetime | None:
    try:
        return datetime.strptime((s or "").strip(), TIME_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------- 数字 ----------------

def to_float(x) -> float | None:
    """宽松转数字："$1,234.5" 也认；None / NaN / 认不出来 → None。"""
    if isinstance(x, str):
        x = x.strip().replace("$", "").replace(",", "")
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def num(x, sig: int = 10) -> str:
    """写进 CSV 的数字：保留 sig 位有效数字（0.00000123 这种小币价也不丢精度），空值写空串。"""
    v = to_float(x)
    return "" if v is None else f"{v:.{sig}g}"


def pct(x) -> str:
    return num(x, 4)


def whole(x) -> str:
    v = to_float(x)
    return "" if v is None else str(round(v))


def wan(x: float) -> str:
    """500000 → '50 万'，给人看的金额。"""
    if x >= 1e8:
        return f"{x / 1e8:g} 亿"
    if x >= 1e4:
        return f"{x / 1e4:g} 万"
    return f"{x:g}"


# ---------------- CSV ----------------
# 统一 UTF-8、带表头、\n 换行（git diff 干净）。快照类只追加；表头变了按列名迁移旧数据，不会错位。

def read_header(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f), [])


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
        return list(r.fieldnames or []), rows


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Mapping]) -> None:
    """整体重写（先写临时文件再替换，写到一半被杀也不会留下半个文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(header), extrasaction="ignore", restval="", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def append_csv(path: Path, header: Sequence[str], rows: Iterable[Mapping]) -> int:
    """追加若干行，返回写了几行。文件不存在就先写表头。"""
    rows = list(rows)
    if not rows:
        return 0
    header = list(header)
    existing = read_header(path)
    if existing and existing != header:
        if set(header) <= set(existing):
            header = existing  # 文件里有代码已经不写的旧列：保留，新行留空
        else:
            header = existing + [h for h in header if h not in existing]  # 新增的列补在最后
            _, old = read_csv(path)
            write_csv(path, header, old)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            broken = f.read(1) != b"\n"  # 上次写到一半被杀：先补个换行，别把新行接在半行后面
    with open(path, "a", newline="", encoding="utf-8") as f:
        if not new and broken:
            f.write("\n")
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore", restval="", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def last_row(path: Path) -> dict | None:
    """最后一行数据（只读文件尾部，文件再大也快）。只有表头或文件不存在 → None。"""
    header = read_header(path)
    if not header:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 16384))
        tail = f.read().decode("utf-8", errors="replace")
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return None
    cells = next(csv.reader([lines[-1]]), [])
    if cells == header:
        return None
    return dict(zip(header, cells))
