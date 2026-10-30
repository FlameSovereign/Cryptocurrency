"""抄底派（均值回归）：跌多了总会弹一下——直到某一次不弹。

- rsi_revert：RSI 跌进超卖区就抄底梭哈，涨进超买区就卖，夹在中间维持现状。教科书是 14 / 30 / 70，
  默认用回测挑出的 14 / 20 / 60（只抄真恐慌、弹一下就走）。把 period 调到 2~4、low 调到 10 左右
  就是 Connors 的短线 RSI(2) 玩法，配上 trend（只在收盘价高于 SMA(trend) 时才抄底，原版用 200）。
- bollinger_revert：收盘跌破布林下轨就抄底，摸到上轨（或回到中轨）就卖。教科书是 20 根、回中轨卖，
  默认用回测挑出的 40 根、摸上轨才卖，外加“反弹空间不到 1% 不抄”的手续费门槛。

共同的毛病：抄底就是接下落的刀子。单边熊市里“越跌越超卖”，买在超卖之后继续阴跌几个月都
等不到卖出信号，满仓硬扛最大回撤（日线回测里 70%~85% 很常见）。止损帮不上什么忙——止损线
正好卖在下一次要抄的底上，来回割肉；真想限亏，用 risk.kill_switch 或把 size 调小。
1h 这种短周期每次反弹就那么一点，Gate 来回一趟约 0.5% 的成本基本把利润吃光。
"""
from __future__ import annotations

from .. import indicators as ind
from . import register

HELD = 0.5  # 仓位过半才算“在车上”，卖剩的零头不算
MAX_WARMUP = 990  # Gate 一页 1000 根，留点余量


def _check_warmup(name: str, n: int) -> int:
    if n > MAX_WARMUP:
        raise ValueError(f"{name} 需要 {n} 根 K 线预热，超过上限 {MAX_WARMUP}（Gate 一页 1000 根），把周期调小点")
    return n


def _trend(params, name: str) -> int:
    trend = int(params["trend"])
    if trend < 0:
        raise ValueError(f"{name} 的 trend 必须 >= 0（0 = 不过滤），收到 {trend}")
    return trend


def _trend_gate(px: list[float], trend: int) -> tuple[bool, float | None]:
    """趋势过滤：trend=0 不过滤；否则收盘价要站在 SMA(trend) 上方才允许抄底。返回 (放不放行, 均线)。"""
    if trend <= 0:
        return True, None
    ma = sum(px[-trend:]) / trend
    return px[-1] > ma, ma


# ---------------- RSI 超卖抄底 ----------------

def _rsi_params(params) -> tuple[int, float, float, int]:
    n, low, high = int(params["period"]), float(params["low"]), float(params["high"])
    if n < 1:
        raise ValueError(f"rsi_revert 的 period 必须 >= 1，收到 {n}")
    if not 0 < low < high < 100:
        raise ValueError(f"rsi_revert 要求 0 < low < high < 100，收到 low={low:g}, high={high:g}")
    return n, low, high, _trend(params, "rsi_revert")


def _rsi_warmup(params) -> int:
    n, _, _, trend = _rsi_params(params)
    # Wilder RSI 有记忆。实测 BTC/ETH/DOGE：窗口 3 倍周期时和全历史算的 RSI 最多差 5 个点，
    # 6 倍周期 + 5 根时差不到 0.3，对 30/70 这种门槛可以忽略
    return _check_warmup("rsi_revert", max(6 * n + 5, trend + 1))


@register(
    "rsi_revert",
    title="RSI 超卖抄底",
    school="抄底派",
    summary="RSI 跌破 low（超卖）就抄底梭哈，涨过 high（超买）就清仓，中间维持现状。"
    "period 调到 2~4 是 Connors 短线玩法；trend > 0 时只在收盘高于 SMA(trend) 时抄底，熊市少接飞刀。",
    # 教科书是 14 / 30 / 70；小网格（只看样本内）挑出 low=20、high=60：只抄真正的恐慌盘，弹到 60 就走，少挨手续费
    defaults={"period": 14, "low": 20, "high": 60, "trend": 0},
    warmup=_rsi_warmup,
    timeframes=("4h",),
)
def rsi_revert(candles, position, params, ctx):
    n, low, high, trend = _rsi_params(params)
    px = ind.closes(candles)
    r = ind.rsi(px, n)[-1]
    tag = f"RSI({n}) {r:.1f}"
    if position >= HELD:
        if r > high:
            return 0.0, f"{tag} 涨过超买线 {high:g}，反弹到手，跑！"
        return 1.0, f"{tag}，还没到超买线 {high:g}，拿住等反弹"
    if r < low:
        ok, ma = _trend_gate(px, trend)
        if not ok:
            return 0.0, f"{tag} 超卖，但收盘在 {trend} 均线 {ma:.6g} 下方，熊市不接飞刀"
        return 1.0, f"{tag} 跌破超卖线 {low:g}，抄底，梭！"
    return 0.0, f"{tag}，没跌到超卖线 {low:g}，空仓等跌"


# ---------------- 布林带抄底 ----------------

_BB_EXIT = {"mid": "中轨", "upper": "上轨"}


def _bb_params(params) -> tuple[int, float, str, int, float]:
    n, k, edge = int(params["period"]), float(params["k"]), float(params["min_edge"])
    exit_ = str(params["exit"]).strip().lower()
    if n < 2 or k <= 0:
        raise ValueError(f"bollinger_revert 要求 period >= 2、k > 0，收到 period={n}, k={k:g}")
    if exit_ not in _BB_EXIT:
        raise ValueError(f"bollinger_revert 的 exit 只能是 'mid'（回中轨就卖）或 'upper'（摸上轨才卖），收到 {params['exit']!r}")
    if not 0 <= edge < 1:
        raise ValueError(f"bollinger_revert 的 min_edge 是小数（0.01 = 1%），收到 {edge:g}")
    return n, k, exit_, _trend(params, "bollinger_revert"), edge


def _bb_warmup(params) -> int:
    n, _, _, trend, _ = _bb_params(params)
    # 均线和标准差只看最近 period 根，没有记忆，不用额外预热
    return _check_warmup("bollinger_revert", max(n, trend) + 1)


@register(
    "bollinger_revert",
    title="布林带抄底",
    school="抄底派",
    summary="收盘跌破布林下轨（period 根均线 − k 倍标准差）就抄底梭哈，默认等摸到上轨才卖（exit='mid' 则回中轨就卖）。"
    "离卖出线的反弹空间不到 min_edge 不抄（赚的不够交手续费）；trend > 0 时只在收盘高于 SMA(trend) 时抄底。",
    # 教科书是 20 / 2 / 回中轨卖，在 Gate 的手续费下来回太勤、亏得最惨；
    # 小网格（只看样本内）挑出 40 根、摸上轨才卖、反弹空间不到 1% 不抄
    defaults={"period": 40, "k": 2.0, "exit": "upper", "trend": 0, "min_edge": 0.01},
    warmup=_bb_warmup,
    timeframes=("1d",),
)
def bollinger_revert(candles, position, params, ctx):
    n, k, exit_, trend, edge = _bb_params(params)
    px = ind.closes(candles)
    # 布林带按惯例包含刚收盘的这根：收盘时已知、下一根开盘才成交，不算偷看未来
    mid, up, lo = (s[-1] for s in ind.bollinger(px[-n:], n, k))
    close = px[-1]
    if close <= 0:
        return 0.0, "价格数据异常，空仓"
    line = mid if exit_ == "mid" else up
    if position >= HELD:
        if close > line:
            return 0.0, f"收盘 {close:.6g} 回到布林{_BB_EXIT[exit_]} {line:.6g} 上方，反弹到手，跑！"
        return 1.0, f"收盘 {close:.6g}，还没回到布林{_BB_EXIT[exit_]} {line:.6g}，拿住等反弹"
    if close < lo:
        room = line / close - 1
        if room < edge:
            # 来回一趟手续费 + 滑点约 0.5%，弹回卖出线也赚不回来就别折腾
            return 0.0, f"收盘 {close:.6g} 跌破布林下轨，但离{_BB_EXIT[exit_]}只有 {room:.2%}，不够 {edge:.2%}，不抄"
        ok, ma = _trend_gate(px, trend)
        if not ok:
            return 0.0, f"收盘 {close:.6g} 跌破布林下轨，但在 {trend} 均线 {ma:.6g} 下方，熊市不接飞刀"
        return 1.0, f"收盘 {close:.6g} 跌破布林下轨 {lo:.6g}，离{_BB_EXIT[exit_]}还有 {room:.1%}，抄底，梭！"
    return 0.0, f"收盘 {close:.6g}，在布林下轨 {lo:.6g} 上方，空仓等跌"
