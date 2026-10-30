"""推送：requests.post 全部打桩，不联网。"""
import pytest
import requests

from bot import notify

MD = """## 🎰 梭哈机器人 · 模拟盘 · 2026-10-30 06:00:00 UTC

| 机器人 | 标的 | 策略 | 动作 | 价格 | 净值 | 收益 | 说明 |
|---|---|---|---|---|---|---|---|
| btc-hodl | BTC/USDT 1d | 拿住不动 | 🟢 买入 100.00 USDT @ 78616.3 | 78616.3 | 99.80 USDT | -0.2% | 满仓拿着 |
| x | SOL/USDT | t | ⚠️ error | | | | boom |

**备注**
- `btc-hodl` 拿实时价失败，用最近收盘价
"""

TOKEN = "123456789:AAH-telegram-secret-token"
ENV_ALL = {
    "SERVERCHAN_SENDKEY": "SCT12345TabcdefSECRET",
    "PUSHPLUS_TOKEN": "pp-secret-token-0001",
    "TELEGRAM_BOT_TOKEN": TOKEN,
    "TELEGRAM_CHAT_ID": "987654321",
    "BARK_KEY": "barkSECRETkey42",
}


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


OK = {
    "sctapi.ftqq.com": Resp(200, {"code": 0, "message": "", "data": {"pushid": "1", "errno": 0}}),
    "push.ft07.com": Resp(200, {"code": 0, "message": "SUCCESS"}),
    "pushplus.plus": Resp(200, {"code": 200, "msg": "请求成功", "data": "abc"}),
    "api.telegram.org": Resp(200, {"ok": True, "result": {"message_id": 1}}),
    "/push": Resp(200, {"code": 200, "message": "success", "timestamp": 1}),
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """兜底：哪个测试忘了打桩，真请求一律拦下。"""
    def blocked(*a, **k):
        raise AssertionError("测试里不许真的联网")

    monkeypatch.setattr(requests.Session, "request", blocked)
    monkeypatch.setattr(notify.requests, "post", blocked)


@pytest.fixture
def post(monkeypatch):
    """记录每次 post；按 URL 关键字返回 OK 表里的响应，可以在测试里改。"""
    calls = []
    table = dict(OK)

    def fake(url, json=None, timeout=None, **kw):
        calls.append({"url": url, "json": json, "timeout": timeout})
        for key, r in table.items():
            if key in url:
                if isinstance(r, Exception):
                    raise r
                return r
        raise AssertionError(f"没想到会请求 {url}")

    monkeypatch.setattr(notify.requests, "post", fake)
    fake.calls, fake.table = calls, table
    return fake


def by_host(calls, key):
    return next(c for c in calls if key in c["url"])


def test_nothing_configured_sends_nothing(post):
    assert notify.send("t", MD, {}, env={}) == []
    assert notify.send("t", MD, None, env={"BARK_SERVER": "https://bark.example.com"}) == []  # 只有服务器地址不算配置
    assert post.calls == []


def test_all_channels_ok_with_right_formats(post):
    out = notify.send("梭哈机器人有动静", MD, {}, env=ENV_ALL)
    assert out == ["serverchan: ok", "pushplus: ok", "telegram: ok", "bark: ok"]

    sc = by_host(post.calls, "sctapi")
    assert sc["url"] == "https://sctapi.ftqq.com/SCT12345TabcdefSECRET.send"
    assert sc["json"]["desp"] == MD and sc["json"]["title"] == "梭哈机器人有动静"  # 微信渠道发 markdown 原文
    assert sc["json"]["short"].startswith("btc-hodl 🟢 买入") and len(sc["json"]["short"]) <= 64

    pp = by_host(post.calls, "pushplus")
    assert pp["url"] == "https://www.pushplus.plus/send"
    assert pp["json"] == {"token": "pp-secret-token-0001", "title": "梭哈机器人有动静", "content": MD, "template": "markdown"}

    tg = by_host(post.calls, "telegram")
    assert tg["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and tg["json"]["chat_id"] == "987654321"
    text = tg["json"]["text"]
    assert "|---" not in text and "**" not in text and "`" not in text and "##" not in text
    assert "• btc-hodl" in text and "动作: 🟢 买入 100.00 USDT @ 78616.3" in text and "说明: boom" in text

    bark = by_host(post.calls, "/push")
    assert bark["url"] == "https://api.day.app/push"
    assert bark["json"]["device_key"] == "barkSECRETkey42" and "• btc-hodl" in bark["json"]["body"]
    assert all(c["timeout"] == notify.DEFAULT_TIMEOUT for c in post.calls)


def test_serverchan_url_formats():
    assert notify.serverchan_url("SCT1TabcXYZ") == "https://sctapi.ftqq.com/SCT1TabcXYZ.send"
    assert notify.serverchan_url(" sctp1234tKEYabc ") == "https://1234.push.ft07.com/send/sctp1234tKEYabc.send"
    assert notify.serverchan_url("https://1234.push.ft07.com/send/sctp1234tKEYabc.send") == \
        "https://1234.push.ft07.com/send/sctp1234tKEYabc.send"  # 粘贴了完整地址也认
    assert notify.serverchan_url("https://sctapi.ftqq.com/SCT1TabcXYZ.send") == "https://sctapi.ftqq.com/SCT1TabcXYZ.send"
    with pytest.raises(ValueError):
        notify.serverchan_url("sctpXYZ")


def test_serverchan3_key_goes_to_ft07(post):
    out = notify.send("t", MD, {}, env={"SERVERCHAN_SENDKEY": "sctp42tSECRETKEY"})
    assert out == ["serverchan: ok"] and post.calls[0]["url"] == "https://42.push.ft07.com/send/sctp42tSECRETKEY.send"


def test_failures_are_lines_not_exceptions(post):
    post.table.update({
        "pushplus.plus": Resp(500, None),
        "api.telegram.org": Resp(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
        "sctapi.ftqq.com": Resp(200, {"code": 40001, "message": "bad pushkey"}),
        "/push": requests.ConnectionError("Max retries exceeded"),
    })
    out = notify.send("t", MD, {}, env=ENV_ALL)
    assert out == ["serverchan: 失败 bad pushkey", "pushplus: 失败 HTTP 500", "telegram: 失败 HTTP 401 Unauthorized",
                   "bark: 失败 连不上（ConnectionError）"]


def test_timeout_and_non_json(post):
    post.table.update({"pushplus.plus": requests.Timeout("read timed out"), "api.telegram.org": Resp(200, None)})
    out = notify.send("t", MD, {"timeout": 3}, env=ENV_ALL)
    assert "pushplus: 失败 超时（3 秒）" in out and "telegram: 失败 返回的不是 JSON" in out
    assert {c["timeout"] for c in post.calls} == {3.0}


def test_secrets_never_leak_into_results(post):
    post.table.update({
        "api.telegram.org": RuntimeError(f"boom at https://api.telegram.org/bot{TOKEN}/sendMessage"),
        "sctapi.ftqq.com": Resp(200, {"code": 1, "message": "key SCT12345TabcdefSECRET 不存在"}),
        "pushplus.plus": Resp(400, {"code": 903, "msg": "token pp-secret-token-0001 无效"}),
        "/push": OSError("https://bark.example.com/push barkSECRETkey42"),
    })
    out = notify.send("t", MD, {}, env={**ENV_ALL, "BARK_SERVER": "https://bark.example.com"})
    joined = "\n".join(out)
    for secret in (TOKEN, "SCT12345TabcdefSECRET", "pp-secret-token-0001", "barkSECRETkey42", "bark.example.com"):
        assert secret not in joined
    assert len(out) == 4 and all("失败" in line for line in out) and "***" in joined


def test_allow_list(post):
    assert notify.send("t", MD, {"channels": ["pushplus"]}, env=ENV_ALL) == ["pushplus: ok"]
    assert [c["url"] for c in post.calls] == ["https://www.pushplus.plus/send"]
    assert notify.send("t", MD, {"channels": []}, env=ENV_ALL) == []  # 写 [] = 全关
    out = notify.send("t", MD, {"channels": ["Serverchan", "wechat"]}, env={})
    assert out[0] == "serverchan: 未配置，缺少 SERVERCHAN_SENDKEY" and out[1].startswith("wechat: 失败 不认识的渠道")
    assert notify.send("t", MD, {"channels": "bark"}, env=ENV_ALL) == ["bark: ok"]  # 写成字符串也认


def test_half_configured_telegram_is_reported(post):
    assert notify.send("t", MD, {}, env={"TELEGRAM_BOT_TOKEN": TOKEN}) == ["telegram: 失败，缺少 TELEGRAM_CHAT_ID"]
    assert post.calls == []


def test_reads_os_environ(post, monkeypatch):
    for k in ENV_ALL:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUSHPLUS_TOKEN", "pp-from-env-123")
    assert notify.send("t", MD) == ["pushplus: ok"]
    assert post.calls[0]["json"]["token"] == "pp-from-env-123"


def test_bark_custom_server_and_pasted_url(post):
    notify.send("t", MD, {}, env={"BARK_KEY": "k1", "BARK_SERVER": "https://bark.example.com/"})
    assert post.calls[-1]["url"] == "https://bark.example.com/push" and post.calls[-1]["json"]["device_key"] == "k1"
    notify.send("t", MD, {}, env={"BARK_KEY": "https://api.day.app/PASTEDkey/"})
    assert post.calls[-1]["url"] == "https://api.day.app/push" and post.calls[-1]["json"]["device_key"] == "PASTEDkey"
    notify.send("t", MD, {}, env={"BARK_KEY": "https://bark.example.com/K2/这里改成你自己的推送内容"})  # App 里的示例地址
    assert post.calls[-1]["url"] == "https://bark.example.com/push" and post.calls[-1]["json"]["device_key"] == "K2"


def test_long_messages_are_clipped(post):
    long_md = MD + "\n" + "\n".join(f"- 第 {i} 条备注 🚀" for i in range(3000))
    assert notify.send("t" * 50, long_md, {}, env=ENV_ALL) == ["serverchan: ok", "pushplus: ok", "telegram: ok", "bark: ok"]
    tg = by_host(post.calls, "telegram")["json"]["text"]
    assert len(tg.encode("utf-16-le")) // 2 <= 4096 and tg.endswith("…")
    assert len(by_host(post.calls, "/push")["json"]["body"].encode()) <= 2500
    sc = by_host(post.calls, "sctapi")["json"]
    assert len(sc["title"]) <= 32 and len(sc["desp"].encode()) <= 32_000


def test_never_raises_even_on_garbage(post, monkeypatch):
    with monkeypatch.context() as m:  # 只撤销这一处，别把 requests.post 的桩也撤了
        m.setattr(notify, "md_to_text", lambda md: 1 / 0)
        assert notify.send("t", MD, {}, env=ENV_ALL) == ["notify: 失败 ZeroDivisionError"]
    assert notify.send(None, None, {"timeout": "abc"}, env={"PUSHPLUS_TOKEN": "pp-token-x"}) == ["pushplus: ok"]
    assert post.calls[-1]["json"]["title"] == "梭哈机器人" and post.calls[-1]["timeout"] == notify.DEFAULT_TIMEOUT


def test_md_to_text_shapes():
    text = notify.md_to_text(MD)
    lines = text.splitlines()
    assert lines[0] == "🎰 梭哈机器人 · 模拟盘 · 2026-10-30 06:00:00 UTC"
    assert lines[2] == "• btc-hodl"
    assert lines[3] == ("  标的: BTC/USDT 1d | 策略: 拿住不动 | 动作: 🟢 买入 100.00 USDT @ 78616.3 | 价格: 78616.3 "
                        "| 净值: 99.80 USDT | 收益: -0.2% | 说明: 满仓拿着")
    assert lines[5] == "  标的: SOL/USDT | 策略: t | 动作: ⚠️ error | 说明: boom"  # 空单元格跳过
    assert "备注" in lines and "- btc-hodl 拿实时价失败，用最近收盘价" in lines
    assert notify.md_to_text("") == "" and notify.md_to_text("纯文本 **加粗** [链接](https://x.y)") == "纯文本 加粗 链接 https://x.y"


def test_works_with_real_render_output():
    from bot.run import render

    rep = {"bot": "eth-trend", "symbol": "ETH/USDT", "title": "趋势", "timeframe": "4h", "status": "ok", "notes": ["备注一条"],
           "fill": {"side": "sell", "price": 2400.5, "base": 0.04, "quote": 96.0}, "price": 2400.5, "equity": 96.0,
           "pnl": -0.04, "reason": "跌破止损", "event": "stop_loss", "exposure": 0.0, "plan": {"side": "sell"}}
    try:
        md = render([rep], "paper")
    except Exception as e:  # render 归 run.py 管，字段变了就跳过这条兼容性检查
        pytest.skip(f"render() 接口变了：{e}")
    text = notify.md_to_text(md)
    assert "• eth-trend" in text and "|---" not in text and "备注一条" in text
    assert notify.short_summary(md).startswith("eth-trend")


def test_cli_self_test(post, monkeypatch, capsys):
    for k in ENV_ALL:
        monkeypatch.delenv(k, raising=False)
    assert notify.main([]) == 1 and "没有配置任何推送渠道" in capsys.readouterr().out
    monkeypatch.setenv("PUSHPLUS_TOKEN", "pp-cli-token-1")
    assert notify.main(["标题"]) == 0 and capsys.readouterr().out.strip() == "pushplus: ok"
    assert post.calls[-1]["json"]["title"] == "标题" and "测试推送" in post.calls[-1]["json"]["content"]
