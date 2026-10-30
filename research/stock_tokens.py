"""股票代币研究台：Gate 上的 xStocks（<T>X/USDT，Backed 发行）和 Ondo（<T>ON/USDT）。

四个问题，全用数据回答（结论写在 research/notes/stock_tokens.md）：
1. 跟不跟得住正股：美股盘中 / 盘前盘后 / 工作日夜里 / 周末，代币相对正股偏离多少
2. 周末和隔夜：美股休市时代币自己在飘，开盘时谁对？扣掉手续费 + 滑点还有没有便宜可捡
3. 简单趋势规则：代币只有 ~16 个月数据，够不够下结论（统计功效）；换成正股几十年日线又如何
4. 实操：分时段成交量、盘口价差、300 USDT 市价单冲击

数据（都不进仓库）：
- Gate xStocks 1h/4h/1d：research.harness 的离线缓存
- --data 目录（默认 .cache/stocks）：Yahoo 正股 1h（含盘前盘后，近 2 年）/ 1d（全历史）/ 30m（近 60 天）、
  ES/NQ 期货 1h、Gate 30m（近 ~208 天，用来对齐美股盘中 :30 的时点）、Ondo 代币 1h、盘口快照。
  `fetch` 子命令下载，合计几 MB。

    python -m research.stock_tokens fetch --data DIR
    python -m research.stock_tokens report --data DIR                  # 打印全部 markdown 表
    python -m research.stock_tokens report --data DIR --only premium,weekend

口径：
- 偏离 = 代币价 / (正股价 × 分红基线) − 1。代币把税后分红再投资进价格，比值会慢慢往上爬，
  所以先除掉“前 10 个交易日 16:00 收盘比值的中位数”这条基线，剩下的才是真偏离。
- 成本和 research.harness 一致：手续费 0.2%/边、股票代币滑点 0.1%/边，一个来回约 0.6%。
- 不依赖其他人的策略文件；趋势规则是本文件里的私有小版本，不注册进策略表。
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bot import indicators as ind
from bot.backtest import Result, run_backtest
from bot.candles import Candle, from_ccxt
from bot.strategies import REGISTRY, Strategy
from bot.strategies import baselines as _baselines  # noqa: F401  只注册 hodl / monkey，不去加载别人的策略文件

from . import harness as hz

ET = ZoneInfo("America/New_York")
MIN = 60_000
H = 3_600_000
DAY = 86_400_000
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / ".cache" / "stocks"

PAIRS = {"SPYX": "SPY", "QQQX": "QQQ", "NVDAX": "NVDA", "TSLAX": "TSLA",
         "SPYON": "SPY", "QQQON": "QQQ", "NVDAON": "NVDA", "TSLAON": "TSLA"}
XSTOCKS = ["SPYX", "QQQX", "NVDAX", "TSLAX"]
ONDO = ["SPYON", "QQQON", "NVDAON", "TSLAON"]
TOKENS = XSTOCKS + ONDO
UNDERLYING = ["SPY", "QQQ", "NVDA", "TSLA"]
FUTURES = {"SPY": "ES=F", "QQQ": "NQ=F"}  # 夜里拿股指期货推算“正股此刻大概值多少”
SLIP = hz.SLIPPAGE["SPYX/USDT"]  # 股票代币滑点 0.1%/边
COST = 1 - (1 - SLIP) / (1 + SLIP) * (1 - hz.FEE) ** 2  # 买卖一个来回 ≈ 0.6%
BASE_DAYS = 10  # 分红基线：前 10 个交易日收盘比值的中位数
DIV_NET = 0.7  # 美股分红先扣 30% 预扣税再投资（Ondo 文档的口径，xStocks 实测也接近）
SESSIONS = {"rth": "盘中", "close": "16:00 收盘", "pre": "盘前", "post": "盘后",
            "night_fut": "工作日夜里*", "sunday_fut": "周日晚*"}
YAHOO_KINDS = {"1h": "1h-2y-pp", "1d": "1d-max", "30m": "30m-60d-pp"}


# ---------------- 时间 ----------------

def et(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ET)


def at_et(d: date, hh: int, mm: int = 0) -> int:
    """美东某天 hh:mm 对应的 UTC 毫秒（自动处理夏令时）。"""
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp() * 1000)


def _minute(ms: int) -> int:
    t = et(ms)
    return t.hour * 60 + t.minute


# ---------------- 数据 ----------------

@dataclass(frozen=True)
class YBar:
    """Yahoo 的一根 K 线。close 就是 end 时点的价格。"""

    ts: int
    end: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj: float  # 复权收盘价（日线才有意义）


def load_yahoo(data: Path, ticker: str, iv: str) -> tuple[list[YBar], list[tuple[int, float]]]:
    """读 Yahoo chart JSON：返回 (K 线, [(除息日毫秒, 每股分红)])。"""
    raw = json.loads((Path(data) / "yahoo" / f"{ticker}-{YAHOO_KINDS[iv]}.json").read_text())["chart"]["result"][0]
    q = raw["indicators"]["quote"][0]
    stamps = raw.get("timestamp") or []
    adj = ((raw["indicators"].get("adjclose") or [{}])[0].get("adjclose")) or [None] * len(stamps)
    step = {"1h": H, "30m": 30 * MIN, "1d": DAY}[iv]
    rows = []
    for i, t in enumerate(stamps):
        o, h, lo, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, lo, c) or t % 60:  # 不在整分钟上的是实时快照行
            continue
        rows.append((t * 1000, o, h, lo, c, q["volume"][i] or 0.0, adj[i] if adj[i] is not None else c))
    bars = []
    for k, (t, o, h, lo, c, v, a) in enumerate(rows):
        end = t + step
        if iv != "1d" and k + 1 < len(rows):
            end = min(end, rows[k + 1][0])  # 盘前 9:00 那根只到 9:30，盘中 15:30 那根只到 16:00
        if iv != "1d" and 0 < k < len(rows) - 1 and _spike(rows[k - 1][4], c, rows[k + 1][4]):
            continue  # Yahoo 盘后偶尔有离谱的单笔（比如 TSLA 459 → 319 → 459），前后都对得上就当坏点扔掉
        bars.append(YBar(t, end, float(o), float(h), float(lo), float(c), float(v), float(a)))
    events = raw.get("events") or {}
    divs = sorted((int(k) * 1000, float(v["amount"])) for k, v in events.get("dividends", {}).items())
    return bars, divs


def _spike(prev: float, cur: float, nxt: float, jump: float = 0.02) -> bool:
    """孤立尖刺：和前后两根都差 2% 以上、方向相反，而前后两根彼此很接近。财报跳空（跳了不回来）不算。"""
    a, b = cur / prev - 1, nxt / cur - 1
    return abs(a) > jump and abs(b) > jump and a * b < 0 and abs(nxt / prev - 1) < 0.5 * min(abs(a), abs(b))


def gate(data: Path, token: str, tf: str) -> list[Candle]:
    """Gate K 线：harness 缓存里有就用缓存，否则读 --data/gate/ 下的 CSV。"""
    sym = f"{token}/USDT"
    if sym in hz.UNIVERSE and tf in hz.TIMEFRAMES:
        return hz.candles(sym, tf)
    p = Path(data) / "gate" / f"{token}_USDT-{tf}.csv"
    if not p.exists():
        return []
    with open(p, newline="") as f:
        rows = list(csv.reader(f))[1:]
    return from_ccxt(r for r in rows if r)


def closes_at(candles, step_ms: int) -> dict[int, float]:
    """{收盘时点: 收盘价}：一根 K 线的收盘价就是它结束那一刻的最后成交价。"""
    return {c.ts + step_ms: c.close for c in candles}


class Calendar:
    """美股交易日历（从 SPY 日线反推），顺带记半天交易日的收盘时刻。"""

    def __init__(self, data: Path):
        daily, _ = load_yahoo(data, "SPY", "1d")
        self.days = sorted({et(b.ts).date() for b in daily})
        self.dayset = set(self.days)
        intraday, _ = load_yahoo(data, "SPY", "1h")
        last_rth: dict[date, int] = {}
        for b in intraday:
            m = _minute(b.ts)
            if 570 <= m < 960 and m % 60 == 30:  # 盘中的 1h K 线从 :30 开始；盘前盘后从整点开始
                d = et(b.ts).date()
                last_rth[d] = max(last_rth.get(d, 0), _minute(b.end))
        # 半天交易日（感恩节次日、平安夜、7/3）13:00 收盘：盘中 K 线在 12:00~16:00 之间就断了的算半天
        # （Yahoo 常常缺 12:30 那根）；更早就断的是数据缺口，没有 1h 数据的老日子都按 16:00
        self.close_min = {d: (780 if 720 <= last_rth.get(d, 960) < 960 else 960) for d in self.days}
        # 日线最后一天之后的工作日先当交易日（不认识节假日），这样最新的盘口快照也能分对时段
        d = self.days[-1]
        for _ in range(10):
            d += timedelta(days=1)
            if d.weekday() < 5:
                self.days.append(d)
                self.dayset.add(d)
                self.close_min[d] = 960

    def close_ms(self, d: date) -> int:
        m = self.close_min[d]
        return at_et(d, m // 60, m % 60)

    def session(self, ms: int) -> str:
        """rth / pre / post / night（工作日夜里）/ weekend（周末或长假）。时点按“截至 ms 那一刻”算。"""
        t = et(ms)
        d, m = t.date(), t.hour * 60 + t.minute
        if d in self.dayset:
            c = self.close_min[d]
            if 570 < m <= c:
                return "rth"
            if 240 < m <= 570:
                return "pre"
            if c < m <= 1200:
                return "post"
        # 休市：找“上一个已经收完盘后的交易日”和“下一个还没开盘前的交易日”，看它们隔了几天
        if d in self.dayset and m > 1200:
            prv = d
        else:
            i = bisect.bisect_left(self.days, d)
            prv = self.days[i - 1] if i > 0 else None
        if d in self.dayset and m <= 240:
            nxt = d
        else:
            i = bisect.bisect_right(self.days, d)
            nxt = self.days[i] if i < len(self.days) else None
        if prv is None or nxt is None:
            return "weekend"
        return "night" if (nxt - prv).days == 1 else "weekend"

    def pairs(self):
        """相邻两个交易日 (d0, d1, kind)：kind = night（工作日隔夜）/ weekend（周末或假日休市）。"""
        for d0, d1 in zip(self.days, self.days[1:]):
            yield d0, d1, ("night" if (d1 - d0).days == 1 else "weekend")


# ---------------- 小工具 ----------------

def pct(x: float | None, nd: int = 2, sign: bool = True) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:+.{nd}%}" if sign else f"{x:.{nd}%}"


def quantile(xs, q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def ols(xs, ys) -> tuple[float, float, float, float]:
    """一元回归 y = a + b·x：返回 (b, b 的 t 值, R², 相关系数)。"""
    n = len(xs)
    if n < 3:
        return float("nan"), float("nan"), float("nan"), float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return float("nan"), float("nan"), float("nan"), float("nan")
    b = sxy / sxx
    r = sxy / math.sqrt(sxx * syy)
    resid = syy - b * sxy
    se = math.sqrt(max(resid, 0.0) / (n - 2) / sxx)
    return b, (b / se if se > 0 else float("inf")), r * r, r


def mean_t(xs) -> tuple[float, float]:
    """均值和它的 t 值（均值 / 标准误）。"""
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else float("nan")), float("nan")
    m = sum(xs) / n
    sd = statistics.stdev(xs)
    return m, (m / (sd / math.sqrt(n)) if sd > 0 else float("nan"))


def md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# ---------------- 1. 跟踪：溢价 / 折价 ----------------

@dataclass(frozen=True)
class Point:
    ms: int
    session: str
    token: float
    fair: float  # 正股价（夜里是期货推算值）
    base: float  # 分红基线：代币 / 正股 的“正常比值”

    @property
    def dev(self) -> float:
        return self.token / (self.fair * self.base) - 1


class Pair:
    """一个代币和它的正股：把两边价格对齐到同一时点。"""

    def __init__(self, data: Path, token: str, cal: Calendar):
        self.token, self.und, self.cal = token, PAIRS[token], cal
        self.g1 = closes_at(gate(data, token, "1h"), H)
        self.g30 = closes_at(gate(data, token, "30m"), 30 * MIN)
        daily, self.divs = load_yahoo(data, self.und, "1d")
        self.daily = {et(b.ts).date(): b for b in daily}
        self.y1h, _ = load_yahoo(data, self.und, "1h")
        try:
            self.y30, _ = load_yahoo(data, self.und, "30m")
        except FileNotFoundError:
            self.y30 = []
        self.u_at = {b.end: b.close for b in self.y1h}
        self.u_at.update({b.end: b.close for b in self.y30})
        # 每个交易日 16:00（半天日 13:00）的原始比值 = 代币收盘前最后成交价 / 正股官方收盘价
        self.raw_close: dict[date, float] = {}
        for d in cal.days:
            g, y = self.g1.get(cal.close_ms(d)), self.daily.get(d)
            if g and y:
                self.raw_close[d] = g / y.close
        days = sorted(self.raw_close)
        self.base: dict[date, float] = {}
        for d in cal.days:
            j = bisect.bisect_left(days, d)
            prev = [self.raw_close[x] for x in days[max(0, j - BASE_DAYS):j]]
            if len(prev) >= 3:
                self.base[d] = statistics.median(prev)
        fut = FUTURES.get(self.und)
        self.f_at: dict[int, float] = {}
        if fut:
            try:
                fb, _ = load_yahoo(data, fut, "1h")
                self.f_at = {b.end: b.close for b in fb}
            except FileNotFoundError:
                pass

    def token_at(self, ms: int) -> float | None:
        return self.g30.get(ms) or self.g1.get(ms)

    def base_for(self, ms: int) -> float | None:
        """时点 ms 用哪个交易日的基线：当天是交易日就用当天，否则用下一个交易日（基线只含之前的收盘）。"""
        d = et(ms).date()
        i = bisect.bisect_left(self.cal.days, d)
        if i < len(self.cal.days):
            return self.base.get(self.cal.days[i])
        return None

    def points(self) -> list[Point]:
        out: list[Point] = []
        seen = set()
        for b in sorted(self.y1h + self.y30, key=lambda x: x.end):
            e = b.end
            if e in seen:
                continue
            seen.add(e)
            s = self.cal.session(e)
            if s not in ("rth", "pre", "post"):
                continue
            tok, base = self.token_at(e), self.base_for(e)
            if not tok or not base:
                continue
            d = et(e).date()
            if s == "rth" and e == self.cal.close_ms(d):
                s = "close"
            out.append(Point(e, s, tok, b.close, base))
        out += self._futures_points()
        return out

    def _roll_nights(self) -> set[date]:
        """期货换月那几天：期货 / 正股 的基差一天跳超过 0.25% 就当换月，那晚不用期货推算。"""
        basis = {}
        for d in self.cal.days:
            f, y = self.f_at.get(self.cal.close_ms(d)), self.daily.get(d)
            if f and y:
                basis[d] = f / y.close
        days = sorted(basis)
        return {d0 for d0, d1 in zip(days, days[1:]) if abs(basis[d1] / basis[d0] - 1) > 0.0025}

    def _futures_points(self) -> list[Point]:
        """美股休市时，用股指期货把正股最后一个价格“推”到当前时点。只有 SPY / QQQ 有对应期货。"""
        if not self.f_at:
            return []
        out = []
        rolls = self._roll_nights()
        for d0, d1, kind in self.cal.pairs():
            if d0 in rolls or self.cal.close_min[d0] != 960:
                continue
            if kind == "night":
                ref, start, label = at_et(d0, 20), at_et(d0, 21), "night_fut"
            else:
                sunday = _sunday(d1)
                if sunday <= d0:
                    continue
                ref, start, label = at_et(d0, 17), at_et(sunday, 19), "sunday_fut"
            u_ref, f_ref = self.u_at.get(ref), self.f_at.get(ref)
            if not u_ref or not f_ref:
                continue
            base = self.base.get(d1)
            if not base:
                continue
            e = start
            while e <= at_et(d1, 4):
                f, tok = self.f_at.get(e), self.g1.get(e)
                if f and tok:
                    out.append(Point(e, label, tok, u_ref * f / f_ref, base))
                e += H
        return out


def premium_tables(data: Path, cal: Calendar, tokens=TOKENS) -> tuple[str, str]:
    """(紧凑表：中位绝对偏离 / p95 绝对偏离，明细表)。"""
    order = list(SESSIONS)
    compact, detail = [], []
    for tok in tokens:
        pts = Pair(data, tok, cal).points()
        by: dict[str, list[float]] = {}
        for p in pts:
            by.setdefault(p.session, []).append(p.dev)
        cells = []
        for s in order:
            xs = by.get(s, [])
            if len(xs) < 20:
                cells.append("-")
                continue
            ab = [abs(x) for x in xs]
            cells.append(f"{statistics.median(ab):.2%} / {quantile(ab, 0.95):.2%}")
            detail.append([tok, SESSIONS[s], len(xs), pct(statistics.median(xs)), pct(quantile(xs, 0.05)), pct(quantile(xs, 0.95)),
                           pct(sum(ab) / len(ab), sign=False), pct(sum(a > 0.005 for a in ab) / len(ab), 0, sign=False)])
        compact.append([tok] + cells)
    t1 = md_table(["代币"] + [SESSIONS[s] for s in order], compact)
    t2 = md_table(["代币", "时段", "样本", "中位偏离", "p5", "p95", "平均绝对偏离", "超过 0.5% 的占比"], detail)
    return t1, t2


def dividend_table(data: Path, cal: Calendar, tokens=TOKENS) -> str:
    """原始比值（不除基线）随时间往上爬多少，对照“税后分红再投资”的理论值。"""
    rows = []
    for tok in tokens:
        p = Pair(data, tok, cal)
        days = sorted(p.raw_close)
        if len(days) < 60:
            continue
        first = statistics.median(p.raw_close[d] for d in days[:20]) - 1
        last = statistics.median(p.raw_close[d] for d in days[-20:]) - 1
        lo, hi = days[10], days[-10]  # 头尾两个窗口的中点之间发生的除息
        ddays = sorted(p.daily)
        theo, n = 1.0, 0
        for ts, amt in p.divs:
            ex = et(ts).date()
            if not lo < ex <= hi:
                continue
            j = bisect.bisect_left(ddays, ex)
            if j == 0:
                continue
            theo *= 1 + DIV_NET * amt / p.daily[ddays[j - 1]].close
            n += 1
        rows.append([tok, f"{days[0]} → {days[-1]}", pct(first), pct(last), pct(last - first), n, pct(theo - 1)])
    return md_table(["代币", "区间", "头 20 天比值−1", "末 20 天比值−1", "累计上爬", "期间除息次数", "税后分红再投资理论值"], rows)


# ---------------- 2. 周末 / 隔夜 ----------------

@dataclass(frozen=True)
class Closure:
    """一次美股休市（d0 收盘 → d1 收盘）里各个时点的价格。"""

    d0: date
    d1: date
    kind: str  # night / weekend
    tok_c0: float  # d0 16:00 代币
    und_c0: float  # d0 正股收盘
    tok_pre: float  # d1 04:00 代币（盘前开始那一刻）
    und_open: float  # d1 正股开盘
    tok_c1: float  # d1 16:00 代币
    und_c1: float  # d1 正股收盘
    base: float  # d1 的分红基线
    tok_sun: float | None = None  # 周日 18:00（期货开盘）代币

    @property
    def tok_dark(self) -> float:  # 休市期间代币自己飘了多少
        return self.tok_pre / self.tok_c0 - 1

    @property
    def gap(self) -> float:  # 正股跳空：前收 → 今开
        return self.und_open / self.und_c0 - 1

    @property
    def open_err(self) -> float:  # 代币休市最后报价 vs 正股真实开盘（除掉基线）
        return self.tok_pre / (self.und_open * self.base) - 1

    @property
    def tok_after(self) -> float:  # 代币从 04:00 到当天收盘
        return self.tok_c1 / self.tok_pre - 1


def closures(data: Path, token: str, cal: Calendar) -> list[Closure]:
    p = Pair(data, token, cal)
    out = []
    for d0, d1, kind in cal.pairs():
        y0, y1 = p.daily.get(d0), p.daily.get(d1)
        tok_c0, tok_pre, tok_c1 = p.g1.get(cal.close_ms(d0)), p.g1.get(at_et(d1, 4)), p.g1.get(cal.close_ms(d1))
        base = p.base.get(d1)
        if not (y0 and y1 and tok_c0 and tok_pre and tok_c1 and base):
            continue
        sun = None
        if kind == "weekend":
            sunday = _sunday(d1)
            if sunday > d0:
                sun = p.g1.get(at_et(sunday, 18))
        out.append(Closure(d0, d1, kind, tok_c0, y0.close, tok_pre, y1.open, tok_c1, y1.close, base, sun))
    return out


def rule_stats(rets: list[float]) -> list[str]:
    """一组单笔收益（已扣成本）：笔数、平均、t 值、胜率。"""
    if not rets:
        return ["0", "-", "-", "-"]
    m, t = mean_t(rets)
    return [str(len(rets)), pct(m), f"{t:.2f}" if not math.isnan(t) else "-", pct(sum(r > 0 for r in rets) / len(rets), 0, sign=False)]


def net(p0: float, p1: float) -> float:
    """p0 买、p1 卖，扣手续费和滑点后的收益（和 bot.engine.simulate_fill 同口径）。"""
    return p1 / p0 * (1 - SLIP) / (1 + SLIP) * (1 - hz.FEE) ** 2 - 1


def weekend_tables(data: Path, cal: Calendar, tokens=TOKENS, theta: float = 0.01) -> tuple[str, str]:
    """(休市期间代币能不能预告开盘，反转规则扣成本后的结果)。"""
    t1, t2 = [], []
    for tok in tokens:
        cs = closures(data, tok, cal)
        for kind, label in (("weekend", "周末/假日"), ("night", "工作日隔夜")):
            xs = [c for c in cs if c.kind == kind]
            if len(xs) < 20:
                continue
            dark, gap = [c.tok_dark for c in xs], [c.gap for c in xs]
            b, bt, r2, _ = ols(dark, gap)
            err = [c.open_err for c in xs]
            naive = [c.gap for c in xs]  # 拿前收当开盘预测的误差
            t1.append([tok, label, len(xs), pct(statistics.pstdev(dark), sign=False), pct(statistics.pstdev(gap), sign=False),
                       f"{b:.2f}（t={bt:.1f}）", f"{r2:.2f}", pct(statistics.median(abs(e) for e in err), sign=False),
                       pct(statistics.median(abs(g) for g in naive), sign=False), pct(statistics.mean(err))])
            after = [c.tok_after for c in xs]
            _, rt, _, rr = ols(dark, after)
            fade = [net(c.tok_pre, c.tok_c1) for c in xs if c.tok_dark <= -theta]
            chase = [net(c.tok_pre, c.tok_c1) for c in xs if c.tok_dark >= theta]
            # 拿着的人：周末涨多了先卖、收盘买回（只做多也能做的“反向”）
            sell_pop = [c.tok_pre / c.tok_c1 * (1 - SLIP) / (1 + SLIP) * (1 - hz.FEE) ** 2 - 1 for c in xs if c.tok_dark >= theta]
            t2.append([tok, label, f"{rr:+.2f}（t={rt:.1f}）"] + rule_stats(fade) + rule_stats(chase)[:3] + rule_stats(sell_pop)[:3])
    h1 = ["代币", "休市类型", "次数", "代币休市波动", "正股跳空波动", "跳空对代币休市涨跌的斜率", "R²",
          "代币报价误差（中位绝对）", "拿前收当预测的误差", "代币平均偏差"]
    h2 = ["代币", "休市类型", "休市涨跌 vs 之后涨跌 相关",
          f"抄底笔数（跌≥{theta:.0%}）", "抄底净收益", "t", "胜率",
          f"追涨笔数（涨≥{theta:.0%}）", "追涨净收益", "t", f"拿着的人涨≥{theta:.0%}先卖、收盘买回", "相对拿着", "t"]
    return md_table(h1, t1), md_table(h2, t2)


def weekend_hold_table(data: Path, cal: Calendar, tokens=XSTOCKS) -> str:
    """周末拿着 vs 周五收盘卖、周一盘前买回：躲周末风险要付多少钱。"""
    rows = []
    for tok in tokens:
        cs = [c for c in closures(data, tok, cal) if c.kind == "weekend"]
        hold = [c.tok_pre / c.tok_c0 - 1 for c in cs]
        und = [c.gap for c in cs]
        m, t = mean_t(hold)
        rows.append([tok, len(cs), pct(m), f"{t:.2f}", pct(statistics.mean(und)), pct(statistics.pstdev(hold), sign=False),
                     pct(-COST), pct(-COST * 52, 0)])
    return md_table(["代币", "周末数", "代币周末平均涨跌", "t", "正股周末跳空平均", "周末波动",
                     "躲一次周末的成本", "每周都躲的年化成本"], rows)


def _sunday(d1: date) -> date:
    """d1 之前最近的周日（周一开盘 → 前一天；周二开盘（周一放假）→ 前两天）。"""
    return d1 - timedelta(days=(d1.weekday() + 1) % 7)


def weekend_btc_table(data: Path, cal: Calendar, tokens=TOKENS, theta: float = 0.005) -> str:
    """纯周末（周五 16:00 → 周日 18:00 期货开盘前）代币在跟谁走、之后反不反转、抄底扣完成本剩多少。"""
    btc = closes_at(hz.candles("BTC/USDT", "1h"), H)
    rows, basket = [], {}
    for tok in tokens:
        xs = []
        for c in closures(data, tok, cal):
            if c.kind != "weekend" or not c.tok_sun:
                continue
            b0, b1 = btc.get(cal.close_ms(c.d0)), btc.get(at_et(_sunday(c.d1), 18))
            if b0 and b1:
                xs.append((c, b1 / b0 - 1))
                if tok in XSTOCKS:
                    basket.setdefault(c.d1, []).append((c, b1 / b0 - 1))
        rows.append(_weekend_row(tok, [(c.tok_c0, c.tok_sun, c.tok_c1, c.gap, b) for c, b in xs], theta))
    # 4 个 xStocks 等权篮子：同一个周末四个一起动，单个代币的 t 值会夸大证据
    legs = []
    for d1, xs in sorted(basket.items()):
        if len(xs) == len(XSTOCKS):
            legs.append(xs)
    rows.append(_weekend_row("xStocks 等权篮子", legs, theta, basket=True))
    return md_table(["代币", "周末数", "纯周末涨跌 vs BTC 同期 相关", "正股周一跳空 vs BTC 周末 相关",
                     "纯周末涨跌 vs 周日18:00→周一收盘 相关", f"抄底笔数（纯周末跌≥{theta:.1%}）", "抄底净收益", "t",
                     "不挑周末一律周日买周一卖"], rows)


def _weekend_row(name: str, xs, theta: float, basket: bool = False) -> list:
    if basket:
        pts = []
        for legs in xs:
            pure = statistics.mean(c.tok_sun / c.tok_c0 - 1 for c, _ in legs)
            after = statistics.mean(c.tok_c1 / c.tok_sun - 1 for c, _ in legs)
            r = statistics.mean(net(c.tok_sun, c.tok_c1) for c, _ in legs)
            gap = statistics.mean(c.gap for c, _ in legs)
            pts.append((pure, after, r, gap, legs[0][1]))
    else:
        pts = [(sun / c0 - 1, c1 / sun - 1, net(sun, c1), gap, b) for c0, sun, c1, gap, b in xs]
    if len(pts) < 10:
        return [name, len(pts)] + ["-"] * 7
    pure, after, rets, gaps, btc = (list(v) for v in zip(*pts))
    r_btc = ols(btc, pure)[3]
    r_gap = ols(btc, gaps)[3]
    _, t_rev, _, r_rev = ols(pure, after)
    dip = [r for p, r in zip(pure, rets) if p <= -theta]
    return [name, len(pts), f"{r_btc:+.2f}", f"{r_gap:+.2f}", f"{r_rev:+.2f}（t={t_rev:.1f}）"] + rule_stats(dip)[:3] + [pct(statistics.mean(rets))]


def sunday_table(data: Path, cal: Calendar, tokens=("SPYX", "QQQX", "SPYON", "QQQON"), theta: float = 0.003) -> str:
    """周日 18:00 期货开盘后，代币相对“期货推算的正股价”偏离多少、几个小时收敛、能不能吃到。"""
    rows = []
    for tok in tokens:
        pts = [x for x in Pair(data, tok, cal).points() if x.session == "sunday_fut"]
        by_week: dict[date, dict[tuple[int, int], Point]] = {}
        for x in pts:
            t = et(x.ms)
            wk = t.date() if t.weekday() == 6 else t.date() - timedelta(days=(t.weekday() + 1) % 7)
            by_week.setdefault(wk, {})[(t.weekday(), t.hour)] = x
        med = {}
        for key, label in (((6, 19), "周日19点"), ((6, 21), "周日21点"), ((0, 0), "周一0点"), ((0, 4), "周一4点")):
            xs = [abs(w[key].dev) for w in by_week.values() if key in w]
            med[label] = statistics.median(xs) if xs else float("nan")
        first, last = [], []
        for w in by_week.values():
            a = w.get((6, 19))
            later = [k for k in w if k[0] != 6]  # 周一（或长周末的周二）凌晨，取最晚那个点
            b = w[max(later)] if later else None
            if a and b:
                first.append(a)
                last.append(b)
        slope = ols([a.dev for a in first], [b.dev for b in last])[0]
        cheap = [(a, b) for a, b in zip(first, last) if a.dev <= -theta]
        rel = [(b.token / a.token) / (b.fair / a.fair) - 1 for a, b in cheap]
        tok_net = [net(a.token, b.token) for a, b in cheap]
        rows.append([tok, len(first)] + [pct(v, sign=False) for v in med.values()] + [f"{slope:.2f}", len(cheap),
                     pct(statistics.mean(rel)) if rel else "-", pct(statistics.mean(tok_net)) if tok_net else "-"])
    return md_table(["代币", "周末数", "周日19点 中位绝对偏离", "周日21点", "周一0点", "周一4点", "偏离持续系数（19点→次日4点）",
                     f"19点折价≥{theta:.1%} 次数", "之后跑赢期货", "买代币扣成本净收益"], rows)


# ---------------- 4. 流动性 ----------------

def volume_table(data: Path, cal: Calendar, tokens=TOKENS) -> str:
    """Gate 1h K 线成交额按时段汇总：钱都在什么时候成交。"""
    rows = []
    names = {"rth": "盘中", "pre": "盘前", "post": "盘后", "night": "工作日夜里", "weekend": "周末/假日"}
    for tok in tokens:
        cs = gate(data, tok, "1h")
        last = [c for c in cs if c.ts >= cs[-1].ts - 90 * DAY]  # 最近 90 天
        by: dict[str, list[float]] = {}
        for c in last:
            by.setdefault(cal.session(c.ts + 30 * MIN), []).append(c.volume * c.close)
        total = sum(sum(v) for v in by.values()) or 1.0
        daily = total / 90
        rows.append([tok, f"{daily:,.0f}"] + [f"{sum(by.get(s, [])) / total:.0%} / {statistics.median(by[s]) if by.get(s) else 0:,.0f}" for s in names])
    return md_table(["代币", "近 90 天日均成交额 USDT"] + [f"{v}（占比 / 每小时中位 USDT）" for v in names.values()], rows)


def book_table(data: Path, cal: Calendar, budget: float = hz.BUDGET) -> tuple[str, str]:
    """盘口快照：价差、±1% 深度、300 USDT 市价单冲击。(本文 8 个代币的每次快照, 最新一次的全部股票代币)。"""
    snaps = sorted(Path(data).glob("snap_*.json"))
    if not snaps:
        return "（没有盘口快照，先跑 fetch）", ""
    rows, allrows, stamp = [], [], []
    for k, path in enumerate(snaps):
        snap = json.loads(path.read_text())
        when = et(snap["ts"])
        stamp.append(f"{when:%Y-%m-%d %H:%M} 美东（{SESSION_NAME[cal.session(snap['ts'])]}）")
        for sym, ob in sorted(snap["books"].items()):
            bids, asks = ob["bids"], ob["asks"]
            if not bids or not asks:
                continue
            bb, ba = bids[0][0], asks[0][0]
            mid = (bb + ba) / 2
            depth_b = sum(p * a for p, a, *_ in bids if p >= mid * 0.99)
            depth_a = sum(p * a for p, a, *_ in asks if p <= mid * 1.01)
            imp_buy, imp_sell = _impact(asks, budget, mid), _impact(bids, budget, mid)
            qv = (snap["tickers"].get(sym) or {}).get("quoteVolume") or 0.0
            row = [sym, f"{qv:,.0f}", f"{(ba - bb) / mid * 1e4:.1f}", f"{depth_b:,.0f} / {depth_a:,.0f}",
                   f"{imp_buy * 1e4:.1f} / {imp_sell * 1e4:.1f}"]
            base = sym.split("/")[0]
            if base in TOKENS:
                rows.append([f"{when:%m-%d %H:%M}"] + row)
            if k == len(snaps) - 1:  # 全部交易对只列最新一次
                allrows.append((qv, [f"{when:%m-%d %H:%M}"] + row))
    h = ["快照(美东)", "交易对", "24h 成交额 USDT", "买一卖一价差 bp", "±1% 深度 USDT（买/卖）", f"{budget:.0f} USDT 市价单冲击 bp（买/卖）"]
    allrows.sort(key=lambda x: -x[0])
    return "快照时间：" + "；".join(stamp) + "\n\n" + md_table(h, rows), md_table(h, [r for _, r in allrows])


SESSION_NAME = {"rth": "盘中", "pre": "盘前", "post": "盘后", "night": "工作日夜里", "weekend": "周末/假日"}


def _impact(levels, notional: float, mid: float) -> float:
    """吃掉 notional USDT 的平均成交价相对中间价的距离（正数 = 吃亏）。"""
    left, qty, cost = notional, 0.0, 0.0
    for p, a, *_ in levels:
        take = min(left, p * a)
        qty += take / p
        cost += take
        left -= take
        if left <= 1e-9:
            break
    if left > 1e-9 or qty <= 0:
        return float("nan")
    return abs(cost / qty / mid - 1)


# ---------------- 3. 简单策略 ----------------

def _sma_cross(candles, position, params, ctx):
    px = ind.closes(candles)
    fast, slow = int(params["fast"]), int(params["slow"])
    f, s = sum(px[-fast:]) / fast, sum(px[-slow:]) / slow
    return (1.0, "快线在慢线上方") if f > s else (0.0, "快线在慢线下方")


def _donchian(candles, position, params, ctx):
    n_in, n_out = int(params["entry"]), int(params["exit"])
    prev = candles[:-1]  # 不含最新一根，避免偷看
    hi = max(c.high for c in prev[-n_in:])
    lo = min(c.low for c in prev[-n_out:])
    close = candles[-1].close
    if close > hi:
        return 1.0, "收盘创新高"
    if close < lo:
        return 0.0, "收盘创新低"
    return (1.0, "通道内，拿着") if position > 0.5 else (0.0, "通道内，空着")


def _tsmom(candles, position, params, ctx):
    n = int(params["lookback"])
    r = candles[-1].close / candles[-1 - n].close - 1
    return (1.0, f"{n} 根涨了 {r:+.1%}") if r > 0 else (0.0, f"{n} 根跌了 {r:+.1%}")


def _private(name: str, fn, defaults: dict, warmup) -> Strategy:
    return Strategy(f"_stk_{name}", name, "趋势（私有对照）", "stock_tokens 研究用，不注册", defaults, warmup, fn)


FAMILIES = {
    "sma_cross": (_private("sma_cross", _sma_cross, {"fast": 20, "slow": 100}, lambda p: int(p["slow"]) + 1),
                  [{"fast": f, "slow": s} for f, s in ((10, 50), (20, 50), (20, 100), (50, 100), (20, 200), (50, 200))]),
    "donchian": (_private("donchian", _donchian, {"entry": 55, "exit": 20}, lambda p: max(int(p["entry"]), int(p["exit"])) + 1),
                 [{"entry": a, "exit": b} for a, b in ((20, 10), (55, 20), (100, 50), (200, 100))]),
    "tsmom": (_private("tsmom", _tsmom, {"lookback": 50}, lambda p: int(p["lookback"]) + 1),
              [{"lookback": n} for n in (20, 50, 100, 200)]),
}


def _label(family: str, params: dict) -> str:
    return f"{family}(" + ",".join(str(v) for v in params.values()) + ")"


def bt(strategy: Strategy, candles: list[Candle], tf: str, sym: str, params=None, *, start_ms=None, end_ms=None,
       slippage: float | None = None) -> Result:
    """和 research.harness.backtest 同一套成本（预算 300、手续费 0.2%、最小单 3 USDT）。"""
    c = candles if end_ms is None else [x for x in candles if x.ts < end_ms]
    slip = hz.SLIPPAGE.get(sym, hz.DEFAULT_SLIPPAGE) if slippage is None else slippage
    return run_backtest(strategy, c, timeframe=tf, symbol=sym, params=params, budget=hz.BUDGET, fee=hz.FEE,
                        slippage=slip, min_cost=hz.MIN_COST, start_ms=start_ms)


def monkey_band(candles, tf, sym, *, seeds: int = 30, start_ms=None, slippage=None) -> tuple[float, float]:
    """同 harness.monkey_band，但直接用 baselines 里的猴子（不去 load_all 别人的策略文件）。"""
    m = REGISTRY["monkey"]
    rets = sorted(bt(m, candles, tf, sym, {"seed": s, "hold": 24}, start_ms=start_ms, slippage=slippage).total_return
                  for s in range(seeds))
    return statistics.median(rets), rets[int(0.9 * (len(rets) - 1))]


def grid_search(series: dict[str, list[Candle]], tf: str, *, split: float = 0.6, slippage=None):
    """每个规则族在样本内（前 60%）挑参数：按 4 个标的“超额收益（策略 − 囤币）中位数”最高者，再看样本外。

    返回 (选中参数 {族: params}, 样本内 rows, 样本外 rows, {标的: 切分时点})。
    注意：日线只有 ~480 根，200 根的预热会吃掉样本内的大半，所以 1d 的“挑参数”本身就很虚。
    """
    cuts = {sym: c[int(len(c) * split)].ts for sym, c in series.items()}
    chosen, ins, outs = {}, [], []
    for fam, (strat, grid) in FAMILIES.items():
        best, best_score = None, -math.inf
        for p in grid:
            rows = []
            for sym, c in series.items():
                try:
                    rows.append(hz.Row(_label(fam, p), sym, tf, "in", bt(strat, c, tf, sym, p, end_ms=cuts[sym], slippage=slippage)))
                except ValueError:
                    continue
            if not rows:
                continue
            ins += rows
            score = statistics.median(r.r.total_return - r.r.hodl_return for r in rows)
            if score > best_score:
                best, best_score = p, score
        if best is None:
            continue
        chosen[fam] = best
        for sym, c in series.items():
            try:
                outs.append(hz.Row(_label(fam, best), sym, tf, "out", bt(strat, c, tf, sym, best, start_ms=cuts[sym], slippage=slippage)))
            except ValueError:
                pass
    return chosen, ins, outs, cuts


def token_series(data: Path, tf: str) -> dict[str, list[Candle]]:
    return {f"{t}/USDT": gate(data, t, tf) for t in XSTOCKS}


def underlying_series(data: Path) -> dict[str, list[Candle]]:
    """正股全历史日线，按复权价（分红再投资）换算 OHLC，和代币的总回报口径一致。"""
    out = {}
    for t in UNDERLYING:
        bars, _ = load_yahoo(data, t, "1d")
        cs = []
        for b in bars:
            k = b.adj / b.close if b.close else 1.0
            cs.append(Candle(b.ts, b.open * k, b.high * k, b.low * k, b.adj, b.volume))
        out[t] = cs
    return out


def strategy_report(data: Path, *, monkeys: bool = True) -> str:
    parts = []
    hodl = REGISTRY["hodl"]
    for tf in hz.TIMEFRAMES:
        series = token_series(data, tf)
        chosen, ins, outs, cuts = grid_search(series, tf)
        full = [hz.Row("hodl", s, tf, "full", bt(hodl, c, tf, s)) for s, c in series.items()]
        for fam, p in chosen.items():
            strat = FAMILIES[fam][0]
            full += [hz.Row(_label(fam, p), s, tf, "full", bt(strat, c, tf, s, p)) for s, c in series.items()]
        parts.append(f"### Gate xStocks {tf}\n\n样本内选中参数：" + "，".join(f"{k}={v}" for k, v in chosen.items()))
        if monkeys:
            band = []
            for s, c in series.items():
                med_f, p90_f = monkey_band(c, tf, s)
                med_o, p90_o = monkey_band(c, tf, s, start_ms=cuts[s])
                band.append([s, tf, pct(med_f, 1), pct(p90_f, 1), pct(med_o, 1), pct(p90_o, 1)])
            parts.append("猴子（30 只，hold=24 根）：\n\n" + md_table(["标的", "周期", "全样本中位", "全样本 p90", "样本外中位", "样本外 p90"], band))
        parts.append("样本外（后 40%，参数只用前 60% 挑）：\n\n" + hz.table(outs) + "\n\n" + hz.scoreboard(outs))
        parts.append("全样本（含囤币）：\n\n" + hz.table(full) + "\n\n" + hz.scoreboard(full))
        parts.append("样本内全部网格（挑参数用）：\n\n" + hz.scoreboard(ins))
    return "\n\n".join(parts)


def underlying_report(data: Path) -> str:
    """同样的规则和成本，换成正股几十年的日线：这才有统计意义。"""
    series = underlying_series(data)
    chosen, ins, outs, cuts = grid_search(series, "1d", slippage=SLIP)
    hodl = REGISTRY["hodl"]
    oos_h = [hz.Row("hodl", s, "1d", "out", bt(hodl, c, "1d", s, start_ms=cuts[s], slippage=SLIP)) for s, c in series.items()]
    span = "，".join(f"{s} {et(c[0].ts):%Y}→{et(cuts[s]):%Y-%m}→{et(c[-1].ts):%Y-%m}" for s, c in series.items())
    return (f"区间（开始→样本外起点→结束）：{span}\n\n样本内选中参数：" + "，".join(f"{k}={v}" for k, v in chosen.items())
            + "\n\n样本外：\n\n" + hz.table(outs + oos_h) + "\n\n" + hz.scoreboard(outs) + "\n\n样本内网格：\n\n" + hz.scoreboard(ins))


# ---------------- 统计功效 ----------------

def power_table(data: Path) -> str:
    """16 个月能看出什么：年化均值的 95% 置信区间、夏普的标准误、能确认的最小夏普。

    夏普标准误 ≈ sqrt((1 + SR²/2) / 年数)（Lo 2002）。确认一个夏普 0.5 的优势（5% 显著、80% 功效）
    大约要 (2.8 / 0.5)² ≈ 31 年数据。
    """
    rows = []
    for t in XSTOCKS:
        cs = gate(data, t, "1d")
        rows.append(_power_row(f"{t}/USDT（Gate 日线，24/7）", cs, 365))
    for t, cs in underlying_series(data).items():
        rows.append(_power_row(f"{t}（正股全历史）", cs, 252))
        recent = [c for c in cs if c.ts >= cs[-1].ts - int(1.33 * 365.25 * DAY)]
        rows.append(_power_row(f"{t}（正股最近 16 个月）", recent, 252))
    return md_table(["序列", "年数", "年化收益（对数）", "年化波动", "夏普", "夏普标准误", "年化收益 95% 区间",
                     "能确认的最小夏普（5% 显著 + 80% 功效）"], rows)


def _power_row(name: str, cs: list[Candle], per_year: int) -> list[str]:
    lr = [math.log(b.close / a.close) for a, b in zip(cs, cs[1:]) if a.close > 0 and b.close > 0]
    years = len(lr) / per_year
    mu, sd = statistics.mean(lr) * per_year, statistics.stdev(lr) * math.sqrt(per_year)
    sr = mu / sd if sd > 0 else 0.0
    se_sr = math.sqrt((1 + sr * sr / 2) / years)
    half = 1.96 * sd / math.sqrt(years)
    mde = (1.96 + 0.84) / math.sqrt(years)  # 最小可检测夏普（Lo 2002 近似，真实夏普不大时）
    return [name, f"{years:.1f}", pct(mu, 1), pct(sd, 0, sign=False), f"{sr:.2f}", f"{se_sr:.2f}",
            f"{pct(mu - half, 0)} ~ {pct(mu + half, 0)}", f"{mde:.2f}"]


# ---------------- 下载 ----------------

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def fetch(data: Path) -> None:
    """下载 Yahoo 正股/期货、Gate 30m 和 Ondo 1h、盘口快照到 data 目录（已存在的文件跳过，快照每次新存一份）。"""
    import requests

    from bot.data import make_exchange

    data = Path(data)
    (data / "yahoo").mkdir(parents=True, exist_ok=True)
    (data / "gate").mkdir(parents=True, exist_ok=True)
    jobs = [(t, iv) for t in UNDERLYING for iv in ("1h", "1d", "30m")] + [(f, "1h") for f in FUTURES.values()]
    for ticker, iv in jobs:
        p = data / "yahoo" / f"{ticker}-{YAHOO_KINDS[iv]}.json"
        if p.exists():
            continue
        params = {"interval": iv, "includePrePost": "true" if iv != "1d" else "false", "events": "div,splits"}
        if iv == "1d":
            params.update(period1=0, period2=int(time.time()))
        else:
            params["range"] = "2y" if iv == "1h" else "60d"
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", params=params,
                         headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        p.write_text(r.text)
        print("yahoo", ticker, iv, len(r.text), "字节")
        time.sleep(1)

    ex = make_exchange(hz.EXCHANGE)
    ex.load_markets()
    for tok, tf in [(t, "30m") for t in TOKENS] + [(t, "1h") for t in ONDO]:
        p = data / "gate" / f"{tok}_USDT-{tf}.csv"
        if p.exists():
            continue
        step = {"30m": 30 * MIN, "1h": H}[tf]
        now = int(time.time() * 1000)
        cur, rows = now - 9_980 * step, {}  # Gate 只给最近 1 万根
        while cur < now:
            batch = [b for b in ex.fetch_ohlcv(f"{tok}/USDT", tf, since=cur, limit=1000) if b[0] >= cur]
            if not batch:
                cur += 1000 * step
                continue
            rows.update({b[0]: b for b in batch})
            if batch[-1][0] + step <= cur:
                break
            cur = batch[-1][0] + step
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "open", "high", "low", "close", "volume"])
            w.writerows(rows[k][:6] for k in sorted(rows) if k + step <= now)
        print("gate", tok, tf, len(rows), "根")

    syms = sorted(s for s, m in ex.markets.items() if m.get("spot") and s.endswith("/USDT") and _is_stock_token(m["base"]))
    now = int(time.time() * 1000)
    tick = ex.fetch_tickers(syms)
    books = {}
    for s in syms:
        ob = ex.fetch_order_book(s, limit=100)
        books[s] = {"bids": ob["bids"], "asks": ob["asks"], "ts": ob.get("timestamp")}
    snap = {"ts": now, "tickers": {s: {k: tick[s].get(k) for k in ("last", "bid", "ask", "baseVolume", "quoteVolume")} for s in tick},
            "books": books}
    p = data / f"snap_{datetime.fromtimestamp(now / 1000, timezone.utc):%Y%m%dT%H%MZ}.json"
    p.write_text(json.dumps(snap))
    print("盘口快照", p, len(books), "个交易对")


def _is_stock_token(base: str) -> bool:
    for suffix in ("ON", "X"):
        if base.endswith(suffix) and base[: -len(suffix)] in KNOWN_TICKERS:
            return True
    return False


KNOWN_TICKERS = {"SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "COIN", "MSTR", "HOOD", "CRCL",
                 "GLD", "AMD", "NFLX", "PLTR", "SLV", "VTI", "AVGO", "BABA", "MCD", "UNH"}


# ---------------- 命令行 ----------------

SECTIONS = ("premium", "dividend", "weekend", "volume", "book", "power", "strategies", "underlying")


def report(data: Path, only=None) -> str:
    if not (Path(data) / "yahoo" / f"SPY-{YAHOO_KINDS['1d']}.json").exists():
        raise SystemExit(f"{data} 里没有数据，先跑：python -m research.stock_tokens fetch --data {data}")
    cal = Calendar(data)
    want = set(only or SECTIONS)
    out = []
    if "premium" in want:
        t1, t2 = premium_tables(data, cal)
        out += ["## 1. 跟踪偏离（中位绝对偏离 / p95 绝对偏离）", t1,
                "*夜里/周日晚：正股不交易，用 ES/NQ 期货把正股最后价格推到当前时点（只有 SPY/QQQ 能这么算；"
                "期货换月那几晚剔除）。盘中用 Gate 30m 对齐，只有最近 ~208 天。", "### 明细", t2]
    if "dividend" in want:
        out += ["## 1b. 代币/正股 原始比值的上爬（分红再投资）", dividend_table(data, cal)]
    if "weekend" in want:
        a, b = weekend_tables(data, cal)
        out += ["## 2. 休市期间代币报价能不能预告开盘", a, "## 2b. 休市涨跌之后反不反转（04:00 买、当天 16:00 卖，已扣 0.6% 来回成本）", b,
                "## 2c. 周末拿着还是躲开", weekend_hold_table(data, cal),
                "## 2d. 纯周末（期货开盘前）代币跟着 BTC 飘？之后反转能吃吗（周日 18:00 买、周一收盘卖）", weekend_btc_table(data, cal),
                "## 2e. 周日晚期货开盘后的收敛（SPY/QQQ 代币 vs 期货推算价）", sunday_table(data, cal)]
    if "volume" in want:
        out += ["## 4. 成交额分时段（最近 90 天）", volume_table(data, cal)]
    if "book" in want:
        a, b = book_table(data, cal)
        out += ["## 4b. 盘口快照（股票代币）", a, "### 全部股票代币（按 24h 成交额排序）", b]
    if "power" in want:
        out += ["## 3a. 统计功效", power_table(data)]
    if "strategies" in want:
        out += ["## 3b. 趋势规则 on Gate xStocks", strategy_report(data)]
    if "underlying" in want:
        out += ["## 3c. 同样规则 on 正股几十年日线（成本按代币算：0.2% + 0.1%/边）", underlying_report(data)]
    return "\n\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="股票代币研究")
    ap.add_argument("cmd", choices=("fetch", "report"))
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="额外数据目录（Yahoo / Gate 30m / Ondo / 盘口快照）")
    ap.add_argument("--only", help="逗号分隔：" + ",".join(SECTIONS))
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        fetch(Path(a.data))
        return 0
    print(report(Path(a.data), a.only.split(",") if a.only else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
