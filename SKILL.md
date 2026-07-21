---
name: flowmax
description: 让非开发用户用自己的 JWT 在 Hubble/Flowmax 市场上管理「自己的 agent」——创建 Research Agent、创建 PM Agent（自动纸面交易凭证+自动启停）、查看交易日志（每日盈亏/逐单/持仓）和 agent 信息（列表/状态/部署进度）。当用户说想建一个 agent、看看自己的 agent 跑得怎么样、亏了还是赚了、持仓多少时使用。配套脚本 scripts/flowmax.py 负责所有机械步骤。
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
---

# Flowmax Agent Skill（JWT 管理「我自己的」agent）

面向**非开发用户**：用户用自然语言说要什么，你（Claude）采访清楚高层选项，再调脚本完成。用户**不碰命令行**。

## 何时用

- 用户想**创建**一个 Research Agent（研究某个主题/数据维度）或 PM Agent（自动决策交易）
- 用户想**看自己的 agent 跑得怎样**：盈亏、持仓、决策状态、列表
- 用户想**删掉**一个建错的 agent

## ⚠️ 前置：JWT（必须，且只在用户自己的 shell 里设置）

- 用户登录网站后，从浏览器 localStorage / cookie 拿到登录 JWT（Privy）。
- **绝不让用户把 JWT 贴进对话**——会进对话记录。让用户**自己在终端** export：
  ```
  export FLOWMAX_JWT="<把 token 粘这里>"
  ```
  如果在 Claude Code 里，让用户用 `! export ...` 或在自己的 shell 里设好再启动。
- 环境变量没设时，脚本会直接报错退出，提示设置方法。**别替用户猜/存 token**。
- 环境：默认连 **staging**（`market.dev.gcp.hubble-rpc.xyz`）。用户要操作**线上正式**环境，让他再设 `export FLOWMAX_BASE="https://market.prod.gcp.hubble-rpc.xyz"`（注意 prod 用的 LLM 可能和 staging 不同，脚本每次探测，不写死）。

## 写操作必须先确认

创建 / 删除 / 启停 scheduler 是用户账号上的**真实 agent**。执行前把「将做什么 + 关键参数」复述给用户，得到明确「确认」再调脚本。**不要**未经确认就建一堆 agent。

---

## 一、创建 PM Agent（最常见）

### 你来采访，用户只做选择

依次问（可用 AskUserQuestion 一次性问）：

1. **交易标的**：哪个币？如 `BTCUSDT`（大写、不带横杠）。⚠️ `MATICUSDT` 已下架，用 `POLUSDT`。
2. **策略目标 goal**：`steady` 稳健保值 / `swing` 波段 / `trend` 趋势 / `arbitrage` 套利。
3. **风险档位 style**：`conservative` 保守(1x) / `balanced` 平衡(≤3x) / `aggressive` 激进(≤5x)。
4. **公开？** 公开会进 marketplace；默认建议先私有，验证好再公开。
5. （可选）**绑 research**：要不要绑几个已建好的 research agent 喂它？把 id 要过来。
6. （可选）**真实交易加风控**：真实交易 PM 默认加 `--overlay hard-stop`（硬止损纪律，防堆仓爆仓）。

> 名字你帮用户起：`<风格>·<目标> (<币种>)`，如「保守·稳健保值 (BTC)」。

### 一条命令建好（默认 mock 纸面凭证 + 自动启动）

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" pm create \
  --name "保守·稳健保值 (BTC)" \
  --symbol BTCUSDT \
  --goal steady \
  --style conservative \
  --overlay hard-stop \
  --public false
```

脚本自动做：探测启用 LLM → 建 mock 纸面交易凭证（**不碰真钱**）→ 从内嵌模板组装 system_prompt + risk_config → POST 创建 → 默认自动启 scheduler。返回 `agent_id` 和 `mock_auth_id`，**记下来**给用户。

### 建完立刻验证它在跑

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" pm status --agent <id>
```

看 `isSchedulerRunning=true` 且 `intervalMs=900000`（不是 3600000）。如果不是 running，补启：
```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" pm start --agent <id> --ms 900000
```

### 默认值（已替非开发用户踩过坑）

- `interval_ms=900000`（15min）：**总是显式传**。不传的话 steady/arbitrage 会默认 1h，用户会以为"数据两小时没更新"。
- `auto_start_scheduler=true`：建完即跑。批量建（≥5 个）才改 `--no-auto-start` + 错峰启动（见末尾）。
- mock 凭证：纸面交易，**每次新建**（旧的解密失败不可逆，别复用）。

---

## 二、创建 Research Agent

### 采访

1. **主题/名字**：研究什么？如「Crypto 资金面·OI 动向」。
2. **asset_type**：`Crypto` / `A-shares` / `HK stocks` / `US stocks`（**首字母大写带空格**，写 `crypto` 会 400）。
3. **analysis_type**：探测确认可选值；Crypto 通常是 `Quantitative Analysis` / `Capital Flow Analysis` / `Investment Research`。
4. **公开？**

### prompt 你来写（关键：显式约束数据源）

**用户不会写 prompt，你写。** 核心技巧（Creator Reconcile）：prompt 里**显式写「必用 X / 禁用 Y」会被严格遵守**，Creator 据此精确挑数据源。模板：

```
<研究目标 + 研究流程步骤>
【必用数据源】资金费率 / OI / 爆仓 …（要哪些写哪些）
【输出格式】结论 + 信号方向 + 置信度 + 关键价位
仅允许使用上文【必用数据源】列出的数据源；不得调用未列出的其他数据源
不得编造数据；数据缺失时明确说明
```

> 想知道当前哪些数据源稳定，先 `probe --asset-type Crypto` 看 datasource 清单，必要时问用户确认。

### 创建 + 等部署完成

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" research create \
  --name "Crypto·资金面 OI动向" \
  --asset-type Crypto \
  --analysis-type "Capital Flow Analysis" \
  --prompt "<上面写的完整 prompt>" \
  --public false \
  --wait 120        # 等 120s 部署到 deployed
```

`status=succeeded/deployed` + `endpoint_url` 有值 = 部署完成。把返回的 `agent_id` 给用户（建 PM 时可绑）。

---

## 三、看「我的 agent」信息 + 交易日志

### 列出我的 agent
```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" pm list --limit 50
```

### 看运行状态
```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" pm status --agent <id>
```
`phase`: idle → initializing → researching → decision → completed。

### 看每日盈亏（谁在亏）
```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" logs pnl-summary --agent <id>
```

### 看逐单盈亏（哪笔爆仓）
```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" logs pnl-orders --agent <id>
```

### 看当前持仓（有没有堆仓）
```bash
python3 "$CLAUDE_SKILL_DIR/scripts/flowmax.py" logs positions --agent <id>
```
健康 PM 持仓 1-3 个；≥6 个脚本会告警（说明 prompt 的克制被忽略）。

> 字段口径在不同部署可能变。表格解析不出来时加 `--raw` 看原始 JSON，再适配。

---

## 四、运维（用户明确要求时才做，先确认）

| 操作 | 命令 |
|---|---|
| 启 scheduler | `pm start --agent <id> --ms 900000` |
| 停 scheduler | `pm stop --agent <id>` |
| 改决策周期 | `pm set-interval --agent <id> --ms 1800000` |
| 改 prompt/风险档 | `pm update --agent <id> --field risk_config --style balanced` |
| 删除（先停后删） | `pm delete --agent <id> --mock-auth <mock_id>` |

**改 prompt / risk / interval 必须 `stop→PUT→start` 三步**（`pm update` / `pm set-interval` 已封装）。只 PUT 不重启 = 改了个寂寞，CF Worker 不自动重载。删 PM 路径**不带 `pm`**（`DELETE /agents/{id}`），删 PM 后才能删它绑的 mock 凭证（否则 409）。

---

## 必避的坑（创建/查看时高频踩中）

| 坑 | 现象 | 规避 |
|---|---|---|
| 用错 API 前缀 | `/api/market/*` 404 | 脚本已用 `/api/v1/*` 直连，别手改 |
| asset_type 大小写 | `crypto` → 400 | 必须 `"Crypto"`（首字母大写） |
| `MATICUSDT` 下架 | K 线 400 | 用 `POLUSDT` |
| 列 PM 用 `page_size` | 静默忽略，只回默认条数 | 脚本已用 `?limit=`（max 100） |
| scheduler/start 不带 body | 422 | 脚本已带 `{interval_ms}` |
| 改了配置不生效 | 还跑旧 prompt/interval | 必须 stop→PUT→start 三步 |
| mock 解密失败 | `Decryption failed (AES-GCM)` | 不可逆，删旧凭证、建新 mock（脚本默认新建） |
| list 有、详情 404 | `pnl/summary`、`status` 返回 not found | 已知 list/detail 缓存不一致；该 PM 仍能跑，日志查不到时反馈维护者 |

## 验证 checklist（建完一个 PM 后念一遍）

- [ ] `pm status` 显示 `isSchedulerRunning=true`
- [ ] `intervalMs=900000`（不是 3600000 / 300000）
- [ ] `logs positions` 持仓 ≤ 几个（不是一堆）
- [ ] 名字/币种/风格/档位符合用户要的

---

## 脚本速查（`scripts/flowmax.py`，stdlib only，`python3` 直跑）

```bash
export FLOWMAX_JWT="<你的 JWT>"          # 必须，只在自己 shell 里设
export FLOWMAX_BASE="https://market.prod.gcp.hubble-rpc.xyz"  # 可选，默认 staging

S="$CLAUDE_SKILL_DIR/scripts/flowmax.py"
python3 $S probe                                 # 探测合法值 + mock id + 内嵌模板
python3 $S research create --name .. --prompt .. # 建 research（单个 flags 或 --spec 批量）
python3 $S research get    --agent <id>          # 详情（看 reconcile 挑了哪些数据源）
python3 $S research job    --job <job_id>        # 部署进度
python3 $S pm create --name .. --symbol BTCUSDT --goal steady --style conservative
python3 $S pm start   --agent <id> --ms 900000
python3 $S pm stop    --agent <id>
python3 $S pm status  --agent <id>
python3 $S pm list    --limit 50
python3 $S pm set-interval --agent <id> --ms 1800000
python3 $S pm update  --agent <id> --field risk_config --style balanced
python3 $S pm delete  --agent <id> --mock-auth <mock_id>
python3 $S logs pnl-summary --agent <id>
python3 $S logs pnl-orders  --agent <id>
python3 $S logs positions   --agent <id>
python3 $S --help                                 # 全部子命令
```

> **批量建 ≥5 个 PM**：每个加 `--no-auto-start`，建完后**错峰串行启动**（N 个同 interval=T 的 PM，间隔 `T/N` 秒启动；别并行，否则周期边界对齐集中刷数据）。这是开发向场景，非开发用户一次建一个用默认 auto-start 即可。
