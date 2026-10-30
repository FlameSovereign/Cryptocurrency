"""GitHub Actions 每次触发时跑这个：所有机器人各走一步（看信号 → 风控 → 该买买该卖卖 → 记账）。

    python -m bot.run                    # 按 config.toml / BOT_MODE 跑全部机器人
    python -m bot.run --only btc-trend   # 只跑某几个（逗号分隔）
    python -m bot.run --dry-run          # 只看信号不下单、不存盘
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone

from . import notify
from .brokers import LiveBroker, PaperBroker, SkipOrder, new_tag, tag_prefix
from .candles import now_ms, timeframe_ms
from .config import BotConfig, Config, load_config
from .data import fetch_fear_greed, fetch_recent, make_exchange
from .engine import apply_events, decide, plan_order
from .state import append_csv, load_bot, save_bot
from .strategies import Context, get as get_strategy
from .wallet import Wallet

TRADE_HEADER = ["time", "bot", "symbol", "side", "price", "base", "quote", "fee_quote", "event", "reason", "order_id", "equity"]
EQUITY_HEADER = ["time", "bot", "symbol", "price", "equity", "exposure", "signal", "target", "event"]
RECOVER_WINDOW_MS = 3 * 86_400_000


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
    wallet = Wallet.from_dict(st["wallet"])
    if st.get("symbol") and st["symbol"] != bot.symbol and wallet.qty * price >= wallet.dust_value():
        raise RuntimeError(f"{bot.name} 换了交易对（{st['symbol']} → {bot.symbol}）但旧仓位还没清，先清仓或者给机器人改个名字")
    if msg := wallet.rebudget(bot.budget):
        rep["notes"].append(msg)
    rep["notes"] += broker.sync(wallet, bot.symbol, price, tag_prefix(bot.name), st["orders"], now_ms() - RECOVER_WINDOW_MS)

    wallet.mark(price)
    exposure_before = wallet.exposure(price)
    ctx = Context(timeframe=bot.timeframe, asof_ms=candles[-1].ts + tf, symbol=bot.symbol, fear_greed=fear_greed)
    d = decide(strat, bot.params, candles, wallet, price, size=bot.size, risk=bot.risk, ctx=ctx)
    apply_events(wallet, d)
    slippage = bot.slippage if bot.slippage is not None else cfg.slippage
    plan = plan_order(d.target, wallet, price, threshold=cfg.rebalance_threshold, min_cost=broker.min_cost(bot.symbol, price))

    def persist():
        st.update(symbol=bot.symbol, strategy=strat.name, timeframe=bot.timeframe, mode=broker.mode, wallet=wallet.to_dict())
        save_bot(state_dir, bot.name, st)

    fill = None
    if plan and not dry_run:
        try:
            fill = broker.execute(plan, bot.symbol, price, slippage=slippage, tag=new_tag(bot.name))
        except SkipOrder as e:
            rep["notes"].append(str(e))
        if fill:
            wallet.apply(fill)
            if fill.order_id:
                st["orders"].append(fill.order_id)
            persist()  # 成交后第一时间存盘，后面就算崩了下次也能对上账
            append_csv(state_dir / "trades.csv", TRADE_HEADER, {
                "time": _iso(fill.ts or now_ms()), "bot": bot.name, "symbol": bot.symbol, "side": fill.side,
                "price": f"{fill.price:.10g}", "base": f"{fill.base:.10g}", "quote": f"{fill.quote:.10g}",
                "fee_quote": f"{fill.fee_quote:.6g}", "event": d.event or "", "reason": d.reason,
                "order_id": fill.order_id or "", "equity": f"{wallet.equity(price):.4f}"})

    equity = wallet.equity(price)
    rep.update(
        price=price, signal=d.signal, target=d.target, event=d.event, reason=d.reason,
        exposure_before=exposure_before, exposure=wallet.exposure(price), equity=equity,
        pnl=equity / wallet.budget - 1 if wallet.budget else 0.0, budget=wallet.budget,
        plan=None if plan is None else {"side": plan.side, "cost": plan.cost, "amount": plan.amount},
        fill=None if fill is None else {"side": fill.side, "price": fill.price, "base": fill.base, "quote": fill.quote},
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


def describe_action(rep: dict, quote: str) -> str:
    f = rep.get("fill")
    if f:
        if f["side"] == "buy":
            return f"🟢 买入 {f['quote']:.2f} {quote} @ {f['price']:.6g}"
        return f"🔴 卖出 {f['base']:.6g} @ {f['price']:.6g}"
    if rep.get("plan") and rep["status"] == "ok":
        return "⏸ 想下单但没下成（见备注）" if rep["notes"] else "👀 只看不动（dry-run）"
    return "💎 持有" if rep.get("exposure", 0) > 0.01 else "🍵 空仓观望"


def render(reports: list[dict], mode: str) -> str:
    lines = [f"## 🎰 梭哈机器人 · {'实盘' if mode == 'live' else '模拟盘'} · {_iso(now_ms())} UTC", "",
             "| 机器人 | 标的 | 策略 | 动作 | 价格 | 净值 | 收益 | 说明 |", "|---|---|---|---|---|---|---|---|"]
    for r in reports:
        quote = r["symbol"].split("/")[-1]
        if r["status"] != "ok":
            lines.append(f"| {r['bot']} | {r['symbol']} | {r.get('title', '')} | ⚠️ {r['status']} | | | | {r.get('reason', '')} |")
            continue
        why = r.get("reason") or ""
        if r.get("event"):
            why = f"**{r['event']}** {why}"
        lines.append(
            f"| {r['bot']} | {r['symbol']} {r['timeframe']} | {r['title']} | {describe_action(r, quote)} | {r['price']:.6g} "
            f"| {r['equity']:.2f} {quote} | {r['pnl']:+.1%} | {why} |")
    notes = [(r["bot"], n) for r in reports for n in r.get("notes", [])]
    if notes:
        lines += ["", "**备注**", *[f"- `{b}` {n}" for b, n in notes]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="梭哈机器人：跑一步")
    ap.add_argument("--config")
    ap.add_argument("--only", help="只跑这些机器人（逗号分隔）")
    ap.add_argument("--mode", choices=["paper", "live"], help="覆盖 config / BOT_MODE")
    ap.add_argument("--dry-run", action="store_true", help="只看信号，不下单不存盘")
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
    if not bots:
        print("没有启用的机器人，下班。")
        return 0

    ex = make_exchange(cfg.exchange)
    if cfg.mode == "live":
        broker = LiveBroker(make_exchange(cfg.exchange, auth=True), max_order=cfg.max_order)
    else:
        broker = PaperBroker(ex, fee=cfg.fee)

    fear_greed = None
    if any("fear_greed" in get_strategy(b.strategy).needs for b in bots):
        try:
            fear_greed = fetch_fear_greed(limit=400)
        except Exception as e:
            print(f"恐惧贪婪指数获取失败：{e}", file=sys.stderr)

    reports = []
    for bot in bots:
        try:
            reports.append(tick(bot, cfg, ex=ex, broker=broker, fear_greed=fear_greed, dry_run=args.dry_run))
        except Exception as e:
            traceback.print_exc()
            reports.append({"bot": bot.name, "symbol": bot.symbol, "title": bot.strategy, "status": "error",
                            "reason": f"{type(e).__name__}: {e}", "notes": []})

    text = render(reports, broker.mode)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text)

    noteworthy = [r for r in reports if r.get("fill") or r.get("event") in ("stop_loss", "take_profit", "trailing_stop", "kill_switch")
                  or r["status"] == "error"]
    if noteworthy and not args.dry_run:
        for line in notify.send("梭哈机器人有动静", text, cfg.notify):
            print(line)
    return 1 if any(r["status"] == "error" for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
