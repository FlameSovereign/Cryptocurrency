"""Runner 体检：在要跑实盘的那台机器上检查环境。不需要 API key，不下单。

    python -m bot.doctor

看这几样：
- 这台机器的公网 IPv4（填进 Gate API key 的 IP 白名单）
- 连不连得上 Gate、GitHub
- 本机时钟和 Gate 服务器差多少（Gate 的签名对时间有要求）
- 系统、Python、ccxt 版本，是不是 GitHub 托管的机器
"""
from __future__ import annotations

import ipaddress
import os
import platform
import re
import sys
import time

import requests

from .data import make_exchange

# 都是只返回 IPv4 的服务：Gate 的 IP 白名单只收 IPv4
IP_SERVICES = ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://v4.ident.me", "https://myip.ipip.net")
MAX_SKEW_S = 5.0


def public_ipv4(get=requests.get) -> str | None:
    for url in IP_SERVICES:
        try:
            r = get(url, timeout=8, headers={"User-Agent": "curl/8"})
        except Exception:
            continue
        for m in re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", r.text if r.ok else ""):
            try:
                ip = ipaddress.IPv4Address(m)
            except ValueError:
                continue
            if ip.is_global:
                return str(ip)
    return None


def gate_clock(ex) -> tuple[float, float]:
    """(本机比 Gate 快多少秒, 往返毫秒)。"""
    t0 = time.time()
    server_ms = ex.fetch_time()
    t1 = time.time()
    return (t0 + t1) / 2 - server_ms / 1000, (t1 - t0) * 1000


def checkup(exchange_id: str = "gate", *, get=requests.get, exchange=None) -> tuple[list[str], bool]:
    """返回 (给人看的结果, 能不能跑实盘)。"""
    import ccxt

    ok = True
    env = os.environ.get("RUNNER_ENVIRONMENT") or ("本地" if not os.environ.get("GITHUB_ACTIONS") else "未知")
    lines = [f"- 机器：{platform.system()} {platform.release()} {platform.machine()} · Python {platform.python_version()} · "
             f"ccxt {ccxt.__version__} · runner：{env}"]
    if env == "github-hosted":
        ok = False
        lines.append("- ⚠️ 这是 GitHub 托管的机器：只能跑模拟盘。实盘要在你自己的机器上装 self-hosted runner，"
                     "再把仓库变量 RUNNER 设成 self-hosted")

    ip = public_ipv4(get)
    lines.append(f"- 公网 IPv4：**{ip}**　← 填到 Gate API key 的 IP 白名单（家里宽带的 IP 可能会变）" if ip
                 else "- 公网 IPv4：查不到（几个查 IP 的服务都连不上）")

    try:
        ex = exchange or make_exchange(exchange_id)
        skew, rtt = gate_clock(ex)
        lines.append(f"- {exchange_id}：连得上，往返 {rtt:.0f} ms；本机时钟比交易所{'快' if skew >= 0 else '慢'} {abs(skew):.1f} 秒")
        if abs(skew) > MAX_SKEW_S:
            ok = False
            lines.append(f"- ⚠️ 时钟偏差超过 {MAX_SKEW_S:.0f} 秒，下单签名可能被拒：打开系统的自动对时")
    except Exception as e:
        ok = False
        lines.append(f"- ⚠️ {exchange_id}：连不上（{type(e).__name__}: {e}）")

    try:
        code = get("https://api.github.com", timeout=8).status_code
        lines.append(f"- GitHub：连得上（HTTP {code}）")
    except Exception as e:
        lines.append(f"- ⚠️ GitHub：连不上（{type(e).__name__}）。runner 要一直连着 GitHub 才收得到任务")
    return lines, ok


def main() -> int:
    from .run import utf8_stdout

    utf8_stdout()
    lines, ok = checkup()
    verdict = "✅ 这台机器可以跑实盘" if ok else "❌ 这台机器现在还不能跑实盘（见上面的 ⚠️）"
    text = "\n".join(["## 🩺 Runner 体检", "", *lines, "", verdict]) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
