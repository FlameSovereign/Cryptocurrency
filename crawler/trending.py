"""CoinGecko 热搜榜（按过去 24 小时搜索量排的币）：每次记一份前 N 名快照。

免费接口，不用 key，限频很松；数据来源请注明 CoinGecko。
"""
from __future__ import annotations

from .common import Ctx, append_csv, num, pct, to_float

NAME = "trending"
TITLE = "CoinGecko 热搜"
FILE = "trending.csv"
HEADER = ["time_utc", "rank", "id", "symbol", "name", "market_cap_rank", "price_usd", "change_24h_pct"]
SNAPSHOT = True
DEFAULTS = {"top_n": 10}
URL = "https://api.coingecko.com/api/v3/search/trending"


def parse(payload: dict, time_utc: str, top_n: int) -> list[dict]:
    """接口返回 → CSV 行。字段缺了就留空，不报错。"""
    rows: list[dict] = []
    for c in payload.get("coins") or []:
        if len(rows) >= top_n:
            break
        it = (c or {}).get("item") or {}
        if not it.get("id"):
            continue
        data = it.get("data") or {}
        change = data.get("price_change_percentage_24h")
        if isinstance(change, dict):  # 现在是按币种分的 {"usd": 1.2, "cny": ...}
            change = change.get("usd")
        rank = to_float(it.get("market_cap_rank"))
        rows.append({
            "time_utc": time_utc,
            "rank": len(rows) + 1,
            "id": it.get("id"),
            "symbol": str(it.get("symbol") or "").upper(),
            "name": it.get("name") or "",
            "market_cap_rank": "" if rank is None else int(rank),
            "price_usd": num(data.get("price")),
            "change_24h_pct": pct(change),
        })
    return rows


def crawl(ctx: Ctx, opts: dict) -> str:
    rows = parse(ctx.http.get_json(URL), ctx.stamp, int(opts["top_n"]))
    if not rows:
        raise ValueError("CoinGecko 热搜返回空列表")
    append_csv(ctx.data_dir / FILE, HEADER, rows)
    top = "、".join(r["symbol"] or r["id"] for r in rows[:5])
    return f"记了前 {len(rows)} 名：{top}{' …' if len(rows) > 5 else ''}"
