"""决策引擎：实盘、模拟盘、回测三方共用，保证“回测里赚的钱”和“实盘里的逻辑”是同一套。

流程：
    decide()      策略信号 + 风控规则 -> 目标仓位（纯函数，不碰钱包）
    apply_events  把熔断 / 冷静期这些状态写回钱包
    plan_order()  目标仓位 vs 当前仓位 -> 要不要下单、下多少
    simulate_fill 模拟盘和回测用的撮合（手续费 + 滑点）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .strategies import Context, Strategy, normalize
from .wallet import Fill, Wallet


@dataclass(frozen=True)
class Risk:
    stop_loss: float = 0.0  # 0.15 = 跌破成本 15% 割肉；0 = 不止损（久留美模式）
    take_profit: float = 0.0  # 0.5 = 赚 50% 落袋；0 = 让利润奔跑
    trailing_stop: float = 0.0  # 0.2 = 从持仓最高点回撤 20% 离场
    kill_switch: float = 0.0  # 0.5 = 净值跌到预算一半就清仓罢工；0 = 战至最后一个铜板

    def __post_init__(self):
        for name in ("stop_loss", "take_profit", "trailing_stop", "kill_switch"):
            v = getattr(self, name)
            if not 0 <= v < 10:
                raise ValueError(f"risk.{name} 应该是 0~1 之间的小数（比如 0.15），收到 {v}")
        if self.stop_loss >= 1 or self.trailing_stop >= 1 or self.kill_switch >= 1:
            raise ValueError("stop_loss / trailing_stop / kill_switch 必须小于 1")


@dataclass(frozen=True)
class Decision:
    target: float  # 目标仓位 0~1（已乘梭哈系数）
    signal: float | None  # 策略原始信号 0~1；被风控截胡时为 None
    event: str | None  # stop_loss / take_profit / trailing_stop / kill_switch / halted / locked / unlocked
    reason: str


@dataclass(frozen=True)
class OrderPlan:
    side: str  # "buy" | "sell"
    cost: float = 0.0  # 买入：打算花多少钱
    amount: float = 0.0  # 卖出：打算卖多少币
    meta: dict = field(default_factory=dict, compare=False)


EXIT_EVENTS = ("stop_loss", "take_profit", "trailing_stop")


def decide(
    strategy: Strategy,
    params: dict,
    candles,
    wallet: Wallet,
    price: float,
    *,
    size: float = 1.0,
    risk: Risk = Risk(),
    ctx: Context,
) -> Decision:
    if wallet.halted:
        return Decision(0.0, None, "halted", "已熔断，罢工中（删掉 state 里这个机器人的文件可复活）")

    equity = wallet.equity(price)
    if risk.kill_switch > 0 and equity <= wallet.budget * (1 - risk.kill_switch):
        return Decision(0.0, None, "kill_switch", f"净值 {equity:.2f} 跌破熔断线，清仓停机")

    if wallet.in_position and wallet.entry_price:
        chg = price / wallet.entry_price - 1
        if risk.stop_loss > 0 and chg <= -risk.stop_loss:
            return Decision(0.0, None, "stop_loss", f"止损：较成本 {chg:+.1%}")
        if risk.take_profit > 0 and chg >= risk.take_profit:
            return Decision(0.0, None, "take_profit", f"止盈：较成本 {chg:+.1%}")
        if risk.trailing_stop > 0 and wallet.high_water and price <= wallet.high_water * (1 - risk.trailing_stop):
            dd = price / wallet.high_water - 1
            return Decision(0.0, None, "trailing_stop", f"移动止损：较持仓最高点 {dd:+.1%}")

    position = min(1.0, wallet.exposure(price) / size) if size > 0 else 0.0
    signal, note = normalize(strategy.fn(candles, position, params, ctx))

    if wallet.exit_lock:
        if signal <= 0:
            return Decision(0.0, signal, "unlocked", "信号归零，冷静期结束" + (f"（{note}）" if note else ""))
        return Decision(0.0, signal, "locked", "刚止盈/止损过，等信号先归零再上车" + (f"（{note}）" if note else ""))

    return Decision(signal * size, signal, None, note)


def apply_events(wallet: Wallet, decision: Decision) -> None:
    if decision.event == "kill_switch":
        wallet.halted = True
    elif decision.event in EXIT_EVENTS:
        wallet.exit_lock = True
    elif decision.event == "unlocked":
        wallet.exit_lock = False


def plan_order(
    target: float,
    wallet: Wallet,
    price: float,
    *,
    threshold: float = 0.02,
    min_cost: float = 1.0,
) -> OrderPlan | None:
    """目标仓位 -> 订单。仓位差小于 threshold（占净值比例）就不折腾，省手续费。"""
    equity = wallet.equity(price)
    if equity <= 0 or price <= 0:
        return None
    have = wallet.qty * price

    if target <= 0:  # 清仓：有多少卖多少
        if have < min_cost:
            return None
        return OrderPlan("sell", amount=wallet.qty)

    diff = target * equity - have
    if abs(diff) < threshold * equity:
        return None
    if diff > 0:
        cost = wallet.cash if target >= 1 - 1e-9 else min(diff, wallet.cash)  # 梭哈 = 现金全上
        if cost < min_cost:
            return None
        return OrderPlan("buy", cost=cost)
    amount = min(wallet.qty, -diff / price)
    if amount * price < min_cost:
        return None
    return OrderPlan("sell", amount=amount)


def simulate_fill(plan: OrderPlan, price: float, *, fee: float, slippage: float, ts: int | None = None) -> Fill:
    """模拟撮合：买贵一点、卖便宜一点（滑点），手续费按成交额扣。

    手续费口径和 Gate 现货一致：买入扣币，卖出扣钱。
    """
    if plan.side == "buy":
        px = price * (1 + slippage)
        gross = plan.cost / px
        fee_base = gross * fee
        return Fill("buy", px, gross - fee_base, plan.cost, fee_base * px, ts=ts)
    px = price * (1 - slippage)
    gross = plan.amount * px
    fee_q = gross * fee
    return Fill("sell", px, plan.amount, gross - fee_q, fee_q, ts=ts)
