"""状态存盘：每个机器人一个 JSON（钱包 + 已认领订单），外加成交和净值流水 CSV。

GitHub Actions 每次都是全新的机器，状态靠 workflow 最后一步 commit 回仓库来延续。
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from .candles import now_ms
from .wallet import Wallet

KEEP_ORDERS = 300


def bot_path(state_dir: Path, bot_name: str) -> Path:
    return state_dir / "bots" / f"{bot_name}.json"


def load_bot(state_dir: Path, bot_name: str, budget: float) -> dict:
    p = bot_path(state_dir, bot_name)
    if p.exists():
        st = json.loads(p.read_text())
        st.setdefault("orders", [])
        st.setdefault("runs", 0)
        return st
    return {"bot": bot_name, "created_ms": now_ms(), "runs": 0, "orders": [], "wallet": Wallet.new(budget).to_dict()}


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def save_bot(state_dir: Path, bot_name: str, st: dict) -> None:
    st["orders"] = st.get("orders", [])[-KEEP_ORDERS:]
    st["updated_ms"] = now_ms()
    save_json(bot_path(state_dir, bot_name), st)


def append_csv(path: Path, header: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
