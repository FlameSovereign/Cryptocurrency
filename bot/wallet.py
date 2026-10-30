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
    def apply(self, fill: Fill, flat_below: float = 0.0) -> float | None:
        """记一笔成交。如果这笔成交让仓位回到空仓，返回这一轮的盈亏（按净值算，含手续费）。

        flat_below：交易所最小下单额。卖完剩下的零头低于它就卖不掉了，当作空仓（不然零头会触发止损、卡住冷静期）。
        """
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
            # 只给账本里真有的那部分记钱：账本没有的币（比如重置后对账翻出的旧卖单）不能凭空变出现金
            got = fill.quote * (sold / fill.base) if fill.base > 0 else 0.0
            if self.entry_price is not None:
                self.realized_pnl += got - self.entry_price * sold
            self.qty -= sold
            self.cash += got
            if self.qty * fill.price < max(self.dust_value(), flat_below):
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

    def settle_dust(self, price: float, flat_below: float = 0.0) -> bool:
        """手里只剩卖不掉的零头（低于最小下单额）就当空仓。返回有没有改动。"""
        if self.in_position and self.qty * price < max(self.dust_value(), flat_below):
            self.entry_price = self.high_water = self.entry_equity = None
            return True
        return False

    def rebudget(self, new_budget: float, equity: float | None = None) -> str | None:
        """配置里改了预算。

        加预算：补现金。减预算：按比例从净值里抽走——预算砍一半就抽走一半净值，收益率和离熔断线的距离都不变
        （不然一个亏了 30% 的机器人砍一半预算，会直接撞上熔断线）。现金不够抽就先抽一部分，卖出后下次再抽。
        equity 是当前净值，不传就当只有现金。
        """
        new_budget = float(new_budget)
        delta = new_budget - self.budget
        if abs(delta) < 1e-9:
            return None
        if delta > 0:
            self.cash += delta
            self.budget += delta
            return f"追加预算 {delta:.2f}"
        eq = self.cash if equity is None else float(equity)
        if eq <= 0 or self.budget <= 0:
            self.budget = new_budget
            return f"预算改成 {new_budget:.2f}"
        want = eq * (-delta / self.budget)
        take = min(want, self.cash)
        self.cash -= take
        if take >= want - 1e-9:
            self.budget = new_budget
            return f"预算减到 {new_budget:.2f}（按比例抽走 {take:.2f}）"
        self.budget -= take / eq * self.budget
        return f"想把预算减到 {new_budget:.2f}，现金只够按比例抽走 {take:.2f}（剩下的在币里，卖出后再抽）"

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
