# Flowmax Agent Skill

[![Claude Code](https://img.shields.io/badge/Claude%20Code-Skill-blueviolet)](https://claude.ai/code)
[![Codex](https://img.shields.io/badge/Codex-Skill-412991)](https://github.com/openai/codex)
[![Python](https://img.shields.io/badge/python-3-stdlib%20only-blue)](https://www.python.org/)

> 用**你自己的登录 JWT** 在 Hubble / Flowmax 市场上管理「属于你的」agent —— 创建 Research Agent、创建 PM Agent（自动纸面交易凭证 + 自动启停）、查看交易日志（盈亏 / 逐单 / 持仓）和 agent 信息。
> 面向**非开发用户**：用自然语言告诉 Claude / Codex 你要什么，它采访几个选项后自动完成，你不用碰命令行。

---

## 这是什么？

一个 Skill（`SKILL.md`）+ 配套命令行工具（`scripts/flowmax.py`，纯 Python 标准库，无需安装依赖）。**同时支持 Claude Code 和 Codex**（两者都识别 `SKILL.md` 标准）。

装上之后，你可以这样对话：

```
> 帮我建一个私有 BTC PM，稳健保值、保守 1×、15 分钟、纸面交易并自动启动
> 看看我那个 ETH 的 PM 最近亏了还是赚了
> 我的 agent 列表有哪些？哪个在跑？
> 持仓是不是太多了？
> 把那个建错的 agent 删掉
```

客户端会调用本 skill 的脚本，用**你的 JWT** 访问市场后端，拿到真实数据 / 执行真实操作，而不是凭空编造。

### 能力一览

| 类别 | 能做什么 |
|------|----------|
| **创建 Research Agent** | 研究型 agent（按数据源 + 分析类型产出研究），支持单个或批量 |
| **创建 PM Agent** | 决策型交易 agent，自动建 mock 纸面凭证（**不碰真钱**）、组装策略 prompt + 风险配置、默认**私有**、默认自动启动 |
| **查交易日志** | 每日已实现盈亏（看谁亏）、逐单盈亏（看哪笔爆仓）、当前持仓（看是否堆仓） |
| **查 agent 信息** | agent 列表、实时运行状态（scheduler / phase / 下次决策时间）、research 部署进度 |
| **运维** | 启停 scheduler、改决策周期、改 prompt / 风险档、删除 agent |

---

## 安装

把整个目录放到对应客户端的 skills 路径，重开客户端即可：

| 客户端 | 安装路径（用户级） |
|--------|--------------------|
| **Claude Code** | `~/.claude/skills/flowmax-skill/` |
| **Codex** | `~/.codex/skills/flowmax-skill/`（即 `$CODEX_HOME/skills/flowmax-skill/`，`$CODEX_HOME` 默认 `~/.codex`） |

目录结构保持不变：

```text
flowmax-skill/
├── SKILL.md            # 客户端读取的指令（Claude / Codex 通用）
├── README.md           # 本文（给人看）
└── scripts/flowmax.py  # 单一 CLI，纯标准库，python3 直跑
```

---

## 登录与环境配置

### JWT（必须）

登录 [Hubble 市场](https://market.prod.gcp.hubble-rpc.xyz) 网站后，从浏览器 **localStorage / cookie** 拿到登录 JWT（Privy）。

> ⚠️ **安全**：JWT 等同于你的账号登录态。**只在你自己终端里设置环境变量，绝不要贴进任何对话 / 聊天 / 截图。**

设置时注意：

- 环境变量**只存 `eyJ...` JWT 本体**。
- **不要加 `Bearer ` 前缀** —— 脚本会自动加 `Authorization: Bearer <jwt>`，你再加一遍会拼成 `Bearer Bearer eyJ...`，直接 `401 INVALID_TOKEN`。
- 在**启动 Claude Code / Codex 之前**设置，确保进程能继承该变量。

### 环境（默认 PROD）

默认连 **PROD**（`market.prod.gcp.hubble-rpc.xyz`）。要切到**测试环境**才需要设 `FLOWMAX_BASE`：

| 环境 | 地址 | 何时用 |
|------|------|--------|
| **prod（默认）** | `https://market.prod.gcp.hubble-rpc.xyz` | 线上真实操作 |
| staging | `https://market.dev.gcp.hubble-rpc.xyz` | 测试 / 日常 |

> prod 与 staging 启用的 LLM 可能不同（例如 prod 可能只有 `gemini_vertex`，无 `deepseek`）。脚本每次自动探测，不会写死。
> **prod JWT 只能配 prod，staging JWT 只能配 staging —— 混用必然 401。**

#### macOS / Linux

```bash
export FLOWMAX_JWT="<JWT 本体，不要带 Bearer>"
# 默认就是 prod，可省略 FLOWMAX_BASE；切 staging 才设下面这条：
# export FLOWMAX_BASE="https://market.dev.gcp.hubble-rpc.xyz"
```

#### Windows PowerShell

```powershell
$env:FLOWMAX_JWT = Read-Host "粘贴 JWT 本体，不要带 Bearer"
# 默认就是 prod；切 staging 才设下面这条：
# $env:FLOWMAX_BASE = "https://market.dev.gcp.hubble-rpc.xyz"
```

然后在**同一个终端**启动 Claude Code / Codex，确保它继承到变量。

---

## 快速开始

### 方式 A：自然语言（Claude Code / Codex，推荐非开发用户）

装好 skill 后直接对话即可。客户端会：

1. 先确认 JWT 已就绪、环境是 prod 还是 staging；
2. 问你几个高层选项（标的 / 策略目标 / 风险档位 / **私有 or 公开**，默认私有）；
3. **写操作前向你复述全部参数并确认**；
4. 调 `scripts/flowmax.py` 完成创建 / 查询 / 运维。

### 方式 B：直接命令行（三步跑通）

**第 1 步：认证探测**

```bash
export FLOWMAX_JWT="<你的 JWT>"   # 不要带 Bearer
python3 scripts/flowmax.py probe
```

确认：`env=PROD`（或你设的 staging）、JWT 认证成功、找到 mock exchange auth、找到当前环境启用的 LLM。每条命令都会在 stderr 打印 `[flowmax] env=… BASE=… JWT=…`，执行前看一眼环境对不对。

**第 2 步：创建一个私有纸面交易 PM**

```bash
python3 scripts/flowmax.py pm create \
  --name "保守·稳健保值 (BTC)" \
  --symbol BTCUSDT --goal steady --style conservative \
  --interval 900000 --overlay hard-stop --private
```

默认行为：新建 mock 纸面凭证（**不碰真钱**）、不绑真实交易所、**私有**（不进 marketplace）、15 分钟决策一次、建完自动启动 scheduler。

**第 3 步：验证它在跑**

```bash
python3 scripts/flowmax.py pm status --agent <agent_id>
```

应看到 `isSchedulerRunning=true` 且 `intervalMs=900000`。没启动就补：

```bash
python3 scripts/flowmax.py pm start --agent <agent_id> --ms 900000
```

---

## 命令参考

> JWT 从 `FLOWMAX_JWT` 读取；base 从 `FLOWMAX_BASE` 读取（**默认 prod**）。所有写操作请先确认。`--public` / `--private` 二选一，**默认私有**。

### 探测

| 命令 | 说明 |
|------|------|
| `probe [--asset-type Crypto]` | 列出合法 asset_type / 启用 LLM / datasource 清单 / mock id / 内嵌模板 |

### Research Agent

| 命令 | 说明 |
|------|------|
| `research create --name --prompt --asset-type --analysis-type [--public\|--private] [--wait 120]` | 建单个（默认私有；也支持 `--spec file.json` 批量） |
| `research get --agent <id>` | 详情，看 Creator 自动挑中了哪些数据源 |
| `research job --job <job_id>` | 部署进度（pending → deployed） |

### PM Agent

| 命令 | 说明 |
|------|------|
| `pm create --name --symbol --goal --style [--interval] [--overlay] [--research-ids] [--public\|--private]` | 建 PM（自动 mock 凭证 + 组装 prompt/risk + 默认自启 + 默认私有） |
| `pm list [--limit 50]` | 列出我的 PM |
| `pm status --agent <id>` | 实时状态（scheduler / phase / intervalMs / 下次决策） |
| `pm start --agent <id> --ms 900000` | 启动 scheduler（必须显式传周期） |
| `pm stop --agent <id>` | 停止 scheduler |
| `pm set-interval --agent <id> --ms 1800000` | 改决策周期（自动 stop→PUT→start） |
| `pm update --agent <id> --field risk_config --style balanced` | 改 prompt / 风险档 / 标的 / 名字（自动 stop→PUT→start） |
| `pm delete --agent <id> [--mock-auth <id>]` | 删除（先停后删，可选清 mock 凭证） |

### 交易日志 / agent 信息

| 命令 | 说明 |
|------|------|
| `logs pnl-summary --agent <id>` | 每日已实现盈亏（看谁亏） |
| `logs pnl-orders --agent <id>` | 逐单盈亏（看哪笔爆仓 / 胜亏比） |
| `logs positions --agent <id> [--status open]` | 当前持仓（看堆仓；≥6 个自动告警） |

> 字段口径在不同部署可能变化。表格解析不出来时加 `--raw` 看原始 JSON。

---

## 策略模板（内嵌，与市场向导逐字一致）

PM 的 `system_prompt` / `risk_config` / 决策周期由下面三组「goal × style」组合生成，无需手写：

| goal（策略目标） | 含义 | 默认周期 |
|------|------|----------|
| `steady` | 稳健保值 | 1h（脚本强制 15min） |
| `swing` | 波段 | 15min |
| `trend` | 趋势跟踪 | 15min |
| `arbitrage` | 套利对冲 | 1h（脚本强制 15min） |

| style（风险档位） | 杠杆 | 单标的最大保证金占比 |
|------|------|------|
| `conservative` | 1x | 15% |
| `balanced` | ≤3x | 20% |
| `aggressive` | ≤5x | 25% |

| 可选 `--overlay` | 作用 |
|------|------|
| `hard-stop` | **真实交易 PM 推荐**：追加硬止损纪律，防堆仓 / 单笔爆仓 |
| `multi-trade` | ⚠️ 历史遗留，会诱导堆仓，真实交易**不要用** |

| 可选 `--exit-policy` | 止盈止损偏好 |
|------|------|
| `take_quick` | 见好就收（+2% / -1.5%） |
| `disciplined` | 纪律为王（+5% / -3%） |
| `diamond` | 钻石手（+12% / -8%） |

> 模板从市场后端 `quick_create_service.py` 冻结抽取，保证与前端向导一致；后端模板升级时需手动同步本脚本。

---

## 401 INVALID_TOKEN 排错

出现 `JWT decode failed: Invalid or expired token` 时，按顺序检查：

1. 环境变量里**是否误带了 `Bearer ` 前缀**（脚本会自动加，不能重复）；
2. JWT **是否过期或因重新登录而失效**（重新登录网站拿新的）；
3. **prod JWT 是否误连了 staging**（或反之）—— 混用必 401，看命令开头的 `[flowmax] env=… BASE=…`；
4. `FLOWMAX_BASE` 是否设在**运行脚本的同一个终端**（子进程才能继承）；
5. Claude Code / Codex 是否在**设置变量之前**就已经启动了（要先设变量再启动客户端）。

---

## 常见坑（创建 / 查询高频踩中）

| 现象 | 原因 | 解决 |
|------|------|------|
| `pm status` / `pnl-summary` 返回 404 | list 与详情端点缓存不一致（已知问题） | 该 PM 仍能正常运行，列表看得到；详情查不到时反馈维护者 |
| 「数据两小时没更新」 | 某些 goal 默认周期 1h | 脚本已强制 `interval=900000`（15min） |
| 改了 prompt / 风险 / 周期不生效 | CF Worker 不自动重载 DB | 必须 `stop → PUT → start` 三步（`pm update` / `pm set-interval` 已封装） |
| `MATICUSDT` K 线报错 | Polygon 已 MATIC→POL 迁移 | 用 `POLUSDT` |
| `asset_type=crypto` 报 400 | 大小写错 | 必须 `Crypto`（首字母大写） |
| mock 凭证 `Decryption failed` | 旧密文不可逆解密 | 删旧凭证、建新 mock（脚本默认每次新建） |
| 删 PM 报 405 | 删除路径写错 | 脚本已用 `DELETE /agents/{id}`（不带 `pm`） |
| `--public false` 报错 | 旧语法，已废弃 | 用 `--private`（默认就是私有，可省略） |

---

## 安全说明

- **JWT 只从环境变量 `FLOWMAX_JWT` 读取**，脚本绝不打印明文（只显示掩码前缀）。
- **不要把 JWT 写进任何文件、提交进 git、或贴进对话**；也**不要带 `Bearer ` 前缀**（脚本自动加）。
- 写操作（创建 / 删除 / 启停）客户端会先向用户复述确认。
- mock 凭证 = 纸面交易，**不会动用真实资金**；真实交易所 PM 请通过网站前端绑定真实交易所。
- 新建 PM / research **默认私有**，不会意外进入 marketplace；确需公开再显式加 `--public`。

---

## 目录结构

```text
flowmax-skill/
├── README.md            # 本文
├── SKILL.md             # Skill 定义（Claude Code / Codex 读取的指令）
└── scripts/
    └── flowmax.py       # 单一 CLI，纯标准库，python3 直跑
```

---

## 许可证 / 反馈

内部工具，按需使用。问题与改进欢迎提 issue 或 PR。
