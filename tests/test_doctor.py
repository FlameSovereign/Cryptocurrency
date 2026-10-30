"""Runner 体检：不联网，假的 HTTP 和交易所。"""
import socket
import time

import pytest

from bot import doctor


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code, self.ok = text, status, status < 400


def fake_get(table):
    def get(url, **kw):
        v = table.get(url)
        if isinstance(v, Exception):
            raise v
        return v or Resp("", 404)
    return get


def test_public_ipv4_skips_failures_and_private_addresses():
    get = fake_get({
        "https://api.ipify.org": ConnectionError("blocked"),
        "https://ipv4.icanhazip.com": Resp("10.0.0.5\n"),  # 内网地址不算
        "https://v4.ident.me": Resp("", 500),
        "https://myip.ipip.net": Resp("当前 IP：203.0.113.77  来自于：中国 上海 电信\n".replace("203.0.113.77", "61.152.1.2")),
    })
    assert doctor.public_ipv4(get) == "61.152.1.2"
    assert doctor.public_ipv4(fake_get({})) is None


class Ex:
    def __init__(self, skew_s=0.0, fail=False):
        self.skew, self.fail = skew_s, fail

    def fetch_time(self):
        if self.fail:
            raise ConnectionError("no route")
        return int((time.time() - self.skew) * 1000)


OK_GET = fake_get({"https://api.ipify.org": Resp("8.8.4.4"), "https://api.github.com": Resp("{}")})


def test_checkup_self_hosted_ok(monkeypatch):
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    lines, ok = doctor.checkup(get=OK_GET, exchange=Ex(0.2))
    text = "\n".join(lines)
    assert ok and "8.8.4.4" in text and "连得上" in text


@pytest.mark.parametrize("env, ex, why", [
    ("github-hosted", Ex(0.0), "GitHub 托管"),
    ("self-hosted", Ex(30.0), "时钟偏差"),
    ("self-hosted", Ex(fail=True), "连不上"),
])
def test_checkup_flags_problems(monkeypatch, env, ex, why):
    monkeypatch.setenv("RUNNER_ENVIRONMENT", env)
    lines, ok = doctor.checkup(get=OK_GET, exchange=ex)
    assert not ok and why in "\n".join(lines)


def test_force_ipv4(monkeypatch):
    import urllib3.util.connection as conn

    from bot.data import force_ipv4

    monkeypatch.setattr(conn, "allowed_gai_family", conn.allowed_gai_family)  # 测完还原
    force_ipv4()
    assert conn.allowed_gai_family() == socket.AF_INET
