# 实盘指南：用 GitHub 跑实盘

## 分工：GitHub 当大脑，你的机器当手

| | 谁来做 |
|---|---|
| 定时触发、存 API key、看日志 / Summary、存账本（`bot-data` 分支）、改配置 | GitHub |
| 真正连 Gate、下单 | 你自己的一台机器（GitHub 的 **self-hosted runner**） |

为什么不直接用 GitHub 的机器跑实盘（代码里直接拒绝）：

- 那些机器在美国机房，IP 每次都变，Gate API key 没法绑 IP 白名单；而不绑白名单的 key [90 天自动失效](https://www.gate.com/announcements/article/27564)，到期机器人就卡住了
- Gate 用户协议把美国列为[受限地区](https://www.gate.com/help/guide/faq/40959/restricted-locations)，从那些 IP 调用你的账户有被风控的风险

费用：自托管 runner 跑的任务历来不消耗 GitHub Actions 的免费分钟数。GitHub 2025 年底宣布过要对私有仓库的自托管 runner 按 $0.002/分钟收费，随后又[宣布推迟](https://www.techzine.eu/news/devops/137396/github-bends-to-criticism-and-delays-paid-self-hosting-of-runners/)，现在的情况以你的 GitHub 账单页为准。就算真收：每小时跑一次、每次一分钟左右，一个月大约 720 分钟 ≈ 1.4 美元。

## 准备

- 一台一直开着、能上网的机器：家里的电脑、小主机、NAS、服务器都行
  - 系统：Linux（Ubuntu 最省心）、macOS，或者 Windows（要装 [Git for Windows](https://git-scm.com/download/win)，workflow 用的是 bash）
  - 能稳定连上 GitHub（runner 靠长连接等任务，连不上就不会跑）和 Gate
  - 系统时间准（开着自动对时就行，Gate 的签名对时间有要求）
- 给机器人的钱：几百 USDT
- 半小时

> Gate 的受限地区名单里也有中国大陆。runner 在哪台机器上跑，就算从哪里访问 Gate——这个只有你自己能判断。

## 第 1 步：仓库改成私有

仓库 Settings → General → 拉到最下面 Danger Zone → Change visibility → Private。

- 公开仓库谁都能提 PR，自托管 runner 等于把你的电脑借给陌生人跑代码（[GitHub 官方也这么说](https://docs.github.com/en/actions/reference/security/secure-use)）
- 改私有后 `bot-data` 里的账本、成交记录也只有你看得到
- 私有仓库每月有 2000 分钟免费额度给 GitHub 托管的机器：只剩爬虫和测试在用，绰绰有余

## 第 2 步：在你的机器上装 runner

仓库 Settings → Actions → Runners → **New self-hosted runner** → 选你的系统，页面会给几行命令（下载、解压、`./config.sh --url … --token …`），照着在那台机器上运行。

- 配置过程一路回车用默认值（默认标签里就有 `self-hosted`）
- 装成系统服务，开机自启、断线自动重连：
  - Linux：在 runner 目录里 `sudo ./svc.sh install`，再 `sudo ./svc.sh start`
  - macOS：在 runner 目录里 `./svc.sh install`，再 `./svc.sh start`（不用 sudo）
  - Windows：配置时问 run as service 选 Y
- 回到 Runners 页面，看到它显示 **Idle**（绿点）就成了

**Python**：workflow 会用 `actions/setup-python` 自动装 Python 3.12（第一次下载，之后有缓存）。如果你的系统它不支持（树莓派、群晖这类非 Ubuntu 的 Linux），先在机器上装好 Python 3.11 以上，再加一个仓库变量 `PYTHON_SETUP` = `system`。

**国内网络**：pip 装依赖慢的话，给跑 runner 的那个系统用户配个镜像：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`。

## 第 3 步：Gate 上建子账户和 API key

1. **子账户**：给机器人单开一个，从主账户划转机器人的预算（USDT）进去。机器人只动自己账本里的钱，但钱物理隔离最省心
2. **在子账户里建 APIv4 key**（要先实名、设好资金密码和二次验证）：
   - 权限**只开「现货」读写**；提现、合约、杠杆、理财全部关掉
   - IP 白名单：填 runner 那台机器的公网 IP（在那台机器上 `curl ip.sb` 看）
     - 家里宽带的 IP 可能会变：变了以后机器人会报 key 无效（你会收到报错推送），去 Gate 改一下白名单就好
     - 也可以不绑：那样 key 90 天自动失效，到期前 Gate 会提醒，换新 key 后更新 GitHub Secret
   - 经典账户、统一账户都支持；统一账户里别开自动借币

## 第 4 步：GitHub 里填密钥和开关

仓库 Settings → Secrets and variables → Actions：

| 类型 | 名字 | 值 |
|---|---|---|
| Secret | `EXCHANGE_API_KEY` | Gate 的 key |
| Secret | `EXCHANGE_API_SECRET` | Gate 的 secret |
| Secret | `SERVERCHAN_SENDKEY` 或 `PUSHPLUS_TOKEN` | 微信推送（可选，强烈建议） |
| Variable | `RUNNER` | `self-hosted` |
| Variable | `BOT_MODE` | `live`（想先演练就填 `testnet`，见文末） |

先**别**开 `BOT_ENABLED`。

## 第 5 步：检查 config.toml

- 所有实盘机器人（`paper_only = true` 的对照组不算）的 `budget` 加起来，别超过子账户里的 USDT。默认是 3 个 × 100 = 300
- `[live] max_order`：单笔买入上限，默认 500，第一次可以设小点
- 想先只开一个机器人：其他的写 `enabled = false`

## 第 6 步：空跑体检

Actions → 梭哈机器人 → Run workflow → 勾上「只看信号」→ Run。跑完看 Summary：

- **API key 体检**：绑没绑 IP 白名单（没绑会提醒 90 天失效）、账户实际费率、是不是统一账户、key 有没有限定交易对
- 每个机器人现在想干嘛（这次不会下单）

有报错先解决。再 Run 一次、只勾「只发一条测试推送」，看看微信收不收得到。

## 第 7 步：上线

Variables 加 `BOT_ENABLED` = `true`。之后每小时第 7 分钟左右自动跑一次，有成交、止损、熔断、报错都会推送。

## 日常操作

| 想干嘛 | 怎么做 |
|---|---|
| 暂停自动交易 | `BOT_ENABLED` 改成 `false`（手里的币不会动） |
| 某个机器人卖光收工 | config 里给它加 `liquidate = true`，push；不想等一小时就手动 Run workflow 一次 |
| 熔断后复活 / 清空战绩 | config 里加 `reset = "随便一个新值"`（实盘要先空仓；以后想再重置就换个值） |
| 加减预算 | 直接改 `budget`（减预算按比例从净值里抽，收益率不变） |
| 看账本、成交记录 | `bot-data` 分支的 `state/live/` |
| 换 API key | 更新 Secrets，下次运行生效 |

## 出岔子会怎样

| 情况 | 机器人的反应 |
|---|---|
| 下单请求超时、不知道成没成 | 先按订单备注去查；查不到就报错，下次运行对账时自动认领，绝不盲目重下 |
| 成交了但账本没推上 GitHub | 下次运行按备注把订单找回来，补进成交记录；止损单的冷静期也会接着算 |
| 拉不到 `bot-data` 分支（网络抽风） | 这次直接不跑，不拿空账本交易 |
| 查不了历史订单（没法对账） | 这次不交易 |
| runner 离线 | 任务排队等 runner 回来（[排满 24 小时左右会被取消](https://docs.github.com/en/actions/hosting-your-own-runners/about-self-hosted-runners)），期间不会交易，止损也不会执行 |
| 账户里的币比账本少（你手动卖了） | 以账户为准 |

## 用假钱演练（可选，推荐）

Gate 有测试网：去 [testnet.gate.com](https://testnet.gate.com/) 建 key，账户类型选 **Testnet**。Secrets 加 `EXCHANGE_TESTNET_API_KEY`、`EXCHANGE_TESTNET_API_SECRET`，Variable `BOT_MODE` = `testnet`，其余步骤一样。流程和实盘完全相同，钱是假的；账本单独存在 `state/testnet/`。测试网同样只在自托管 runner 上跑。
