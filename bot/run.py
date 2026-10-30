"""GitHub Actions 每次触发时跑这个：所有机器人各走一步（看信号 → 风控 → 该买买该卖卖 → 记账）。

单币机器人走 tick()；轮动机器人（strategy = "rotation"）走 rotation_tick()：篮子里每个币都拉 K 线排名，
换车时先把手里的全卖掉，再把现金梭进新第一名。

    python -m bot.run                    # 按 config.toml / BOT_MODE 跑全部机器人
    python -m bot.run --only btc-trend   # 只跑某几个（逗号分隔）
    python -m bot.run --dry-run          # 只看信号不下单、不存盘
    python -m bot.run --preflight        # 实盘/测试网：顺便体检 API key（绑没绑 IP、实际费率……）

实盘和测试网只能在自托管 runner（你自己的电脑/服务器）上跑，GitHub 托管的机器会直接拒绝，原因见 HOSTED_LIVE_REFUSAL。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import notify, rotation
from .brokers import LiveBroker, PaperBroker, SkipOrder, new_tag, tag_prefix
from .candles import now_ms, timeframe_ms
from .config import ROOT, BotConfig, Config, load_config
from .data import fetch_fear_greed, fetch_recent, make_exchange
from .engine import EXIT_EVENTS, Decision, apply_events, decide, plan_order, simulate_fill
from .state import append_csv, load_bot, save_bot
from .strategies import Context, get as get_strategy
from .wallet import Wallet

TRADE_HEADER = ["time", "bot", "symbol", "side", "price", "base", "quote", "fee_quote", "event", "reason", "order_id", "equity", "note"]
EQUITY_HEADER = ["time", "bot", "symbol", "price", "equity", "exposure", "signal", "target", "event"]
RECOVER_WINDOW_MS = 3 * 86_400_000
LIQUIDATE_REASON = "config 里设了 liquidate = true：卖光离场，之后不再买"
NEW_LEDGER_GRACE_MS = 6 * 3_600_000  # 新账本也认领它出生前 6 小时内的单（第一次运行的存档没推上去也能找回）
MODE_NAMES = {"paper": "模拟盘", "live": "实盘", "testnet": "测试网"}
HOSTED_LIVE_REFUSAL = """\
⛔ 实盘 / 测试网不在 GitHub 托管的机器上跑，这次什么都没做。原因：
- 这些机器在美国机房，IP 每次都变，没法绑 Gate API key 的 IP 白名单；不绑白名单的 key 90 天自动失效，失效后机器人会卡住
- Gate 用户协议把美国列为受限地区，从这些 IP 调用你的账户有被风控（冻结）的风险
做法：仓库改成私有，在你自己的电脑/服务器上装 self-hosted runner，再把仓库变量 RUNNER 设成 self-hosted。
只想看效果就把 BOT_MODE 改回 paper（模拟盘在哪跑都行）。"""


def ledger_epoch(st: dict) -> int:
    """这本账从什么时候开始负责订单：重置的账本从重置那一刻起；新账本往前宽限几小时。更早的单不是它的。"""
    return int(st.get("epoch_ms") or st.get("created_ms", 0) - NEW_LEDGER_GRACE_MS)


def recover_since(st: dict) -> int:
    return max(now_ms() - RECOVER_WINDOW_MS, ledger_epoch(st))


def apply_reset(bot: BotConfig, st: dict, wallet: Wallet, mode: str) -> tuple[Wallet, str | None]:
    """config 里的 reset 换了新值：清空账本重新开始，但保留已认领的订单号，并且只认领重置之后的单。"""
    if bot.reset is None or st.get("reset") == bot.reset:
        return wallet, None
    if mode != "paper" and wallet.in_position:
        raise RuntimeError(f"{bot.name} 要按 reset = {bot.reset!r} 重置账本，但手里还有仓位：先卖掉（或等它自己卖掉）再重置")
    st["reset"], st["epoch_ms"] = bot.reset, now_ms()
    return Wallet.new(bot.budget), f"按 reset = {bot.reset!r} 重置了账本，从 {bot.budget:.2f} 重新开始"


def log_trade(state_dir, bot_name: str, symbol: str, fill, *, event: str = "", reason: str = "", equity: float) -> None:
    append_csv(state_dir / "trades.csv", TRADE_HEADER, {
        "time": _iso(fill.ts or now_ms()), "bot": bot_name, "symbol": symbol, "side": fill.side,
        "price": f"{fill.price:.10g}", "base": f"{fill.base:.10g}", "quote": f"{fill.quote:.10g}",
        "fee_quote": f"{fill.fee_quote:.6g}", "event": event, "reason": reason,
        "order_id": fill.order_id or "", "equity": f"{equity:.4f}", "note": fill.note})


def _fill_dict(symbol: str, f) -> dict:
    return {"symbol": symbol, "side": f.side, "price": f.price, "base": f.base, "quote": f.quote, "note": f.note}


def _cell(x) -> str:
    """Markdown 表格单元格：| 和换行会把表格撑坏。"""
    return str(x).replace("|", "\\|").replace("\n", " ")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def tick(bot: BotConfig, cfg: Config, *, ex, broker, fear_greed, dry_run: bool = False) -> dict:
    """一个机器人走一步，返回给人看的报告。"""
    strat = get_strategy(bot.strategy)
    look = strat.lookback(bot.params)
    tf = timeframe_ms(bot.timeframe)
    rep = {"bot": bot.name, "symbol": bot.symbol, "strategy": strat.name, "title": strat.title,
           "timeframe": bot.timeframe, "status": "ok", "notes": [], "fill": None}

    candles = fetch_recent(ex, bot.symbol, bot.timeframe, look)
    if len(candles) < look:
        rep.update(status="skip", reason=f"K 线不够：需要 {look} 根已收盘 K 线，只拿到 {len(candles)} 根（刚上市？）")
        return rep
    if "fear_greed" in strat.needs and fear_greed is None:
        rep.update(status="error", reason="恐惧贪婪指数没拿到，这个机器人这次跳过")
        return rep

    try:
        t = ex.fetch_ticker(bot.symbol)
        price = float(t.get("last") or t.get("close") or candles[-1].close)
    except Exception as e:
        price = candles[-1].close
        rep["notes"].append(f"拿实时价失败，用最近收盘价：{e}")

    state_dir = cfg.state_dir(broker.mode)
    st = load_bot(state_dir, bot.name, bot.budget)
    wallet, msg = apply_reset(bot, st, Wallet.from_dict(st["wallet"]), broker.mode)
    if msg:
        rep["notes"].append(msg)
    if st.get("symbol") and st["symbol"] != bot.symbol:
        if wallet.in_position:
            raise RuntimeError(f"{bot.name} 换了交易对（{st['symbol']} → {bot.symbol}）但旧仓位还没清，先清仓或者给机器人改个名字")
        wallet.qty = 0.0  # 旧币卖剩的零头留在账户里，不再算这个机器人的
    if msg := wallet.rebudget(bot.budget, wallet.equity(price)):
        rep["notes"].append(msg)
    recovered: list = []
    rep["notes"] += broker.sync(wallet, bot.symbol, price, tag_prefix(bot.name), st["orders"], recover_since(st), recovered)
    if recovered and not dry_run:
        for sym, f in recovered:
            log_trade(state_dir, bot.name, sym, f, reason="对账找回", equity=wallet.equity(price))
    rep["recovered"] = [_fill_dict(sym, f) for sym, f in recovered]
    min_cost = broker.min_cost(bot.symbol, price)
    if wallet.settle_dust(price, min_cost):
        rep["notes"].append(f"手里只剩 {wallet.qty:.8g}（低于最小下单额，卖不掉），当作空仓")

    wallet.mark(price)
    exposure_before = wallet.exposure(price)
    ctx = Context(timeframe=bot.timeframe, asof_ms=candles[-1].ts + tf, symbol=bot.symbol, fear_greed=fear_greed)
    d = decide(strat, bot.params, candles, wallet, price, size=bot.size, risk=bot.risk, ctx=ctx)
    if bot.liquidate and d.event not in ("kill_switch", "halted"):
        d = Decision(0.0, d.signal, "liquidate", LIQUIDATE_REASON)
    apply_events(wallet, d)
    slippage = bot.slippage if bot.slippage is not None else cfg.slippage
    plan = plan_order(d.target, wallet, price, threshold=cfg.rebalance_threshold, min_cost=min_cost)

    def persist():
        st.update(symbol=bot.symbol, strategy=strat.name, timeframe=bot.timeframe, mode=broker.mode, wallet=wallet.to_dict())
        save_bot(state_dir, bot.name, st)

    fill = None
    if plan and not dry_run:
        if d.event:
            persist()  # 止损/熔断这类决定先落盘：万一下单结果不确定，冷静期和熔断状态也不会丢
        try:
            fill = broker.execute(plan, bot.symbol, price, slippage=slippage,
                                  tag=new_tag(bot.name, exit=d.event in EXIT_EVENTS))
        except SkipOrder as e:
            rep["notes"].append(str(e))
        if fill:
            wallet.apply(fill, flat_below=min_cost)
            if fill.order_id:
                st["orders"].append(fill.order_id)
            persist()  # 成交后第一时间存盘，后面就算崩了下次也能对上账
            log_trade(state_dir, bot.name, bot.symbol, fill, event=d.event or "", reason=d.reason, equity=wallet.equity(price))
            if fill.note:
                rep["notes"].append(fill.note)

    equity = wallet.equity(price)
    rep.update(
        price=price, signal=d.signal, target=d.target, event=d.event, reason=d.reason,
        exposure_before=exposure_before, exposure=wallet.exposure(price), equity=equity,
        pnl=equity / wallet.budget - 1 if wallet.budget else 0.0, budget=wallet.budget,
        plan=None if plan is None else {"side": plan.side, "cost": plan.cost, "amount": plan.amount},
        fill=_fill_dict(bot.symbol, fill) if fill else (rep["recovered"][-1] if rep["recovered"] else None),
        candle_close=_iso(candles[-1].ts + tf), halted=wallet.halted, exit_lock=wallet.exit_lock,
    )
    if dry_run:
        return rep

    st["runs"] = st.get("runs", 0) + 1
    st["last"] = {k: rep[k] for k in ("price", "signal", "target", "event", "reason", "equity", "candle_close")}
    persist()
    append_csv(state_dir / "equity.csv", EQUITY_HEADER, {
        "time": _iso(now_ms()), "bot": bot.name, "symbol": bot.symbol, "price": f"{price:.10g}",
        "equity": f"{equity:.4f}", "exposure": f"{wallet.exposure(price):.4f}",
        "signal": "" if d.signal is None else f"{d.signal:.4f}", "target": f"{d.target:.4f}", "event": d.event or ""})
    return rep


def sync_rotation(broker, wallet: Wallet, symbols, price_of, prefix: str, known: list, since_ms: int,
                  recovered: list | None = None) -> list[str]:
    """轮动机器人开跑前对账：篮子里每个币都对一遍，先对手里那个（漏记的卖单先补上）。

    broker.sync 只认“qty 就是这个币的数量”的单币钱包，所以没拿着的币用一个 qty=0 的影子钱包去对：
    找回的漏记订单把现金变化记回主钱包；找回的买单如果币还在，它就是现在的持仓。
    拿不到价格的币也照样对（价格只用来判断是不是零头，拿不到就按找回订单的成本价估）。
    """
    notes: list[str] = []
    held = wallet.holding
    for sym in ([held] if held else []) + [s for s in symbols if s != held]:
        px = price_of(sym)
        if sym == wallet.holding:
            notes += broker.sync(wallet, sym, px or 0.0, prefix, known, since_ms, recovered)
            continue
        shadow = Wallet.from_dict({**wallet.to_dict(), "qty": 0.0, "holding": sym, "entry_price": None,
                                   "high_water": None, "entry_equity": None})
        try:
            notes += broker.sync(shadow, sym, px or 0.0, prefix, known, since_ms, recovered)
        except Exception as e:
            if type(e).__name__ != "BadSymbol":
                raise
            notes.append(f"交易所里没有 {sym}（写错了或者下架了？），跳过对账")  # 没拿着、交易所也没有：没什么可找回的
            continue
        if shadow.fills == wallet.fills:
            continue
        wallet.cash, wallet.fees_paid, wallet.realized_pnl, wallet.fills = shadow.cash, shadow.fees_paid, shadow.realized_pnl, shadow.fills
        if shadow.qty * (px or shadow.entry_price or 0.0) >= wallet.dust_value():
            hpx = price_of(wallet.holding) or 0.0 if wallet.holding else 0.0
            # 手里那个币只剩卖不掉的零头（低于最小下单额，换车时本来就会记作损耗）就不算冲突
            floor = max(wallet.dust_value(), broker.min_cost(wallet.holding, hpx) if wallet.holding and hpx else 0.0)
            if wallet.holding is not None and wallet.qty * hpx >= floor:
                raise RuntimeError(f"账本对不上：记着拿 {wallet.holding}，又找回了 {sym} 的买单。请对照交易所订单人工修 state")
            wallet.qty, wallet.holding = shadow.qty, sym
            wallet.entry_price, wallet.high_water, wallet.entry_equity = shadow.entry_price, shadow.high_water, shadow.entry_equity
    return notes


def rotation_tick(bot: BotConfig, cfg: Config, *, ex, broker, dry_run: bool = False) -> dict:
    """轮动机器人走一步：篮子里每个币拉 K 线 → 排名 → 换车（先卖后买）→ 记账，返回给人看的报告。"""
    rot = rotation.get(bot.strategy)
    look = rot.lookback(bot.params)
    tf = timeframe_ms(bot.timeframe)
    universe = list(bot.symbols)
    rep = {"bot": bot.name, "kind": "rotation", "symbol": bot.label, "symbols": universe, "quote": bot.quote,
           "strategy": rot.name, "title": rot.title, "timeframe": bot.timeframe, "status": "ok", "notes": [],
           "fill": None, "fills": [], "dry_run": dry_run}

    state_dir = cfg.state_dir(broker.mode)
    st = load_bot(state_dir, bot.name, bot.budget)
    wallet = Wallet.from_dict(st["wallet"])
    if wallet.holding is None and wallet.qty > 0 and st.get("symbol"):
        wallet.holding = st["symbol"]  # 以前是单币机器人：接手它的仓位，下面会按轮动规则处理
    held = wallet.holding

    windows, short = {}, []
    for sym in dict.fromkeys(universe + ([held] if held else [])):
        try:
            c = fetch_recent(ex, sym, bot.timeframe, look)
        except Exception as e:
            rep["notes"].append(f"{sym} K 线没拿到：{type(e).__name__}: {e}")
            continue
        if len(c) >= look:
            windows[sym] = c
        else:
            short.append(sym)
    if short:
        rep["notes"].append(f"历史不够 {look} 根、这次不参与排名：{', '.join(short)}")
    windows, stale = rotation.align(windows)
    if stale:
        rep["notes"].append(f"K 线没更新到最新、这次不参与排名：{', '.join(stale)}")
    pool = {s: w for s, w in windows.items() if s in universe}
    prices = {s: w[-1].close for s, w in windows.items()}

    def live_price(sym: str) -> float | None:
        """实时价（顺手记进 prices）；拿不到就用最近收盘价，都没有返回 None。"""
        try:
            t = ex.fetch_ticker(sym)
            px = float(t.get("last") or t.get("close") or 0.0)
            if px > 0:
                prices[sym] = px
                return px
            raise ValueError("ticker 里没有价格")
        except Exception as e:
            if sym in prices:
                rep["notes"].append(f"拿 {sym} 实时价失败，用最近收盘价：{e}")
                return prices[sym]
            return None

    def persist():
        st.update(symbol=wallet.holding or "", symbols=universe, strategy=rot.name, timeframe=bot.timeframe,
                  mode=broker.mode, wallet=wallet.to_dict())
        save_bot(state_dir, bot.name, st)

    if held and live_price(held) is None:
        rep.update(status="skip", reason=f"手里的 {held} 拿不到价格，这次不动")
        return rep
    wallet, msg = apply_reset(bot, st, wallet, broker.mode)
    if msg:
        rep["notes"].append(msg)
        held = wallet.holding
    before = (wallet.to_dict(), list(st["orders"]))
    if msg := wallet.rebudget(bot.budget, wallet.cash + (wallet.qty * prices[held] if held and held in prices else 0.0)):
        rep["notes"].append(msg)
    recovered: list = []
    rep["notes"] += sync_rotation(broker, wallet, universe, lambda s: prices.get(s) or live_price(s),
                                  tag_prefix(bot.name), st["orders"], recover_since(st), recovered)
    if recovered and not dry_run:
        for sym, f in recovered:
            log_trade(state_dir, bot.name, sym, f, reason="对账找回", equity=wallet.cash + wallet.qty * (prices.get(wallet.holding) or f.price))
    rep["recovered"] = [_fill_dict(sym, f) for sym, f in recovered]
    if wallet.holding != held:  # 对账时找回了漏记的买单
        held = wallet.holding
        if held and live_price(held) is None:
            if not dry_run:
                persist()
            rep.update(status="skip", reason=f"对账找回了 {held} 的持仓，但拿不到它的价格，这次先不动")
            return rep
    if held and wallet.qty * prices[held] < wallet.dust_value():
        rotation.flatten(wallet)  # 只剩零头 = 空仓
        held = None
    if held in universe and held not in pool:
        if not dry_run and (wallet.to_dict(), st["orders"]) != before:
            persist()  # 对账找回了订单 / 改了预算：先存上，别丢
        rep.update(status="skip", reason=f"手里的 {held} 这次没拿到最新 K 线，不知道该不该换，先不动")
        return rep

    price = prices[held] if held else None
    if held:
        wallet.mark(price)
    exposure_before = wallet.exposure(price) if held else 0.0
    last_ts = max((w[-1].ts for w in pool.values()), default=None)
    ctx = Context(timeframe=bot.timeframe, asof_ms=last_ts + tf if last_ts is not None else 0)
    d = rotation.decide(rot, bot.params, pool, wallet, price, risk=bot.risk, ctx=ctx)
    if bot.liquidate and d.event not in ("kill_switch", "halted"):
        d = rotation.RotationDecision(None, "liquidate", LIQUIDATE_REASON)
    if d.event == "kill_switch":
        wallet.halted = True
    if d.target and d.target != held:
        live_price(d.target)  # 买入按实时价估（拿不到就用收盘价）

    slippage = bot.slippage if bot.slippage is not None else cfg.slippage
    planned: list[dict] = []
    tag_ms = [0]

    def fill(sym, plan, px):
        planned.append({"symbol": sym, "side": plan.side, "cost": plan.cost, "amount": plan.amount})
        if dry_run:
            return simulate_fill(plan, px, fee=cfg.fee, slippage=slippage, ts=now_ms())
        tag_ms[0] = max(now_ms(), tag_ms[0] + 1)  # 一次换车两笔单，备注别撞
        try:
            return broker.execute(plan, sym, px, slippage=slippage, tag=new_tag(bot.name, tag_ms[0]))
        except SkipOrder as e:
            rep["notes"].append(f"{sym}：{e}")
            return None

    def on_fill(sym, f, pnl):
        if dry_run:
            return
        if f.order_id:
            st["orders"].append(f.order_id)
        persist()  # 成交后第一时间存盘，后面就算崩了下次也能对上账
        append_csv(state_dir / "trades.csv", TRADE_HEADER, {
            "time": _iso(f.ts or now_ms()), "bot": bot.name, "symbol": sym, "side": f.side,
            "price": f"{f.price:.10g}", "base": f"{f.base:.10g}", "quote": f"{f.quote:.10g}",
            "fee_quote": f"{f.fee_quote:.6g}", "event": d.event or "", "reason": d.reason,
            "order_id": f.order_id or "", "equity": f"{wallet.cash + wallet.qty * prices.get(sym, f.price):.4f}",
            "note": f.note})
        if f.note:
            rep["notes"].append(f"{sym}：{f.note}")
        rep["fills"].append({"symbol": sym, "side": f.side, "price": f.price, "base": f.base, "quote": f.quote})

    work = Wallet.from_dict(wallet.to_dict()) if dry_run else wallet  # dry-run 在副本上演一遍，看看会下什么单
    rep["notes"] += rotation.rebalance(work, d.target, prices, size=bot.size, threshold=cfg.rebalance_threshold,
                                       min_cost=broker.min_cost, fill=fill, on_fill=on_fill)

    held = wallet.holding
    price = prices[held] if held else None
    equity = wallet.cash + (wallet.qty * price if held else 0.0)
    rep.update(
        symbol=held or "", holding=held, price=price, target=d.target, event=d.event, reason=d.reason,
        exposure_before=exposure_before, exposure=wallet.exposure(price) if held else 0.0, equity=equity,
        pnl=equity / wallet.budget - 1 if wallet.budget else 0.0, budget=wallet.budget, plan=planned or None,
        fill=rep["fills"][-1] if rep["fills"] else None,
        candle_close=_iso(last_ts + tf) if last_ts is not None else "", halted=wallet.halted,
    )
    if dry_run:
        return rep

    st["runs"] = st.get("runs", 0) + 1
    st["last"] = {k: rep[k] for k in ("holding", "price", "target", "event", "reason", "equity", "candle_close")}
    persist()
    append_csv(state_dir / "equity.csv", EQUITY_HEADER, {
        "time": _iso(now_ms()), "bot": bot.name, "symbol": held or "", "price": "" if price is None else f"{price:.10g}",
        "equity": f"{equity:.4f}", "exposure": f"{rep['exposure']:.4f}", "signal": d.target or "",
        "target": f"{bot.size if d.target else 0.0:.4f}", "event": d.event or ""})
    return rep


def describe_action(rep: dict, quote: str) -> str:
    f = rep.get("fill")
    if f:
        if f["side"] == "buy":
            return f"🟢 买入 {f['quote']:.2f} {quote} @ {f['price']:.6g}"
        return f"🔴 卖出 {f['base']:.6g} @ {f['price']:.6g}"
    if rep.get("plan") and rep["status"] == "ok":
        return "⏸ 想下单但没下成（见备注）" if rep["notes"] else "👀 只看不动（dry-run）"
    return "💎 持有" if rep.get("exposure", 0) > 0.01 else "🍵 空仓观望"


def describe_rotation(rep: dict) -> str:
    quote = rep.get("quote", "USDT")
    if rep.get("fills"):
        return " → ".join(
            f"🟢 买入 {rotation.base(f['symbol'])} {f['quote']:.2f} {quote} @ {f['price']:.6g}" if f["side"] == "buy"
            else f"🔴 卖出 {f['base']:.6g} {rotation.base(f['symbol'])} @ {f['price']:.6g}" for f in rep["fills"])
    if rep.get("plan"):
        if rep.get("dry_run"):
            return "👀 只看不动（dry-run）：" + " → ".join(
                f"买 {rotation.base(p['symbol'])} {p['cost']:.2f} {quote}" if p["side"] == "buy"
                else f"卖 {p['amount']:.6g} {rotation.base(p['symbol'])}" for p in rep["plan"])
        return "⏸ 想下单但没下成（见备注）"
    return f"💎 持有 {rotation.base(rep['holding'])}" if rep.get("holding") else "🍵 空仓观望"


def render(reports: list[dict], mode: str) -> str:
    lines = [f"## 🎰 梭哈机器人 · {MODE_NAMES.get(mode, mode)} · {_iso(now_ms())} UTC", "",
             "| 机器人 | 标的 | 策略 | 动作 | 价格 | 净值 | 收益 | 说明 |", "|---|---|---|---|---|---|---|---|"]
    for r in reports:
        quote = r["symbol"].split("/")[-1]
        if r["status"] != "ok":
            lines.append(f"| {r['bot']} | {r['symbol']} | {_cell(r.get('title', ''))} | ⚠️ {r['status']} | | | | {_cell(r.get('reason', ''))} |")
            continue
        why = _cell(r.get("reason") or "")
        if r.get("event"):
            why = f"**{r['event']}** {why}"
        if r.get("kind") == "rotation":
            quote = r.get("quote", quote)
            lines.append(
                f"| {r['bot']} | {r.get('holding') or quote} {r['timeframe']}（{len(r['symbols'])} 币轮动） | {r['title']} "
                f"| {describe_rotation(r)} | {'-' if r.get('price') is None else format(r['price'], '.6g')} "
                f"| {r['equity']:.2f} {quote} | {r['pnl']:+.1%} | {why} |")
            continue
        lines.append(
            f"| {r['bot']} | {r['symbol']} {r['timeframe']} | {r['title']} | {describe_action(r, quote)} | {r['price']:.6g} "
            f"| {r['equity']:.2f} {quote} | {r['pnl']:+.1%} | {why} |")
    notes = [(r["bot"], n) for r in reports for n in r.get("notes", [])]
    if notes:
        lines += ["", "**备注**", *[f"- `{b}` {n}" for b, n in notes]]
    return "\n".join(lines) + "\n"


def utf8_stdout() -> None:
    """中文 + emoji 的输出在非 UTF-8 控制台（Windows 默认 GBK）上别崩。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="梭哈机器人：跑一步")
    ap.add_argument("--config")
    ap.add_argument("--only", help="只跑这些机器人（逗号分隔）")
    ap.add_argument("--mode", choices=sorted(MODE_NAMES), help="覆盖 config / BOT_MODE")
    ap.add_argument("--dry-run", action="store_true", help="只看信号，不下单不存盘")
    ap.add_argument("--preflight", action="store_true", help="实盘/测试网：体检 API key 和费率（手动触发、dry-run 时自动做）")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    if args.mode:
        env["BOT_MODE"] = args.mode
    cfg = load_config(args.config, env)
    bots = [b for b in cfg.bots if b.enabled]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        missing = wanted - {b.name for b in cfg.bots}
        if missing:
            ap.error(f"config.toml 里没有这些机器人: {sorted(missing)}")
        bots = [b for b in cfg.bots if b.name in wanted]
    if cfg.mode != "paper":
        skipped = [b.name for b in bots if b.paper_only]
        if skipped:
            print(f"{MODE_NAMES[cfg.mode]}模式跳过只在模拟盘跑的机器人：{', '.join(skipped)}")
        bots = [b for b in bots if not b.paper_only]
    if not bots:
        print("没有启用的机器人，下班。")
        return 0

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if cfg.mode != "paper" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted":
        print(HOSTED_LIVE_REFUSAL, file=sys.stderr)
        if summary:
            with open(summary, "a") as f:
                f.write(HOSTED_LIVE_REFUSAL.replace("\n", "\n\n") + "\n")
        return 2

    ex = make_exchange(cfg.exchange)
    if cfg.mode == "paper":
        broker = PaperBroker(ex, fee=cfg.fee, max_order=cfg.max_order)
    else:
        testnet = cfg.mode == "testnet"
        broker = LiveBroker(make_exchange(cfg.exchange, auth=True, testnet=testnet), max_order=cfg.max_order, testnet=testnet)

    checkup = []
    if broker.mode != "paper" and (args.preflight or args.dry_run or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"):
        symbols = sorted({s for b in bots for s in (b.symbols or (b.symbol,))})
        try:
            checkup = broker.preflight(symbols)
        except Exception as e:
            checkup = [f"体检失败：{type(e).__name__}: {e}"]

    fear_greed = None
    if any(not b.is_rotation and "fear_greed" in get_strategy(b.strategy).needs for b in bots):
        try:
            fear_greed = fetch_fear_greed(limit=400, fallback_csv=Path(os.environ.get("KURUMI_DATA_DIR") or ROOT / "data") / "fear_greed.csv")
        except Exception as e:
            print(f"恐惧贪婪指数获取失败：{e}", file=sys.stderr)

    reports = []
    for bot in bots:
        try:
            if bot.is_rotation:
                reports.append(rotation_tick(bot, cfg, ex=ex, broker=broker, dry_run=args.dry_run))
            else:
                reports.append(tick(bot, cfg, ex=ex, broker=broker, fear_greed=fear_greed, dry_run=args.dry_run))
        except Exception as e:
            traceback.print_exc()
            reports.append({"bot": bot.name, "symbol": bot.label, "title": bot.strategy, "status": "error",
                            "reason": f"{type(e).__name__}: {e}", "notes": []})

    text = render(reports, broker.mode)
    if checkup:
        text += "\n**API key 体检**\n" + "".join(f"- {n}\n" for n in checkup)
    print(text)
    if summary:
        with open(summary, "a") as f:
            f.write(text)

    noteworthy = [r for r in reports if r.get("fill") or r.get("recovered")
                  or r.get("event") in ("stop_loss", "take_profit", "trailing_stop", "kill_switch") or r["status"] == "error"]
    if noteworthy and not args.dry_run:
        for line in notify.send("梭哈机器人有动静", text, cfg.notify):
            print(line)
    return 1 if any(r["status"] == "error" for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
