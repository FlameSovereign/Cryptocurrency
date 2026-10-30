"""消息推送：机器人有成交、触发止损止盈、出错时发到手机。

渠道靠环境变量（GitHub Secrets）开启，配了哪个用哪个：
- Server酱（微信）：SERVERCHAN_SENDKEY。SCT 开头是 Turbo 版（sctapi.ftqq.com），sctp 开头是 Server酱³ App
- PushPlus（微信）：PUSHPLUS_TOKEN
- Telegram：TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID（国内手机要翻墙才收得到）
- Bark（iPhone）：BARK_KEY；自建服务器再加 BARK_SERVER（默认 https://api.day.app）

config.toml 的 [notify] 只放不涉密的选项：
    channels = ["serverchan", "pushplus"]   # 只用这几个；不写 = 配了密钥的全用；写 [] = 全部关掉
    timeout = 10                            # 每个渠道最多等几秒

send() 绝不抛异常：每个渠道返回一行结果，比如 "pushplus: ok"、"telegram: 失败 HTTP 401 Unauthorized"；
密钥不会出现在返回结果里。能渲染 Markdown 的渠道（Server酱、PushPlus）发原文，
Telegram 和 Bark 发纯文本：表格拆成“• 机器人 + 表头: 值”的紧凑格式。

配好密钥后可以先发一条测试消息：python -m bot.notify
"""
from __future__ import annotations

import os
import re
from typing import Callable, Mapping
from urllib.parse import quote, urlsplit

import requests

CHANNELS = ("serverchan", "pushplus", "telegram", "bark")
REQUIRED = {
    "serverchan": ("SERVERCHAN_SENDKEY",),
    "pushplus": ("PUSHPLUS_TOKEN",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "bark": ("BARK_KEY",),
}
# 这些值不许出现在返回结果里（BARK_SERVER 可能是自建服务器地址，也一起藏起来）
SECRET_VARS = ("SERVERCHAN_SENDKEY", "PUSHPLUS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "BARK_KEY", "BARK_SERVER")
DEFAULT_TIMEOUT = 10.0
BARK_SERVER = "https://api.day.app"
GROUP = "梭哈机器人"


# ---------------- Markdown → 纯文本 ----------------

def _inline(s: str) -> str:
    s = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1 \2", s)  # [文字](链接) → 文字 链接
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)  # **粗体**
    s = s.replace("`", "")
    return s.strip()


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [_inline(c) for c in s.split("|")]


def _is_row(line: str) -> bool:
    return line.strip().startswith("|")


def _is_rule(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", line.strip()))


def _tables(md: str):
    """逐行扫描，产出 ("text", 行) 或 ("table", 表头, [行单元格...])。"""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        if _is_row(lines[i]) and i + 1 < len(lines) and _is_rule(lines[i + 1]):
            header, rows = _cells(lines[i]), []
            i += 2
            while i < len(lines) and _is_row(lines[i]):
                rows.append(_cells(lines[i]))
                i += 1
            yield ("table", header, rows)
            continue
        yield ("text", lines[i])
        i += 1


def md_to_text(md: str) -> str:
    """render() 产出的 Markdown → 手机上好读的纯文本：表格每行变成两行（名字 + “表头: 值 | ...”），去掉 # ** ` 这些符号。"""
    out: list[str] = []
    for item in _tables(md or ""):
        if item[0] == "text":
            out.append(_inline(re.sub(r"^\s*#{1,6}\s+", "", item[1])))
            continue
        _, header, rows = item
        for cells in rows:
            pairs = [f"{h}: {v}" if h else v for h, v in zip(header[1:], cells[1:]) if v]
            pairs += [v for v in cells[len(header):] if v]  # 单元格里有 | 时多出来的部分
            out.append(f"• {cells[0]}" if cells else "•")
            if pairs:
                out.append("  " + " | ".join(pairs))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def short_summary(md: str, limit: int = 64) -> str:
    """消息卡片上的一句话：每个机器人的名字 + “动作”列（没有这一列就用第二列）。"""
    parts = []
    for item in _tables(md or ""):
        if item[0] != "table":
            continue
        _, header, rows = item
        col = header.index("动作") if "动作" in header else 1
        parts += [" ".join(c for c in (cells[0], cells[col] if col < len(cells) else "") if c) for cells in rows if cells]
    text = "；".join(p for p in parts if p) or md_to_text(md).split("\n")[0]
    return _clip(text, limit)


# ---------------- 长度限制 ----------------

def _clip(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def _clip_bytes(s: str, limit: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[: max(0, limit - 3)].decode("utf-8", errors="ignore") + "…"


def _clip_utf16(s: str, limit: int) -> str:
    """Telegram 按 UTF-16 码元算长度（emoji 算 2 个）。"""
    if len(s.encode("utf-16-le")) // 2 <= limit:
        return s
    out, n = [], 0
    for ch in s:
        n += 2 if ord(ch) > 0xFFFF else 1
        if n > limit - 1:
            break
        out.append(ch)
    return "".join(out) + "…"


# ---------------- 各渠道 ----------------

def _json(r) -> dict | None:
    try:
        j = r.json()
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def _verdict(r, ok: Callable[[dict], bool], why: Callable[[dict], object]) -> str | None:
    """None = 成功；否则返回失败原因。"""
    j = _json(r)
    if r.status_code != 200:
        detail = why(j) if j else ""
        return f"HTTP {r.status_code}" + (f" {detail}" if detail else "")
    if j is None:
        return "返回的不是 JSON"
    return None if ok(j) else str(why(j) or j)


def serverchan_url(key: str) -> str:
    """SendKey → 接口地址。sctp<uid>t... 是 Server酱³，其余是 Turbo 版。也接受直接粘贴的完整接口地址。"""
    key = key.strip()
    if "://" in key:
        m = re.search(r"/([^/]+?)\.send\b", key)
        key = m.group(1) if m else key
    if key.startswith("sctp"):
        m = re.match(r"^sctp(\d+)t", key)
        if not m:
            raise ValueError("SERVERCHAN_SENDKEY 格式不对：Server酱³ 的 key 应该长这样 sctp<数字>t...")
        return f"https://{m.group(1)}.push.ft07.com/send/{key}.send"
    return f"https://sctapi.ftqq.com/{key}.send"


def _serverchan(title: str, md: str, text: str, env: Mapping[str, str], timeout: float) -> str | None:
    payload = {"title": _clip(title, 32), "desp": _clip_bytes(md, 32_000), "short": short_summary(md)}
    r = requests.post(serverchan_url(env["SERVERCHAN_SENDKEY"]), json=payload, timeout=timeout)
    return _verdict(r, lambda j: j.get("code") == 0, lambda j: j.get("message") or j.get("info"))


def _pushplus(title: str, md: str, text: str, env: Mapping[str, str], timeout: float) -> str | None:
    payload = {"token": env["PUSHPLUS_TOKEN"].strip(), "title": _clip(title, 100), "content": _clip(md, 18_000),
               "template": "markdown"}
    r = requests.post("https://www.pushplus.plus/send", json=payload, timeout=timeout)
    return _verdict(r, lambda j: j.get("code") == 200, lambda j: j.get("msg"))


def _telegram(title: str, md: str, text: str, env: Mapping[str, str], timeout: float) -> str | None:
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN'].strip()}/sendMessage"
    payload = {"chat_id": env["TELEGRAM_CHAT_ID"].strip(), "text": _clip_utf16(f"{title}\n\n{text}", 4096)}
    r = requests.post(url, json=payload, timeout=timeout)
    return _verdict(r, lambda j: j.get("ok") is True, lambda j: j.get("description"))


def _bark(title: str, md: str, text: str, env: Mapping[str, str], timeout: float) -> str | None:
    server, key = (env.get("BARK_SERVER") or "").strip() or BARK_SERVER, env["BARK_KEY"].strip()
    if "://" in key:  # 直接粘贴了 App 里的示例地址 https://api.day.app/<key>/这里改成你自己的推送内容
        u = urlsplit(key)
        server, key = f"{u.scheme}://{u.netloc}", next((p for p in u.path.split("/") if p), "")
    payload = {"device_key": key, "title": _clip(title, 100), "body": _clip_bytes(text, 2500), "group": GROUP}
    r = requests.post(server.rstrip("/") + "/push", json=payload, timeout=timeout)
    return _verdict(r, lambda j: j.get("code") == 200, lambda j: j.get("message"))


SENDERS = {"serverchan": _serverchan, "pushplus": _pushplus, "telegram": _telegram, "bark": _bark}


# ---------------- 入口 ----------------

def _scrub(s: str, secrets: list[str]) -> str:
    """把密钥从报错信息里抹掉（requests 的异常会带上 URL，Telegram / Server酱 的密钥就在 URL 里）。"""
    for v in sorted(secrets, key=len, reverse=True):
        if len(v) < 4:  # 太短的值不可能是真密钥，硬抹会把 "HTTP 412" 之类也抹花
            continue
        for form in {v, quote(v, safe="")}:
            s = s.replace(form, "***")
    return s


def _describe(e: Exception, timeout: float) -> str:
    if isinstance(e, requests.Timeout):
        return f"超时（{timeout:g} 秒）"
    if isinstance(e, requests.ConnectionError):
        return f"连不上（{type(e).__name__}）"
    return f"{type(e).__name__}: {e}"


def _wanted(settings: Mapping, env: Mapping[str, str]) -> tuple[list[str], bool]:
    allow = settings.get("channels")
    if allow is None:
        return [c for c in CHANNELS if any((env.get(k) or "").strip() for k in REQUIRED[c])], False
    if isinstance(allow, str):
        allow = [allow]
    return list(dict.fromkeys(str(c).strip().lower() for c in allow if str(c).strip())), True


def send(title: str, body: str, settings: dict | None = None, *, env: Mapping[str, str] | None = None) -> list[str]:
    """推送到所有配置好的渠道，返回每个渠道一行结果。永不抛异常。"""
    try:
        env = os.environ if env is None else env
        settings = settings or {}
        secrets = [v for v in ((env.get(k) or "").strip() for k in SECRET_VARS) if v]
        try:
            timeout = float(settings.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        channels, explicit = _wanted(settings, env)
        title, body = str(title or "梭哈机器人"), str(body or "")
        text = md_to_text(body)
        results = []
        for ch in channels:
            if ch not in SENDERS:
                results.append(f"{ch}: 失败 不认识的渠道（可选 {' / '.join(CHANNELS)}）")
                continue
            missing = [k for k in REQUIRED[ch] if not (env.get(k) or "").strip()]
            if missing:
                results.append(f"{ch}: {'未配置' if explicit and len(missing) == len(REQUIRED[ch]) else '失败'}，缺少 {' / '.join(missing)}")
                continue
            try:
                why = SENDERS[ch](title, body, text, env, timeout)
            except Exception as e:  # noqa: BLE001  推送失败不能把机器人搞崩
                why = _describe(e, timeout)
            results.append(f"{ch}: ok" if why is None else f"{ch}: 失败 {_clip(_scrub(why, secrets), 160)}")
        return results
    except Exception as e:  # noqa: BLE001  兜底：连结果都拼不出来也不能抛
        return [f"notify: 失败 {type(e).__name__}"]


SAMPLE = """## 🎰 梭哈机器人 · 测试推送

| 机器人 | 标的 | 动作 | 说明 |
|---|---|---|---|
| demo | BTC/USDT 1d | 🍵 空仓观望 | 能看到这条，说明推送配好了 |
"""


def main(argv: list[str] | None = None) -> int:
    """python -m bot.notify [标题] [正文]：发一条测试消息，看看各渠道配没配好。"""
    import sys
    import tomllib
    from pathlib import Path

    args = sys.argv[1:] if argv is None else argv
    settings = {}
    cfg = Path(__file__).resolve().parent.parent / "config.toml"
    try:
        with open(cfg, "rb") as f:
            settings = tomllib.load(f).get("notify") or {}
    except (OSError, ValueError):
        pass
    lines = send(args[0] if args else "梭哈机器人测试推送", args[1] if len(args) > 1 else SAMPLE, settings)
    print("\n".join(lines) or f"没有配置任何推送渠道：设置 {' / '.join(SECRET_VARS[:5])} 里的至少一个（Telegram 要两个都设）")
    return 0 if lines and all(line.endswith(": ok") for line in lines) else 1


if __name__ == "__main__":
    raise SystemExit(main())
