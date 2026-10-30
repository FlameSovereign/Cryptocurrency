"""恐惧贪婪指数（alternative.me）：每次拉全量历史，按日期合并去重（新值覆盖旧值），按日期排序存盘。"""
from __future__ import annotations

from datetime import datetime, timezone

from .common import Ctx, read_csv, write_csv

NAME = "fear_greed"
TITLE = "恐惧贪婪指数"
FILE = "fear_greed.csv"
HEADER = ["date", "value", "classification"]
SNAPSHOT = False  # 按日期合并，不是快照
DEFAULTS: dict = {}
URL = "https://api.alternative.me/fng/"


def parse(payload: dict) -> list[dict]:
    """接口返回 → [{date, value, classification}]，按日期升序；坏数据跳过。"""
    err = (payload.get("metadata") or {}).get("error")
    if err:
        raise ValueError(f"alternative.me 报错：{err}")
    by_day: dict[str, dict] = {}
    for d in payload.get("data") or []:
        try:
            ts, value = int(d["timestamp"]), int(d["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= value <= 100:
            continue
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        # 接口按新 → 旧排列，同一天出现两次时保留先出现的（更新的那条）
        by_day.setdefault(day, {"date": day, "value": str(value), "classification": str(d.get("value_classification") or "")})
    return [by_day[k] for k in sorted(by_day)]


def crawl(ctx: Ctx, opts: dict) -> str:
    fresh = parse(ctx.http.get_json(URL, params={"limit": 0, "format": "json"}))
    if not fresh:
        raise ValueError("alternative.me 没返回任何数据")
    path = ctx.data_dir / FILE
    header, old_rows = read_csv(path)
    merged = {r["date"]: {k: r.get(k) or "" for k in HEADER} for r in old_rows if r.get("date")}
    added = sum(r["date"] not in merged for r in fresh)
    changed = sum(r["date"] in merged and merged[r["date"]] != r for r in fresh)
    merged.update((r["date"], r) for r in fresh)
    rows = [merged[k] for k in sorted(merged)]
    if added or changed or header != HEADER:
        write_csv(path, HEADER, rows)  # 没变化就不动文件，git 里也不会多一次提交
    last = rows[-1]
    return f"共 {len(rows)} 天（新增 {added}，修正 {changed}），最新 {last['date']} = {last['value']} {last['classification']}"
