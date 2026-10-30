"""实盘下单模块：订单换算、下单、超时找回、开跑对账。

假交易所吐出的订单都是 Gate 原始 JSON 经过 ccxt 真正的 gate.parse_order 解析出来的，
字段长相（info 原始字典、ccxt 的 cost/amount/fees/status）和实盘一模一样。
"""
import json
from decimal import ROUND_DOWN, Decimal

import ccxt
import pytest

from bot.brokers import (
    LiveBroker,
    SkipOrder,
    fill_from_order,
    market_order_caps,
    new_tag,
    order_is_final,
    tag_matches,
    tag_prefix,
)
from bot.candles import now_ms
from bot.config import load_config
from bot.engine import OrderPlan
from bot.run import tick
from bot.wallet import Fill, Wallet

from .helpers import FakeExchange, make_candles

GATE = ccxt.gate()  # 只用来解析订单，不联网
H = 3_600_000
PX = 66_000.0
BOT = "t-hodl"
PREFIX = tag_prefix(BOT)


def _market(base, amount_prec, price_prec, **info):
    return {"id": f"{base}_USDT", "symbol": f"{base}/USDT", "base": base, "quote": "USDT", "baseId": base, "quoteId": "USDT",
            "type": "spot", "spot": True, "margin": True, "swap": False, "future": False, "option": False,
            "contract": False, "linear": None, "inverse": None, "contractSize": None, "active": True,
            "precision": {"amount": amount_prec, "price": price_prec},
            "limits": {"amount": {"min": amount_prec, "max": None}, "cost": {"min": 3.0, "max": 5_000_000.0}},
            "info": {"id": f"{base}_USDT", "min_quote_amount": "3", "slippage": "0.03", **info}}


BTC = _market("BTC", 1e-6, 0.1, market_order_max_money="5000000", market_order_max_stock="61")
GT = _market("GT", 0.01, 0.01, market_order_max_money="300000", market_order_max_stock="28805")


def raw_order(**kw) -> dict:
    """一张 Gate 现货订单（POST /spot/orders、GET /spot/orders 返回的原样）：默认是已全部成交的 100 USDT 市价买单。"""
    r = {"id": "1033441234567", "text": "t-ks-abc123-mh3k2x1q", "amend_text": "-", "create_time": "1793340000",
         "update_time": "1793340000", "create_time_ms": 1793340000123, "update_time_ms": 1793340000125,
         "status": "closed", "currency_pair": "BTC_USDT", "type": "market", "account": "spot", "side": "buy",
         "amount": "100", "price": "0", "time_in_force": "ioc", "iceberg": "0", "left": "0.01",
         "filled_amount": "0.001515", "fill_price": "99.99", "filled_total": "99.99", "avg_deal_price": "66000",
         "fee": "0.00000303", "fee_currency": "BTC", "point_fee": "0", "gt_fee": "0", "gt_maker_fee": "0",
         "gt_taker_fee": "0", "gt_discount": False, "rebated_fee": "0", "rebated_fee_currency": "USDT",
         "stp_id": 0, "stp_act": "-", "finish_as": "filled"}
    r.update(kw)
    return r


def ccxt_order(raw: dict, market: dict = BTC) -> dict:
    return GATE.parse_order(json.loads(json.dumps(raw)), market)


def _s(x: float) -> str:
    return f"{x:.12g}"


def _trunc(x: float, step: float) -> str:
    q = Decimal(str(step))
    return str(Decimal(str(x)).quantize(q, rounding=ROUND_DOWN))


class FakeGate(FakeExchange):
    """够 LiveBroker 用的假 Gate：真扣余额、真记历史订单，返回值都过一遍 ccxt 的 parse_order。

    script：接下来每次下单怎么表现，例如
        {"fill": 0.6, "finish_as": "ioc"}                 部分成交
        {"error": ccxt.RequestTimeout(), "hidden": True}  下成了但回复丢了，而且一时查不到
        {"open": True}                                    回执里还是 open（要轮询）
    """

    def __init__(self, *, btc=0.0, btc_locked=0.0, usdt=1000.0, gt=10.0, candles=None, price=PX):
        super().__init__(candles or make_candles([price] * 5), last=price)
        self.markets = {"BTC/USDT": BTC, "GT/USDT": GT}
        self.options = {"fetchMarkets": {"types": ["spot", "swap", "future", "option"]}}
        self.bal = {"BTC": [btc, btc_locked], "USDT": [usdt, 0.0], "GT": [gt, 0.0]}
        self.prices = {"BTC/USDT": price, "GT/USDT": 4.5}
        self.script: list[dict] = []
        self.history: list[dict] = []  # 交易所里已经结束的订单（原始 JSON）
        self.hidden: list[dict] = []  # 已经成交、但暂时查不到的订单
        self.created: list[dict] = []
        self.balance_params: list = []
        self.history_calls = 0
        self.history_error: Exception | None = None
        self.fetch_order_error: Exception | None = None
        self.open_polls = 0  # fetch_order 还要返回几次 open
        self.unified_mode: str | None = None
        self.account_detail: dict = {"user_id": 1, "ip_whitelist": ["1.2.3.4"], "currency_pairs": [], "key": {"mode": 1}, "tier": 0}
        self.seq = 1033441234000

    # ---- 账户 ----
    def fetch_balance(self, params=None):
        self.balance_params.append(params)
        free = {k: v[0] for k, v in self.bal.items()}
        used = {k: v[1] for k, v in self.bal.items()}
        return {"free": free, "used": used, "total": {k: free[k] + used[k] for k in free}}

    def privateUnifiedGetUnifiedMode(self, params=None):
        if self.unified_mode is None:
            raise ccxt.PermissionDenied('gate {"label":"FORBIDDEN","message":"Forbidden"}')
        return {"mode": self.unified_mode, "settings": {}}

    def privateAccountGetDetail(self, params=None):
        return self.account_detail

    def fetch_trading_fee(self, symbol, params=None):
        return {"symbol": symbol, "taker": 0.001, "maker": 0.001}

    def fetch_ticker(self, symbol):
        return {"last": self.prices[symbol], "close": self.prices[symbol]}

    def amount_to_precision(self, symbol, amount):
        s = _trunc(amount, self.markets[symbol]["precision"]["amount"])
        if Decimal(s) == 0:
            raise ccxt.InvalidOrder(f"gate amount of {symbol} must be greater than minimum amount precision")
        return s

    def cost_to_precision(self, symbol, cost):
        return _trunc(cost, self.markets[symbol]["precision"]["price"])

    # ---- 下单 ----
    def create_market_buy_order_with_cost(self, symbol, cost, params=None):
        return self._create(symbol, "buy", float(cost), params or {})

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        assert type == "market"
        return self._create(symbol, side, float(amount), params or {})

    def _create(self, symbol, side, qty, params):
        spec = self.script.pop(0) if self.script else {}
        m = self.markets[symbol]
        px = self.prices[symbol]
        ratio = spec.get("fill", 1.0)
        base, quote = m["base"], m["quote"]
        if side == "buy":  # 市价买：qty 是计价币金额
            total = qty * ratio
            got = total / px
            fee = got * 0.002
            left = qty - total
            self.bal[quote][0] -= total
            self.bal[base][0] += got - (0 if spec.get("gt") else fee)
        else:
            got = qty * ratio
            total = got * px
            fee = total * 0.002
            left = qty - got
            self.bal[base][0] -= got
            self.bal[quote][0] += total - (0 if spec.get("gt") else fee)
        self.seq += 1
        raw = raw_order(
            id=str(self.seq), text=params.get("text", "apiv4"), currency_pair=m["id"], side=side, amount=_s(qty),
            left=_s(left), filled_amount=_s(got), filled_total=_s(total), fill_price=_s(total),
            avg_deal_price=_s(px) if got else "0", fee="0" if spec.get("gt") else _s(fee),
            fee_currency=base if side == "buy" else quote, rebated_fee_currency=quote if side == "buy" else base,
            gt_fee="0.0123" if spec.get("gt") else "0", gt_discount=bool(spec.get("gt")),
            status="closed" if ratio == 1 else "cancelled", finish_as=spec.get("finish_as", "filled" if ratio == 1 else "ioc"),
            create_time_ms=now_ms(), update_time_ms=now_ms(),
        )
        self.created.append(raw)
        (self.hidden if spec.get("hidden") else self.history).append(raw)
        if "error" in spec:
            raise spec["error"]
        if spec.get("open"):
            self.open_polls = spec.get("polls", 1)
            return ccxt_order({**raw, "status": "open", "finish_as": "open", "left": raw["amount"], "filled_amount": "0",
                               "filled_total": "0", "fill_price": "0", "avg_deal_price": "0", "fee": "0"}, m)
        return ccxt_order(raw, m)

    def reveal(self):
        self.history += self.hidden
        self.hidden = []

    # ---- 查单 ----
    def fetch_order(self, id, symbol=None, params=None):
        if self.fetch_order_error is not None:
            raise self.fetch_order_error
        raw = next(r for r in self.history + self.hidden if r["id"] == id)
        if self.open_polls > 0:
            self.open_polls -= 1
            return ccxt_order({**raw, "status": "open", "finish_as": "open"}, self.markets[symbol])
        return ccxt_order(raw, self.markets[symbol])

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        self.history_calls += 1
        if self.history_error is not None:
            raise self.history_error
        page = (params or {}).get("page", 1)
        m = self.markets[symbol]
        rows = [r for r in self.history if r["currency_pair"] == m["id"] and (since is None or r["update_time_ms"] >= since)]
        rows.sort(key=lambda r: r["update_time_ms"], reverse=True)  # 新的在前
        limit = limit or 100
        return [ccxt_order(r, m) for r in rows[(page - 1) * limit: page * limit]]


def broker(ex, **kw):
    return LiveBroker(ex, max_order=kw.pop("max_order", 500), sleep=lambda s: None, **kw)


def past(**kw):
    """一张早就结束了的订单，直接塞进交易所历史。"""
    return raw_order(**{"create_time_ms": now_ms() - H, "update_time_ms": now_ms() - H, **kw})


# ---------------- 订单 -> Fill ----------------

def test_fill_market_buy_fee_in_base():
    o = ccxt_order(raw_order())
    assert o["cost"] == pytest.approx(100)  # ccxt 给的是下单金额，不是成交额
    f = fill_from_order(o, BTC)
    assert f.side == "buy" and f.price == pytest.approx(66000)
    assert f.quote == pytest.approx(99.99)  # 实际花掉的钱
    assert f.base == pytest.approx(0.001515 - 0.00000303)  # 到手的币 = 成交量 - 手续费（扣币）
    assert f.fee_quote == pytest.approx(0.00000303 * 66000)
    assert f.order_id == "1033441234567" and f.ts == 1793340000123


def test_fill_market_sell_fee_in_quote():
    o = ccxt_order(raw_order(side="sell", amount="0.001512", left="0", filled_amount="0.001512", fill_price="105.84",
                             filled_total="105.84", avg_deal_price="70000", fee="0.21168", fee_currency="USDT",
                             rebated_fee_currency="BTC"))
    f = fill_from_order(o, BTC)
    assert f.side == "sell" and f.base == pytest.approx(0.001512)  # 卖掉的币
    assert f.quote == pytest.approx(105.84 - 0.21168)  # 到手的钱 = 成交额 - 手续费（扣钱）
    assert f.fee_quote == pytest.approx(0.21168)


def test_fill_gt_deducted_fee_does_not_touch_ledger():
    o = ccxt_order(raw_order(fee="0", gt_fee="0.0123", gt_taker_fee="0.0123", gt_discount=True))
    f = fill_from_order(o, BTC, {"GT": 4.5})
    assert f.base == pytest.approx(0.001515) and f.quote == pytest.approx(99.99)  # 手续费扣的是 GT，币和钱都不少
    assert f.fee_quote == pytest.approx(0.0123 * 4.5)
    assert "GT" in f.note
    assert fill_from_order(o, BTC).fee_quote == 0  # 不知道 GT 价格就不折算


def test_fill_gt_fee_on_gt_pair_is_a_base_fee():
    o = ccxt_order(raw_order(currency_pair="GT_USDT", amount="45", left="0", filled_amount="10", filled_total="45",
                             fill_price="45", avg_deal_price="4.5", fee="0", fee_currency="GT", gt_fee="0.017",
                             gt_discount=True), GT)
    f = fill_from_order(o, GT)
    assert f.base == pytest.approx(10 - 0.017)  # 买 GT 时用 GT 抵扣：从到手的 GT 里扣


def test_fill_point_fee_and_rebate():
    o = ccxt_order(raw_order(side="sell", amount="0.001", left="0", filled_amount="0.001", filled_total="66",
                             fill_price="66", fee="0", fee_currency="USDT", point_fee="0.1", rebated_fee="0.01",
                             rebated_fee_currency="USDT"))
    f = fill_from_order(o, BTC)
    assert f.base == pytest.approx(0.001) and f.quote == pytest.approx(66.01)  # 点卡抵扣不动账本，返佣到手
    assert "POINT" in f.note and "返还" in f.note


def test_fill_partial_ioc_counts_only_what_traded():
    o = ccxt_order(raw_order(status="cancelled", finish_as="ioc", left="40", filled_total="60", fill_price="60",
                             filled_amount="0.000909", avg_deal_price="66006.6", fee="0.000001818"))
    assert o["status"] == "canceled" and order_is_final(o)
    f = fill_from_order(o, BTC)
    assert f.quote == pytest.approx(60) and f.base == pytest.approx(0.000909 - 0.000001818)
    assert "部分成交" in f.note


def test_fill_falls_back_when_gate_fields_missing():
    raw = raw_order()
    for k in ("filled_amount", "filled_total"):
        raw.pop(k)
    f = fill_from_order(ccxt_order(raw), BTC)  # 只剩旧字段 fill_price
    assert f.quote == pytest.approx(99.99) and f.base == pytest.approx(99.99 / 66000 - 0.00000303)
    raw.pop("fill_price")
    f = fill_from_order(ccxt_order(raw), BTC)  # 再退一步：下单金额 - 剩余
    assert f.quote == pytest.approx(99.99)


def test_order_is_final_understands_gate_finish_as():
    for fa in ("filled", "ioc", "depth_not_enough", "trader_not_enough", "small", "stp", "fok"):
        o = ccxt_order(raw_order(status="cancelled" if fa != "filled" else "closed", finish_as=fa))
        assert order_is_final(o), fa
    assert not order_is_final(ccxt_order(raw_order(status="open", finish_as="open")))


# ---------------- 备注 ----------------

def test_tag_is_valid_gate_text_and_matches_its_bot():
    t = new_tag(BOT, at_ms=1793340000123)
    assert t.startswith("t-") and len(t) <= 28 and all(c.isalnum() or c in "_-." for c in t)
    assert tag_matches(t, PREFIX) and tag_matches(t[2:], PREFIX)  # t- 被去掉也认
    assert not tag_matches(new_tag("other-bot"), PREFIX)
    assert not tag_matches("apiv4", PREFIX) and not tag_matches("", PREFIX)


def test_market_order_caps_from_gate_info():
    assert market_order_caps(BTC) == (5_000_000.0, 61.0)


# ---------------- 下单 ----------------

def test_live_broker_loads_spot_markets_only():
    ex = FakeGate()
    assert broker(ex).mode == "live"
    assert ex.options["fetchMarkets"]["types"] == ["spot"]


def test_testnet_switches_to_sandbox_and_separate_mode():
    ex = FakeGate()
    ex.set_sandbox_mode = lambda on: setattr(ex, "sandbox", on)
    b = broker(ex, testnet=True)
    assert ex.sandbox is True and b.mode == "testnet"  # 存档目录 state/testnet，和实盘分开


def test_execute_buy_caps_cost_and_tags_order():
    ex = FakeGate(usdt=120)
    b = broker(ex, max_order=100)
    tag = new_tag(BOT)
    f = b.execute(OrderPlan("buy", cost=150), "BTC/USDT", PX, slippage=0.0, tag=tag)
    sent = ex.created[-1]
    assert float(sent["amount"]) == pytest.approx(100) and sent["text"] == tag  # min(计划 150, 可用 120, 上限 100)
    assert f.quote == pytest.approx(100) and f.base == pytest.approx(100 / PX * 0.998)
    assert f.order_id == sent["id"]


def test_execute_buy_below_min_cost_is_skipped():
    ex = FakeGate(usdt=2.5)
    with pytest.raises(SkipOrder, match="不够"):
        broker(ex).execute(OrderPlan("buy", cost=50), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert ex.created == []


def test_execute_sell_truncates_to_amount_precision():
    ex = FakeGate(btc=0.0015119)
    f = broker(ex).execute(OrderPlan("sell", amount=0.0015119), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert ex.created[-1]["amount"] == "0.001511"  # 截断，不四舍五入（多卖会被拒）
    assert f.base == pytest.approx(0.001511) and f.quote == pytest.approx(0.001511 * PX * 0.998)


def test_execute_sell_never_sells_more_than_the_account_has():
    ex = FakeGate(btc=0.001)
    broker(ex).execute(OrderPlan("sell", amount=0.002), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert ex.created[-1]["amount"] == "0.001"


def test_execute_sell_dust_is_skipped():
    ex = FakeGate(btc=0.00004)  # 约 2.6 USDT，低于最小下单 3 USDT
    with pytest.raises(SkipOrder, match="太少"):
        broker(ex).execute(OrderPlan("sell", amount=0.00004), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    with pytest.raises(SkipOrder):  # 截断后是 0：ccxt 会抛 InvalidOrder，当成卖不了
        broker(FakeGate(btc=4e-7)).execute(OrderPlan("sell", amount=4e-7), "BTC/USDT", PX, slippage=0.0, tag="t-x")


def test_execute_partial_fill_returns_what_traded():
    ex = FakeGate()
    ex.script = [{"fill": 0.6, "finish_as": "depth_not_enough"}]  # 滑点保护：超出部分被取消
    f = broker(ex).execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert f.quote == pytest.approx(60) and "部分成交" in f.note


def test_execute_zero_fill_is_a_skip_not_a_fill():
    ex = FakeGate()
    ex.script = [{"fill": 0.0, "finish_as": "small"}]
    with pytest.raises(SkipOrder, match="没成交"):
        broker(ex).execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))


def test_execute_polls_until_order_is_final():
    ex = FakeGate()
    ex.script = [{"open": True, "polls": 2}]
    f = broker(ex).execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert f.quote == pytest.approx(100)


def test_execute_gt_fee_is_valued_at_gt_price():
    ex = FakeGate()
    ex.script = [{"gt": True}]
    f = broker(ex).execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert f.base == pytest.approx(100 / PX) and f.fee_quote == pytest.approx(0.0123 * 4.5)


def test_execute_exchange_rejection_is_raised_without_lookup():
    ex = FakeGate()
    ex.script = [{"error": ccxt.InsufficientFunds('gate {"label":"BALANCE_NOT_ENOUGH","message":"Not enough balance"}'),
                  "hidden": True}]
    with pytest.raises(ccxt.InsufficientFunds):
        broker(ex).execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert ex.history_calls == 0  # 交易所明确拒单：不用去找


def test_execute_timeout_but_order_went_through_is_found_by_tag():
    ex = FakeGate()
    ex.script = [{"error": ccxt.RequestTimeout("gate POST https://api.gateio.ws/api/v4/spot/orders request timeout")}]
    tag = new_tag(BOT)
    f = broker(ex).execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=tag)
    assert f.order_id == ex.created[-1]["id"] and f.quote == pytest.approx(100)
    assert len(ex.created) == 1  # 没有重下


def test_execute_timeout_unknown_outcome_then_sync_recovers_once():
    ex = FakeGate(usdt=100)
    b = broker(ex)
    ex.script = [{"error": ccxt.RequestTimeout("timed out"), "hidden": True}]
    with pytest.raises(RuntimeError, match="不确定"):
        b.execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    assert len(ex.created) == 1

    ex.reveal()  # 下次运行时，这笔单已经能查到了
    w, known = Wallet.new(100), []
    notes = b.sync(w, "BTC/USDT", PX, PREFIX, known, now_ms() - 3 * 86_400_000)
    assert "找回" in notes[0] and known == [ex.created[0]["id"]]
    assert w.cash == pytest.approx(0) and w.qty == pytest.approx(100 / PX * 0.998)
    assert b.sync(w, "BTC/USDT", PX, PREFIX, known, now_ms() - 3 * 86_400_000) == []  # 幂等：不会记两次
    assert w.fills == 1


def test_execute_status_unknown_after_acceptance_raises_then_sync_recovers():
    ex = FakeGate(usdt=100)
    b = broker(ex)
    ex.script = [{"open": True, "polls": 99}]
    ex.fetch_order_error = ccxt.NetworkError("connection reset")
    with pytest.raises(RuntimeError, match="还没结束"):
        b.execute(OrderPlan("buy", cost=100), "BTC/USDT", PX, slippage=0.0, tag=new_tag(BOT))
    w, known = Wallet.new(100), []
    b.sync(w, "BTC/USDT", PX, PREFIX, known, now_ms() - 3 * 86_400_000)
    assert w.qty == pytest.approx(100 / PX * 0.998) and len(known) == 1


# ---------------- 开跑对账 ----------------

def test_sync_recovers_only_own_unknown_finished_orders():
    other = tag_prefix("other-bot")
    ex = FakeGate(btc=1.0)
    ex.history = [
        past(id="1", text=f"{PREFIX}-aaa"),  # 自己的、没记过：找回
        past(id="2", text="apiv4"),  # API 手动下的：不认
        past(id="3", text="3", side="sell", fee_currency="USDT"),  # 网页下的：不认
        past(id="4", text=f"{PREFIX}-bbb"),  # 自己的、已经记过：跳过
        past(id="5", text=f"{other}-ccc"),  # 别的机器人的：不认
        past(id="6", text=f"{PREFIX}-ddd", status="cancelled", finish_as="small", filled_total="0", fill_price="0",
             filled_amount="0", avg_deal_price="0", fee="0"),  # 自己的、没成交：只记订单号
    ]
    w, known = Wallet.new(200), ["4"]
    notes = broker(ex).sync(w, "BTC/USDT", PX, PREFIX, known, now_ms() - 3 * 86_400_000)
    assert sorted(known) == ["1", "4", "6"]
    assert w.fills == 1 and w.qty == pytest.approx(0.001515 - 0.00000303) and w.cash == pytest.approx(200 - 99.99)
    assert len([n for n in notes if "找回" in n]) == 1


def test_sync_reconciles_ledger_down_to_account_total():
    ex = FakeGate(btc=0.003, btc_locked=0.001)  # 账户一共 0.004（其中 0.001 挂单冻结）
    w = Wallet.new(1000)
    w.apply(Fill("buy", PX, 0.01, 660.0))
    notes = broker(ex).sync(w, "BTC/USDT", PX, PREFIX, [], now_ms() - 3 * 86_400_000)
    assert w.qty == pytest.approx(0.004) and "以账户为准" in notes[-1]  # 冻结的币还是你的，按总数对
    assert w.in_position

    w2 = Wallet.new(1000)
    w2.apply(Fill("buy", PX, 0.001, 66.0))
    assert broker(FakeGate(btc=0.5)).sync(w2, "BTC/USDT", PX, PREFIX, [], 0) == []  # 账户比账本多：别的钱，不认领
    assert w2.qty == pytest.approx(0.001)

    w3 = Wallet.new(1000)
    w3.apply(Fill("buy", PX, 0.01, 660.0))
    broker(FakeGate(btc=0.0, usdt=700)).sync(w3, "BTC/USDT", PX, PREFIX, [], 0)  # 被手动卖光：账本清零、视为空仓
    assert w3.qty == 0 and not w3.in_position

    w4 = Wallet.new(1000)
    w4.apply(Fill("buy", PX, 0.01, 660.0))
    notes = broker(FakeGate(btc=0.0, usdt=0.0)).sync(w4, "BTC/USDT", PX, PREFIX, [], 0)  # 全是 0：像是余额读错了
    assert w4.qty == pytest.approx(0.01) and "先不改账本" in notes[-1]


def test_sync_refuses_to_trade_blind_when_history_unavailable():
    ex = FakeGate(btc=0.0)
    ex.history_error = ccxt.ExchangeNotAvailable('gate {"label":"SERVER_ERROR","message":"Internal server error"}')
    w = Wallet.new(1000)
    w.apply(Fill("buy", PX, 0.01, 660.0))
    with pytest.raises(RuntimeError, match="不交易"):
        broker(ex).sync(w, "BTC/USDT", PX, PREFIX, [], now_ms() - 3 * 86_400_000)
    assert w.qty == pytest.approx(0.01)  # 查不清楚就不动账本（可能是漏记的卖单，不能只扣币不加钱）


def test_sync_pages_through_long_history():
    ex = FakeGate(btc=1.0)
    ex.history = [past(id=str(i), text="apiv4", update_time_ms=now_ms() - H + i) for i in range(150)]
    ex.history.append(past(id="old-orphan", text=f"{PREFIX}-x", update_time_ms=now_ms() - 2 * H))  # 排在第 2 页
    w, known = Wallet.new(200), []
    broker(ex).sync(w, "BTC/USDT", PX, PREFIX, known, now_ms() - 3 * 86_400_000)
    assert known == ["old-orphan"] and ex.history_calls == 2


def test_unified_account_balance_comes_from_unified_endpoint():
    ex = FakeGate(usdt=50)
    ex.unified_mode = "multi_currency"
    b = broker(ex)
    assert b.free("BTC/USDT") == (0.0, 50.0)
    assert ex.balance_params[-1] == {"unifiedAccount": True}
    ex2 = FakeGate(usdt=50)  # 经典账户（或者 key 没权限查模式）：走默认
    broker(ex2).free("BTC/USDT")
    assert ex2.balance_params[-1] is None


def test_preflight_warns_about_unbound_key_and_pair_whitelist():
    ex = FakeGate()
    ex.account_detail = {"user_id": 1, "ip_whitelist": [], "currency_pairs": ["ETH_USDT"], "key": {"mode": 1}, "tier": 0}
    notes = broker(ex).preflight(["BTC/USDT"])
    text = "\n".join(notes)
    assert "90 天" in text and "BTC_USDT" in text and "taker 0.100%" in text


# ---------------- 端到端：run.tick + LiveBroker ----------------

CONFIG = """
[exchange]
id = "gate"
mode = "live"

[[bots]]
name = "t-hodl"
symbol = "BTC/USDT"
timeframe = "1h"
strategy = "hodl"
budget = 100
"""


@pytest.fixture
def live_setup(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    cfg = load_config(path, env={"KURUMI_STATE_DIR": str(tmp_path / "state")})
    t0 = (now_ms() // H - 60) * H
    ex = FakeGate(usdt=100, candles=make_candles([PX] * 60, "1h", t0=t0))
    return cfg, ex, broker(ex)


def _state(cfg):
    return json.loads((cfg.state_dir("live") / "bots" / f"{BOT}.json").read_text())


def test_tick_timeout_then_next_run_recovers_without_double_buy(live_setup):
    cfg, ex, b = live_setup
    ex.script = [{"error": ccxt.RequestTimeout("timed out"), "hidden": True}]
    with pytest.raises(RuntimeError, match="不确定"):
        tick(cfg.bots[0], cfg, ex=ex, broker=b, fear_greed=None)
    ex.reveal()
    rep = tick(cfg.bots[0], cfg, ex=ex, broker=b, fear_greed=None)
    assert rep["fill"] is None and any("找回" in n for n in rep["notes"])
    assert len(ex.created) == 1  # 只下过一次单
    st = _state(cfg)
    assert st["orders"] == [ex.created[0]["id"]] and st["wallet"]["qty"] == pytest.approx(100 / PX * 0.998)
    tick(cfg.bots[0], cfg, ex=ex, broker=b, fear_greed=None)
    assert len(ex.created) == 1 and _state(cfg)["wallet"]["fills"] == 1


def test_tick_lost_state_push_is_recovered_from_exchange(live_setup):
    cfg, ex, b = live_setup
    rep = tick(cfg.bots[0], cfg, ex=ex, broker=b, fear_greed=None)
    assert rep["fill"]["side"] == "buy"
    (cfg.state_dir("live") / "bots" / f"{BOT}.json").unlink()  # 存档没推上去：下次拿到的是旧状态
    rep = tick(cfg.bots[0], cfg, ex=ex, broker=b, fear_greed=None)
    assert rep["fill"] is None and len(ex.created) == 1
    assert _state(cfg)["wallet"]["qty"] == pytest.approx(100 / PX * 0.998)
