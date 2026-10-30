"""下单执行：模拟盘（PaperBroker）和实盘（LiveBroker）。

两者接口相同，run.py 不关心是真是假：
    min_cost(symbol, price)                          交易所最小下单金额
    sync(wallet, symbol, price, prefix, known, since) 开跑前对账，返回提示信息
    execute(plan, symbol, price, slippage=, tag=)    下单并返回 Fill

实盘记账的底线（GitHub Actions 随时可能超时、断网、存档推不上去）：
- 每笔单都带备注 text = tag（t-ks-<机器人哈希>-<时间>），账本只记“已认领”的订单号；
- 下单请求没拿到回复 = 不知道下没下成：先按备注找，找不到就报错，绝不盲目重下；
- 只把“已经结束”的订单记进账本，记了就不再改；没结束的留给下次对账；
- 每次开跑先 sync：把交易所里带自己备注、账本却没记的已结束订单补上；历史订单都查不了就不交易。
"""
from __future__ import annotations

import hashlib
import math
import time

from .candles import now_ms
from .engine import OrderPlan, simulate_fill
from .wallet import Fill, Wallet

FINAL_STATUSES = ("closed", "canceled", "cancelled", "expired", "rejected")


class SkipOrder(Exception):
    """钱不够 / 低于最小下单额 / 一点没成交之类，这次不下单（或白下了），但不算出错。"""


def market_min_cost(market: dict, price: float) -> float:
    lim = market.get("limits") or {}
    cmin = (lim.get("cost") or {}).get("min") or 0.0
    amin = (lim.get("amount") or {}).get("min") or 0.0
    return max(float(cmin), float(amin) * price, 1.0)


def market_order_caps(market: dict) -> tuple[float, float]:
    """Gate 市价单上限（超了整单被拒）：买单最多花多少钱、卖单最多卖多少币。没有就是无穷大。"""
    info = market.get("info") or {}
    lim = market.get("limits") or {}
    max_cost = _pos(info.get("market_order_max_money")) or _pos((lim.get("cost") or {}).get("max")) or math.inf
    max_amount = _pos(info.get("market_order_max_stock")) or _pos((lim.get("amount") or {}).get("max")) or math.inf
    return max_cost, max_amount


def tag_prefix(bot_name: str) -> str:
    """订单备注前缀，用来认领自己下的单。

    Gate 的 text 规则：t- 开头，去掉 t- 后不超过 28 字节，只能有 0-9 A-Z a-z _ - .（ccxt 更严：连 t- 不超过 28）。
    完整备注形如 t-ks-1a2b3c-mh3k2x1q，20 个字符。
    """
    return "t-ks-" + hashlib.sha1(bot_name.encode()).hexdigest()[:6]


EXIT_SUFFIX = "-x"  # 止损 / 止盈 / 移动止损卖出的单：对账找回时要恢复冷静期


def new_tag(bot_name: str, at_ms: int | None = None, *, exit: bool = False) -> str:
    return f"{tag_prefix(bot_name)}-{_base36(at_ms or now_ms())}" + (EXIT_SUFFIX if exit else "")


def _base36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = chars[r] + s
    return s or "0"


def _strip_t(s: str) -> str:
    return s[2:] if s.startswith("t-") else s


def order_tag(order: dict) -> str:
    """订单备注（Gate 的 text；ccxt 放在 clientOrderId）。网页/App 下的单是 web、3、101 这类，API 默认是 apiv4。"""
    return str(order.get("clientOrderId") or (order.get("info") or {}).get("text") or "")


def tag_matches(text: str, prefix: str) -> bool:
    """这个备注是不是这个机器人打的。t- 前缀有没有都认，免得被改写后认不出来。"""
    return bool(text) and _strip_t(text).startswith(_strip_t(prefix) + "-")


def order_is_final(order: dict) -> bool:
    """订单是否已经结束（全部成交 / 撤单 / IOC 剩下的被取消）。看 Gate 原始字段，ccxt 状态兜底。

    Gate 现货 status 只有 open / closed / cancelled；finish_as 还会给 ioc、depth_not_enough、small、stp 等，
    这些 ccxt 不认识的值会原样塞进 status，所以不能只看 ccxt 的 status。
    部分成交后被取消的单 status 是 cancelled，但成交的那部分是真的。
    """
    info = order.get("info") or {}
    if str(info.get("status") or "").lower() in ("closed", "cancelled", "finished"):
        return True
    finish_as = str(info.get("finish_as") or "").lower()
    if finish_as and finish_as not in ("open", "_new", "unknown"):
        return True
    return str(order.get("status") or "").lower() in FINAL_STATUSES


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != math.inf else None


def _pos(x) -> float | None:
    v = _f(x)
    return v if v is not None and v > 0 else None


def order_fees(order: dict, market: dict) -> list[tuple[str, float]]:
    """[(币种, 数量)]，币种是 ccxt 统一代码，返佣记负数。

    Gate 原始字段优先：fee（fee_currency）、gt_fee（GT 抵扣）、point_fee（点卡抵扣）、rebated_fee（返还）。
    没有原始字段（别的交易所）就用 ccxt 的 fees / fee。
    """
    info = order.get("info") or {}
    if "fee" in info or "gt_fee" in info:
        ids = {market.get("baseId") or market["base"]: market["base"], market.get("quoteId") or market["quote"]: market["quote"]}
        out = []
        for cur, val, sign in ((info.get("fee_currency"), info.get("fee"), 1), ("GT", info.get("gt_fee"), 1),
                               ("POINT", info.get("point_fee"), 1), (info.get("rebated_fee_currency"), info.get("rebated_fee"), -1)):
            v = _f(val)
            if v:
                out.append((ids.get(cur, cur), sign * v))
        return out
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    return [(f.get("currency"), _f(f.get("cost")) or 0.0) for f in fees if f]


def fill_from_order(order: dict, market: dict, fee_prices: dict | None = None) -> Fill:
    """把交易所返回的订单换算成钱包认的 Fill（到手多少币、花了多少钱、扣了多少手续费）。

    Gate 原始字段优先：filled_amount = 成交量（币），filled_total = 成交额（钱），avg_deal_price = 成交均价。
    ccxt 对 Gate 市价买单的 cost 是“下单金额”、amount 是“下单金额 / 均价”，都不是实际成交，不能直接信。
    手续费：买入一般扣币、卖出扣钱，返佣退回对应币种；开了 GT / 点卡抵扣就扣 GT / 点卡，不动这本账，
    只按 fee_prices（{币种: 折合计价币的价格}）折算进手续费统计，并写进 Fill.note。
    """
    info = order.get("info") or {}
    side = str(order.get("side") or info.get("side") or "").lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"订单方向未知: {side!r}")
    base, quote = market["base"], market["quote"]
    market_buy = side == "buy" and str(order.get("type") or info.get("type") or "").lower() == "market"

    avg = _pos(info.get("avg_deal_price")) or _pos(order.get("average"))
    filled_base = _f(info.get("filled_amount"))
    filled_quote = _f(info.get("filled_total"))
    if filled_quote is None:
        filled_quote = _f(info.get("fill_price"))  # 旧字段名，含义同 filled_total
    if filled_quote is None and market_buy:  # 市价买单：下单金额 - 没花掉的
        amt, left = _f(info.get("amount")), _f(info.get("left"))
        if amt is not None and left is not None:
            filled_quote = amt - left
    if filled_base is None:
        if avg and filled_quote is not None:
            filled_base = filled_quote / avg
        else:
            filled_base = _f(order.get("filled"))
    if filled_quote is None:
        filled_quote = filled_base * avg if filled_base and avg else (None if market_buy else _f(order.get("cost")))
    filled_base, filled_quote = max(filled_base or 0.0, 0.0), max(filled_quote or 0.0, 0.0)
    if not avg and filled_base > 0:
        avg = filled_quote / filled_base

    fee_base = fee_quote = fee_other = 0.0
    notes: list[str] = []
    prices = fee_prices or {}
    for cur, cost in order_fees(order, market):
        if cost < 0:
            notes.append(f"返还手续费 {-cost:.8g} {cur}")
        if cur == base:
            fee_base += cost
        elif cur == quote:
            fee_quote += cost
        else:
            fee_other += cost * (prices.get(cur) or 0.0)
            if cost > 0:
                notes.append(f"手续费用 {cur} 抵扣了 {cost:.8g}（不动这本账）")

    if side == "buy":
        got_base, paid_quote = filled_base - fee_base, filled_quote + fee_quote
    else:
        got_base, paid_quote = filled_base + fee_base, filled_quote - fee_quote
    finish_as = str(info.get("finish_as") or "")
    if finish_as and finish_as not in ("filled", "open") and filled_base > 0:
        notes.append(f"部分成交（{finish_as}）")
    return Fill(
        side=side,
        price=avg or 0.0,
        base=max(got_base, 0.0),
        quote=max(paid_quote, 0.0),
        fee_quote=fee_quote + fee_base * (avg or 0.0) + fee_other,
        ts=order.get("timestamp") or now_ms(),
        order_id=str(order.get("id")) if order.get("id") is not None else None,
        note="；".join(notes),
    )


def _exchange_rejected(e: BaseException) -> bool:
    """ccxt 的 ExchangeError = 交易所明确回了错误（余额不足、参数不对、key 无效……），单子肯定没下成。
    NetworkError / RequestTimeout / BadResponse 或者别的异常 = 不知道下没下成。"""
    names = {c.__name__ for c in type(e).__mro__}
    return "ExchangeError" in names and "NetworkError" not in names


class PaperBroker:
    """模拟盘：用实时价格 + 手续费 + 滑点假装成交。不需要 API key。"""

    mode = "paper"

    def __init__(self, exchange, *, fee: float, max_order: float = math.inf):
        self.ex = exchange
        self.fee = fee
        self.max_order = max_order  # 和实盘一样卡单笔上限，免得模拟盘比实盘“梭”得更猛

    def market(self, symbol: str) -> dict:
        if not self.ex.markets:
            self.ex.load_markets()
        return self.ex.market(symbol)

    def min_cost(self, symbol: str, price: float) -> float:
        return market_min_cost(self.market(symbol), price)

    def sync(self, wallet: Wallet, symbol: str, price: float, prefix: str, known: list, since_ms: int,
             recovered: list | None = None) -> list[str]:
        return []

    def execute(self, plan: OrderPlan, symbol: str, price: float, *, slippage: float, tag: str) -> Fill:
        if plan.side == "buy" and plan.cost > self.max_order:
            plan = OrderPlan("buy", cost=self.max_order)
        fill = simulate_fill(plan, price, fee=self.fee, slippage=slippage, ts=now_ms())
        fill.order_id = tag
        return fill


class LiveBroker:
    """实盘：市价单。买入金额 = min(机器人现金, 账户可用余额, max_order)，卖出数量 = min(机器人持仓, 账户可用币)。

    testnet=True 走 Gate 测试网（api-testnet.gateapi.io，要用测试网的 API key），mode 变成 "testnet"，存档也分开。
    """

    mode = "live"
    POLL_TRIES = 10  # 市价单没立刻结束：最多轮询几次（每次隔 1 秒）
    LOOKUP_TRIES = 3  # 下单请求没回音：按备注找几次（每次隔 2 秒）
    PAGE = 100  # 对账翻历史订单：每页多少笔
    MAX_PAGES = 5  # 最多翻几页

    def __init__(self, exchange, *, max_order: float, testnet: bool = False, sleep=time.sleep):
        self.ex = exchange
        self.max_order = max_order
        self.sleep = sleep
        self._unified: bool | None = None
        opts = getattr(exchange, "options", None)
        if isinstance(opts, dict):
            # 机器人只做现货：只加载现货交易对，少调几个接口，也少几个出错的机会
            opts["fetchMarkets"] = {**(opts.get("fetchMarkets") or {}), "types": ["spot"]}
        if testnet:
            exchange.set_sandbox_mode(True)
            self.mode = "testnet"
        self.ex.load_markets()

    def market(self, symbol: str) -> dict:
        return self.ex.market(symbol)

    def min_cost(self, symbol: str, price: float) -> float:
        return market_min_cost(self.market(symbol), price)

    # ---- 余额 ----
    def _unified_mode(self) -> bool:
        """是不是统一账户（单币种 / 跨币种 / 组合保证金模式）。查不到就当经典现货账户。"""
        if self._unified is None:
            self._unified = False
            try:
                mode = str((self.ex.privateUnifiedGetUnifiedMode() or {}).get("mode") or "classic")
                self._unified = mode != "classic"
            except Exception:
                pass
        return self._unified

    def _balance(self) -> dict:
        """统一账户的钱在 /unified/accounts，经典账户在 /spot/accounts（ccxt 只认得旧版统一账户，这里补上新版）。"""
        return self.ex.fetch_balance({"unifiedAccount": True}) if self._unified_mode() else self.ex.fetch_balance()

    def holdings(self, symbol: str) -> tuple[float, float, float]:
        """(币可用, 币总数含挂单冻结, 计价币可用)。"""
        m = self.market(symbol)
        bal = self._balance()
        free, total = bal.get("free") or {}, bal.get("total") or {}
        base_free = float(free.get(m["base"]) or 0.0)
        return base_free, max(float(total.get(m["base"]) or 0.0), base_free), float(free.get(m["quote"]) or 0.0)

    def free(self, symbol: str) -> tuple[float, float]:
        base_free, _, quote_free = self.holdings(symbol)
        return base_free, quote_free

    # ---- 对账 ----
    def _finished_orders(self, symbol: str, since_ms: int, pages: int | None = None) -> list[dict]:
        """since_ms 以来结束的订单（Gate 按结束时间过滤），自动翻页，按订单号去重。"""
        out: dict[str, dict] = {}
        for page in range(1, (pages or self.MAX_PAGES) + 1):
            batch = self.ex.fetch_closed_orders(symbol, since=since_ms, limit=self.PAGE, params={"page": page})
            for o in batch:
                out.setdefault(str(o.get("id")), o)
            if len(batch) < self.PAGE:
                break
        return list(out.values())

    def sync(self, wallet: Wallet, symbol: str, price: float, prefix: str, known: list, since_ms: int,
             recovered: list | None = None) -> list[str]:
        """开跑前对账：
        1. 找回“单下了但账本没记上”的订单（上次超时 / 崩了 / 存档没推上去），按订单备注认领，订单号记进 known；
           找回的成交以 (symbol, Fill) 追加进 recovered，好写进成交记录；找回的止损卖单（备注带 -x）会恢复冷静期；
        2. 账本里的币比账户里的还多（被手动卖了 / 转走了），以账户为准。
        历史订单查不了就直接报错、这次不交易：不知道上次的单成没成，宁可不动，也不重复下单、不乱改账本。
        """
        notes: list[str] = []
        m = self.market(symbol)
        try:
            orders = self._finished_orders(symbol, since_ms)
        except Exception as e:
            raise RuntimeError(f"查不到 {symbol} 的历史订单，没法对账，这次先不交易（防止重复下单）：{type(e).__name__}: {e}") from e
        seen = {str(k) for k in known}
        for o in sorted(orders, key=lambda o: o.get("timestamp") or 0):
            oid = o.get("id")
            if oid is None or str(oid) in seen or not tag_matches(order_tag(o), prefix) or not order_is_final(o):
                continue
            oid = str(oid)
            fill = fill_from_order(o, m)
            known.append(oid)
            seen.add(oid)
            if fill.base <= 0:
                continue
            wallet.apply(fill, flat_below=market_min_cost(m, fill.price))
            if fill.side == "sell" and order_tag(o).endswith(EXIT_SUFFIX):
                wallet.exit_lock = True  # 那一单是止损/止盈卖的：冷静期接着算
            elif fill.side == "buy":
                wallet.exit_lock = False  # 能买说明当时已经解锁了
            fill.note = "；".join(x for x in ("对账找回", fill.note) if x)
            if recovered is not None:
                recovered.append((symbol, fill))
            notes.append(f"找回漏记的订单 {oid}：{fill.side} {fill.base:.8g} {m['base']} @ {fill.price:.8g}")

        _, base_total, quote_free = self.holdings(symbol)
        if wallet.qty > base_total * 1.001 + 1e-12:
            if base_total <= 0 and quote_free <= 0:  # 一分钱都没有：更像是余额读错了地方（比如统一账户），别乱改账本
                notes.append(f"账户里 {m['base']} 和 {m['quote']} 都是 0，像是余额没读对（统一账户？），这次先不改账本")
                return notes
            notes.append(f"账本记着 {wallet.qty:.8g} {m['base']}，账户里只有 {base_total:.8g}（被手动卖了/转走了？），以账户为准")
            wallet.qty = base_total
            if wallet.qty * price < wallet.dust_value():
                wallet.entry_price = wallet.high_water = wallet.entry_equity = None
        return notes

    # ---- 下单 ----
    def execute(self, plan: OrderPlan, symbol: str, price: float, *, slippage: float, tag: str) -> Fill:
        m = self.market(symbol)
        base_free, quote_free = self.free(symbol)
        min_cost = market_min_cost(m, price)
        max_cost, max_amount = market_order_caps(m)
        if plan.side == "buy":
            cost = self._precise("cost_to_precision", symbol, min(plan.cost, quote_free, self.max_order, max_cost))
            if cost < min_cost:
                raise SkipOrder(f"可用 {m['quote']} 不够：想花 {plan.cost:.2f}，账户可用 {quote_free:.2f}，"
                                f"单笔上限 {self.max_order:.2f}，最小下单 {min_cost:.2f}")

            def send():
                return self.ex.create_market_buy_order_with_cost(symbol, cost, {"text": tag})
        elif plan.side == "sell":
            amount = self._precise("amount_to_precision", symbol, min(plan.amount, base_free, max_amount))
            if amount <= 0 or amount * price < min_cost:
                raise SkipOrder(f"可卖的 {m['base']} 太少：{amount:.8g}（最小下单 {min_cost:.2f} {m['quote']}）")

            def send():
                return self.ex.create_order(symbol, "market", "sell", amount, None, {"text": tag})
        else:
            raise ValueError(f"未知方向 {plan.side!r}")

        started = now_ms()
        try:
            order = send()
        except Exception as e:
            if _exchange_rejected(e):
                raise  # 交易所明确拒单：肯定没下成
            order = self._find_by_tag(symbol, tag, started)
            if order is None:
                raise RuntimeError(f"下单结果不确定（{type(e).__name__}: {e}）。订单备注 {tag}："
                                   "要是其实下成了，下次运行对账会按备注自动找回") from e
        order = self._settle(order, symbol, tag, started)
        fill = fill_from_order(order, m, self._fee_prices(order, m))
        if fill.base <= 0:
            why = (order.get("info") or {}).get("finish_as") or order.get("status")
            raise SkipOrder(f"订单 {order.get('id')} 一点没成交（{why}），这次作罢")
        return fill

    def _precise(self, method: str, symbol: str, value: float) -> float:
        """按交易所精度截断（ccxt 的 amount/cost_to_precision 都是截断）；截成 0 会抛异常，当 0 处理。"""
        try:
            return float(getattr(self.ex, method)(symbol, value))
        except AttributeError:
            return value
        except Exception:
            return 0.0

    def _find_by_tag(self, symbol: str, tag: str, started_ms: int) -> dict | None:
        """下单请求没拿到回复：按备注在刚结束的订单里找这笔单。找不到（或者查不了）返回 None。

        不用“按 text 查单个订单”：Gate 文档说自定义 ID 只在挂单期间能查，市价单一下就结束了。
        """
        for i in range(self.LOOKUP_TRIES):
            if i:
                self.sleep(2)
            try:
                for o in self._finished_orders(symbol, started_ms - 60_000, pages=1):
                    if _strip_t(order_tag(o)) == _strip_t(tag):
                        return o
            except Exception:
                pass
        return None

    def _settle(self, order: dict, symbol: str, tag: str, started_ms: int) -> dict:
        """市价单一般秒成交；万一返回时还没结束就轮询几秒。还没结束就报错：不记半截账，留给下次对账。"""
        last_err = None
        for _ in range(self.POLL_TRIES):
            if order_is_final(order):
                return order
            self.sleep(1)
            try:
                if order.get("id") is None:  # 回执里连订单号都没有：按备注找
                    order = self._find_by_tag(symbol, tag, started_ms) or order
                else:
                    order = self.ex.fetch_order(str(order["id"]), symbol)
            except Exception as e:
                last_err = e
        if order_is_final(order):
            return order
        why = f"，最后一次查询出错：{type(last_err).__name__}: {last_err}" if last_err else ""
        raise RuntimeError(f"订单 {order.get('id') or tag} 过了 {self.POLL_TRIES} 秒还没结束（status={order.get('status')}）{why}；"
                           "先不记账，下次运行对账时按备注找回")

    def _fee_prices(self, order: dict, market: dict) -> dict:
        """用 GT 抵扣了手续费：查一下 GT 价格，好把手续费折成计价币记进统计。查不到就算了。"""
        info = order.get("info") or {}
        if not _pos(info.get("gt_fee")) or "GT" in (market["base"], market["quote"]):
            return {}
        try:
            t = self.ex.fetch_ticker(f"GT/{market['quote']}")
            px = _pos(t.get("last") or t.get("close"))
            return {"GT": px} if px else {}
        except Exception:
            return {}

    # ---- 体检 ----
    def preflight(self, symbols=()) -> list[str]:
        """实盘体检（不下单，只读）：API key 绑没绑 IP、交易对白名单、统一账户模式、实际费率。返回给人看的提示。"""
        notes: list[str] = []
        try:
            d = self.ex.privateAccountGetDetail() or {}
        except Exception as e:
            d = None
            notes.append(f"查不到 API key 信息：{type(e).__name__}: {e}")
        if d is not None:
            if not d.get("ip_whitelist"):
                notes.append("API key 没绑 IP 白名单：Gate 规定这种 key 创建 90 天后自动失效（到期前会提醒），"
                             "到期后机器人会报 INVALID_KEY，记得提前换 key 并更新 GitHub Secrets")
            allowed = set(d.get("currency_pairs") or [])
            wanted = {self.market(s)["id"] for s in symbols if s in (self.ex.markets or {})}
            if allowed and wanted - allowed:
                notes.append(f"API key 限定了交易对，这些不在白名单里、会下单失败：{sorted(wanted - allowed)}")
            if (d.get("key") or {}).get("mode") == 2:
                notes.append("账户是旧版统一账户（key mode=2），ccxt 会用 account=unified 下单")
        if self._unified_mode():
            notes.append("统一账户模式：余额从 /unified/accounts 读")
        for s in dict.fromkeys(symbols):
            try:
                f = self.ex.fetch_trading_fee(s)
                notes.append(f"{s} 实际费率：taker {float(f['taker']):.3%}，maker {float(f['maker']):.3%}")
            except Exception as e:
                notes.append(f"{s} 查费率失败：{type(e).__name__}: {e}")
        return notes
