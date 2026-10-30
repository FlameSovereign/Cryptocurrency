"""下单执行：模拟盘（PaperBroker）和实盘（LiveBroker）。

两者接口相同，run.py 不关心是真是假：
    min_cost(symbol, price)                          交易所最小下单金额
    sync(wallet, symbol, price, prefix, known, since) 开跑前对账，返回提示信息
    execute(plan, symbol, price, slippage=, tag=)    下单并返回 Fill
"""
from __future__ import annotations

import hashlib
import time

from .candles import now_ms
from .engine import OrderPlan, simulate_fill
from .wallet import Fill, Wallet


class SkipOrder(Exception):
    """钱不够 / 低于最小下单额之类，这次不下单，但不算出错。"""


def market_min_cost(market: dict, price: float) -> float:
    lim = market.get("limits") or {}
    cmin = (lim.get("cost") or {}).get("min") or 0.0
    amin = (lim.get("amount") or {}).get("min") or 0.0
    return max(float(cmin), float(amin) * price, 1.0)


def tag_prefix(bot_name: str) -> str:
    """订单备注前缀（Gate 的 text 字段：t- 开头，总长 <= 28）。用来认领自己下的单。"""
    return "t-ks-" + hashlib.sha1(bot_name.encode()).hexdigest()[:6]


def new_tag(bot_name: str, at_ms: int | None = None) -> str:
    return f"{tag_prefix(bot_name)}-{_base36(at_ms or now_ms())}"


def _base36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = chars[r] + s
    return s or "0"


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def fill_from_order(order: dict, market: dict) -> Fill:
    """把交易所返回的订单换算成钱包认的 Fill（到手多少币、花了多少钱、扣了多少手续费）。

    Gate 的原始字段优先：filled_total = 成交额（计价币），filled_amount = 成交量（币）。
    ccxt 对 Gate 市价买单的 cost 用的是“下单金额”而不是“成交额”，所以不能直接信。
    手续费：买入一般扣币、卖出扣钱；开了 GT 抵扣时扣 GT（不影响这本账）。
    """
    info = order.get("info") or {}
    side = order.get("side") or info.get("side")
    base, quote = market["base"], market["quote"]

    filled_quote = _f(info.get("filled_total"))
    if filled_quote is None:
        filled_quote = _f(order.get("cost")) or 0.0
    avg = _f(info.get("avg_deal_price")) or _f(order.get("average"))
    filled_base = _f(info.get("filled_amount"))
    if not filled_base:
        if avg and filled_quote:
            filled_base = filled_quote / avg
        else:
            filled_base = _f(order.get("filled")) or 0.0
    if not avg and filled_base:
        avg = filled_quote / filled_base

    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    fee_base = fee_quote = 0.0
    for f in fees:
        if not f:
            continue
        cost = _f(f.get("cost")) or 0.0
        if f.get("currency") == base:
            fee_base += cost
        elif f.get("currency") == quote:
            fee_quote += cost

    if side == "buy":
        got_base, paid_quote = filled_base - fee_base, filled_quote + fee_quote
    elif side == "sell":
        got_base, paid_quote = filled_base + fee_base, filled_quote - fee_quote
    else:
        raise ValueError(f"订单方向未知: {side!r}")
    return Fill(
        side=side,
        price=avg or 0.0,
        base=max(got_base, 0.0),
        quote=max(paid_quote, 0.0),
        fee_quote=fee_quote + fee_base * (avg or 0.0),
        ts=order.get("timestamp") or now_ms(),
        order_id=str(order.get("id")) if order.get("id") is not None else None,
    )


class PaperBroker:
    """模拟盘：用实时价格 + 手续费 + 滑点假装成交。不需要 API key。"""

    mode = "paper"

    def __init__(self, exchange, *, fee: float):
        self.ex = exchange
        self.fee = fee

    def market(self, symbol: str) -> dict:
        if not self.ex.markets:
            self.ex.load_markets()
        return self.ex.market(symbol)

    def min_cost(self, symbol: str, price: float) -> float:
        return market_min_cost(self.market(symbol), price)

    def sync(self, wallet: Wallet, symbol: str, price: float, prefix: str, known: list, since_ms: int) -> list[str]:
        return []

    def execute(self, plan: OrderPlan, symbol: str, price: float, *, slippage: float, tag: str) -> Fill:
        fill = simulate_fill(plan, price, fee=self.fee, slippage=slippage, ts=now_ms())
        fill.order_id = tag
        return fill


class LiveBroker:
    """实盘：市价单。买入金额 = min(机器人现金, 账户可用余额, max_order)，卖出数量 = min(机器人持仓, 账户可用币)。"""

    mode = "live"

    def __init__(self, exchange, *, max_order: float):
        self.ex = exchange
        self.max_order = max_order
        self.ex.load_markets()

    def market(self, symbol: str) -> dict:
        return self.ex.market(symbol)

    def min_cost(self, symbol: str, price: float) -> float:
        return market_min_cost(self.market(symbol), price)

    def free(self, symbol: str) -> tuple[float, float]:
        m = self.market(symbol)
        free = self.ex.fetch_balance().get("free") or {}
        return float(free.get(m["base"]) or 0.0), float(free.get(m["quote"]) or 0.0)

    def sync(self, wallet: Wallet, symbol: str, price: float, prefix: str, known: list, since_ms: int) -> list[str]:
        """开跑前对账：
        1. 找回“单下了但账本没存上”（比如上次 push 失败）的订单，按订单备注认领；
        2. 账本里的币比账户里多（被手动卖了/转走了），以账户为准。
        """
        notes: list[str] = []
        m = self.market(symbol)
        try:
            orders = self.ex.fetch_closed_orders(symbol, since=since_ms, limit=100)
        except Exception as e:  # 找回失败不影响主流程
            notes.append(f"查历史订单失败，跳过找回：{e}")
            orders = []
        for o in sorted(orders, key=lambda o: o.get("timestamp") or 0):
            cid = o.get("clientOrderId") or (o.get("info") or {}).get("text") or ""
            oid = str(o.get("id"))
            if not cid.startswith(prefix) or oid in known:
                continue
            known.append(oid)
            fill = fill_from_order(o, m)
            if fill.base <= 0:
                continue
            wallet.apply(fill)
            notes.append(f"找回漏记的订单 {oid}：{fill.side} {fill.base:.8g} {m['base']} @ {fill.price:.8g}")

        base_free, _ = self.free(symbol)
        if wallet.qty > base_free * 1.001 + 1e-12:
            notes.append(f"账本记着 {wallet.qty:.8g} {m['base']}，账户里只有 {base_free:.8g}，以账户为准")
            wallet.qty = base_free
            if wallet.qty * price < wallet.dust_value():
                wallet.entry_price = wallet.high_water = wallet.entry_equity = None
        return notes

    def execute(self, plan: OrderPlan, symbol: str, price: float, *, slippage: float, tag: str) -> Fill:
        m = self.market(symbol)
        base_free, quote_free = self.free(symbol)
        min_cost = market_min_cost(m, price)
        if plan.side == "buy":
            cost = min(plan.cost, quote_free, self.max_order)
            if cost < min_cost:
                raise SkipOrder(f"可用 {m['quote']} 不够：想花 {plan.cost:.2f}，账户可用 {quote_free:.2f}，最小下单 {min_cost:.2f}")
            order = self.ex.create_market_buy_order_with_cost(symbol, cost, {"text": tag})
        else:
            amount = min(plan.amount, base_free)
            try:
                amount = float(self.ex.amount_to_precision(symbol, amount))
            except Exception:
                amount = 0.0
            if amount <= 0 or amount * price < min_cost:
                raise SkipOrder(f"可卖的 {m['base']} 太少：{amount:.8g}（最小下单 {min_cost:.2f} {m['quote']}）")
            order = self.ex.create_order(symbol, "market", "sell", amount, None, {"text": tag})
        order = self._settle(order, symbol)
        fill = fill_from_order(order, m)
        if fill.base <= 0:
            raise RuntimeError(f"订单 {order.get('id')} 没有成交：status={order.get('status')}")
        return fill

    def _settle(self, order: dict, symbol: str, tries: int = 10) -> dict:
        """市价单一般秒成交；万一返回时还没结束，就轮询几秒。"""
        for _ in range(tries):
            if order.get("status") in ("closed", "canceled", "cancelled", "expired", "rejected"):
                return order
            time.sleep(1)
            order = self.ex.fetch_order(order["id"], symbol)
        return order
