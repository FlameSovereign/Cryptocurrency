"""对照组：所有策略都得先打赢这两位。"""
from __future__ import annotations

import random

from ..candles import timeframe_ms
from . import register


@register(
    "hodl",
    title="无脑囤币",
    school="对照组",
    summary="买入后一直拿着，啥也不干。打不过它的策略只是在给交易所交手续费。",
    defaults={},
    warmup=lambda p: 1,
)
def hodl(candles, position, params, ctx):
    return 1.0, "拿住不动"


@register(
    "monkey",
    title="猴子扔骰子",
    school="对照组",
    summary="每隔 hold 根 K 线随机决定梭哈还是空仓。打不过猴子的策略 = 玄学。",
    defaults={"hold": 24, "p_long": 0.5, "seed": 42},
    warmup=lambda p: 1,
)
def monkey(candles, position, params, ctx):
    hold = max(1, int(params["hold"]))
    epoch = candles[-1].ts // (timeframe_ms(ctx.timeframe) * hold)
    # 用 (种子, 时间段) 做随机种子：同一时间段结果固定，满足幂等
    rng = random.Random(f"{params['seed']}:{epoch}")
    go = rng.random() < float(params["p_long"])
    return (1.0, "猴子说：梭！") if go else (0.0, "猴子说：跑！")
