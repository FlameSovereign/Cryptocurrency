"""机器人的虚拟钱包。

每个机器人只认自己这本账：预算多少、手里多少币、多少现金。实盘时下单金额同时受这本账和
交易所真实余额限制，所以就算账户里还有别的钱，机器人也最多亏掉自己的预算。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass
class Fill:
    """一笔成交（不管是真成交还是模拟的）。"""

    side: str  # "buy" | "sell"
    price: float  # 成交均价
    base: float  # 币的变化量（绝对值）：买入时是到手的币（已扣手续费），卖出时是卖掉的币
    quote: float  # 钱的变化量（绝对值）：买入时是花掉的钱（含手续费），卖出时是到手的钱（已扣手续费）
    fee_quote: float = 0.0  # 手续费折合成计价货币
    ts: int | None = None
    order_id: str | None = None
    note: str = ""


@dataclass
class Wallet:
    budget: float
    cash: float
    qty: float = 0.0
    entry_price: float | None = None  # 持仓均价（含手续费）；None 表示空仓
    high_water: float | None = None  # 持仓以来的最高价，给移动止损用
    entry_equity: float | None = None  # 开仓前的净值，用来算这一轮赚了还是亏了
    holding: str | None = None  # 当前持有的交易对（轮动策略会换）
    halted: bool = False  # 熔断后罢工
    exit_lock: bool = False  # 止损/止盈后进入冷静期，等策略信号先归零才允许再上车
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    fills: int = 0

    @classmethod
    def new(cls, budget: float) -> "Wallet":
        return cls(budget=float(budget), cash=float(budget))

    # ---- 估值 ----
    def equity(self, price: float) -> float:
        return self.cash + self.qty * price

    def exposure(self, price: float) -> float:
        eq = self.equity(price)
        return self.qty * price / eq if eq > 0 else 0.0

    @property
    def in_position(self) -> bool:
        return self.entry_price is not None

    def dust_value(self) -> float:
        """低于这个价值的零头当作空仓（精度截断留下的碎币）。"""
        return max(0.01, self.budget * 0.001)

    def mark(self, price: float) -> None:
        """用最新价格更新持仓最高价。"""
        if self.in_position:
            self.high_water = price if self.high_water is None else max(self.high_water, price)

    # ---- 记账 ----
    def apply(self, fill: Fill) -> float | None:
        """记一笔成交。如果这笔成交让仓位回到空仓，返回这一轮的盈亏（按净值算，含手续费）。"""
        closed_pnl = None
        if fill.side == "buy":
            if not self.in_position:
                self.entry_equity = self.cash + self.qty * fill.price
                self.high_water = fill.price
            new_qty = self.qty + fill.base
            basis = (self.entry_price or 0.0) * self.qty + fill.quote
            self.entry_price = basis / new_qty if new_qty > 0 else None
            self.qty = new_qty
            self.cash -= fill.quote
        elif fill.side == "sell":
            sold = min(fill.base, self.qty)
            if self.entry_price is not None:
                self.realized_pnl += fill.quote - self.entry_price * sold
            self.qty -= sold
            self.cash += fill.quote
            if self.qty * fill.price < self.dust_value():
                if self.entry_equity is not None:
                    closed_pnl = self.equity(fill.price) - self.entry_equity
                self.entry_price = self.high_water = self.entry_equity = None
        else:
            raise ValueError(f"未知方向 {fill.side!r}")
        # 浮点误差别让余额变成 -0.0000000001
        if -1e-9 < self.cash < 0:
            self.cash = 0.0
        if -1e-12 < self.qty < 0:
            self.qty = 0.0
        self.fees_paid += fill.fee_quote
        self.fills += 1
        return closed_pnl

    def rebudget(self, new_budget: float) -> str | None:
        """配置里改了预算：加预算就补现金，减预算就从现金里扣（扣不够就能扣多少扣多少）。"""
        delta = float(new_budget) - self.budget
        if abs(delta) < 1e-9:
            return None
        if delta > 0:
            self.cash += delta
            self.budget += delta
            return f"追加预算 {delta:.2f}"
        take = min(-delta, self.cash)
        self.cash -= take
        self.budget -= take
        if take < -delta - 1e-9:
            return f"想减预算 {-delta:.2f}，但现金只够扣 {take:.2f}（剩下的在币里，卖出后再改）"
        return f"减少预算 {take:.2f}"

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
