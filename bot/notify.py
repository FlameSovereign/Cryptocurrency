"""消息推送（占位实现，后面补渠道）。

接口：send(title, body, settings) -> list[str]，返回每个渠道的结果描述；任何渠道失败都不抛异常。
"""
from __future__ import annotations


def send(title: str, body: str, settings: dict | None = None) -> list[str]:
    return []
