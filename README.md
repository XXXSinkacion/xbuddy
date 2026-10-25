# 投资 X Buddy — 面向投资研究者的个人 Agent 工作台

> 题目 13｜给出一个研究目标，系统规划任务 → 调用金融工具（扶摇 / iFinD MCP）→ 管理上下文与长期记忆 → 在需要时请求确认 → 产出每条结论都能回溯到原始字段的研究报告。
> 同时展示 Agent Harness 的运行机制：能力注册、计划执行、上下文压缩、记忆、检查点与恢复、权限审批、停止规则、成本/时延预算、可观测性、降级与恢复。

- **在线体验**：`<部署后填写 URL>`（部署方式见下文「部署」）
- **架构设计**：[docs/DESIGN.md](docs/DESIGN.md)
- **AI 使用与验证记录**：[docs/AI_USAGE.md](docs/AI_USAGE.md)
- **测试说明**：[docs/TESTING.md](docs/TESTING.md)

---

## 1. 产品选择：做什么、不做什么

| 选择 | 说明 |
|---|---|
| 核心任务 | “个股 / 板块研究简报”：解析标的 → 行情与 K 线统计 → 估值 → 财务指标与报表 → 板块对比 → （可选）市场情绪 → 带引用的报告 |
| 用户 | 个人投资研究者；输入自然语言研究目标，得到可核验的研究过程与结果 |
| 不做 | 不预测涨跌、不给目标价、不给买卖 / 加减仓建议、不承诺收益（系统提示 + 合规 Linter 双重约束） |
| 数据 | 优先扶摇金融数据 API（REST）；iFinD MCP 作为可选扩展源（配置密钥后自动注册其工具） |
| 无 Key 也能跑 | 未配模型 → **模板模式**（规则规划 + 确定性流水线 + 模板报告）；未配扶摇 Key → **演示数据（fixture）**，证据与报告中全程标注“演示数据，非真实行情” |

## 2. 快速开始

### 2.1 Docker（推荐，一条命令）

```bash
cp .env.example .env        # 按需填入 LLM / 扶摇 / iFinD 密钥；不填也能以降级模式运行
docker compose up --build   # 打开 http://localhost:8000
```

### 2.2 本地开发

```bash
# 后端（Python 3.11+）
cd backend && pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000

# 前端（Node 18+），另开终端；开发服务器把 /api 代理到 8000
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Windows 可直接运行 `powershell -ExecutionPolicy Bypass -File scripts/dev.ps1`。
生产方式（无 Docker）：`bash scripts/build_and_run.sh`（构建前端后由 FastAPI 单进程托管）。

### 2.3 运行测试

```bash
cd backend && python -m pytest -q      # 23 个用例，约 25s，全部离线（fixture + mock LLM）
```

## 3. 环境变量（.env）

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_PROVIDER` | `openai_compat`（DeepSeek / Qwen / Kimi / GLM / OpenAI / OpenRouter）、`anthropic`、`none` | `none` → 模板模式 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 主模型 | DeepSeek 示例 |
| `LLM_PRICE_INPUT_PER_M` / `LLM_PRICE_OUTPUT_PER_M` | 成本估算单价（美元 / 百万 token） | 0.27 / 1.10 |
| `LLM_FALLBACK_*` | 备用模型（主模型 429 / 5xx / 超时后切换） | 空 |
| `FUYAO_API_KEY` | 扶摇 API Key（https://fuyao.aicubes.cn/admin/ 用同花顺账号签发） | 空 → fixture |
| `FUYAO_MODE` | `auto` / `live` / `fixture` | `auto` |
| `IFIND_MCP_URL` / `IFIND_MCP_AUTHORIZATION` | iFinD MCP 地址与 Authorization 头（iFinD 终端 / mcp.51ifind.com「一键复制」） | 空 → 不注册 |
| `HARNESS_MAX_LLM_STEPS` / `HARNESS_MAX_TOOL_CALLS` / `HARNESS_MAX_COST_USD` / `HARNESS_MAX_DURATION_S` | 停止规则默认值（UI 可逐次覆盖） | 40 / 60 / 0.5 / 600 |
| `HARNESS_CONTEXT_BUDGET_TOKENS` | 单任务上下文预算，超出即压缩 | 24000 |
| `HARNESS_PLAN_APPROVAL` | 计划确认：`always` / `auto` / `never` | `auto` |
| `HARNESS_MEMORY_WRITE_APPROVAL` | 写长期记忆是否需审批 | `true` |
| `ENABLE_FAULT_INJECTION` | 允许通过 UI 注入故障（演示降级用） | `true` |
| `DATA_DIR` / `PORT` | SQLite 与日志目录 / 端口 | `./data` / `8000` |

**不要把密钥提交到仓库**：`.env` 已在 `.gitignore` 中。

## 4. 一次研究是怎么跑的（主链路）

```
用户目标 ──► Planner（LLM JSON 计划 ⇄ 模板规划）──► [计划确认?] ──► 逐任务 Executor
                                                                   │ 每步：策略检查 → 工具调用(缓存/熔断/重试/备用链)
                                                                   │       → 证据记录(E#) → UI 块 → 上下文压缩 → 检查点
                                                                   ▼
                      长期记忆 ◄─[审批]─ Synthesizer（报告 + 引用）──► 合规 Linter ──► 产物(报告 .md / 证据包 .json)
```

- **能力 / 工具注册**：`backend/app/tools/registry.py`。每个工具带 JSON Schema、风险级（L0/L1 自动，L2/L3 审批）、来源、超时、缓存、备用链、相对成本。扶摇 18 个工具 + 本地分析 4 个 + 记忆 3 个；iFinD MCP 工具在启动时动态注册为 `ifind_*`。
- **计划执行**：`harness/planner.py`、`harness/executor.py`、`harness/runtime.py`。LLM 模式为有界 ReAct 循环；模板模式按确定性流水线（`$target0`、`$d90`、`$report0` 等占位符由工作记忆解析）。
- **上下文压缩**：`harness/context.py`。工具原始返回只进证据库，模型看到的是紧凑渲染 + `E#`；超预算时把最旧的「assistant+tool」组折叠为摘要并保留证据编号（可用 `get_evidence` 取回）。
- **记忆**：`harness/memory.py`。工作记忆（run 内）、线程记忆（历史运行摘要自动带入）、长期记忆（SQLite FTS5，匿名构造数据，写入需审批，UI 可增删）。
- **检查点与恢复**：每步 `checkpoint()` 落盘；进程重启把 running 标为 `interrupted`，UI 一键「从检查点恢复」，已完成任务不重跑，证据编号连续。
- **权限与审批**：`harness/policy.py`。计划确认、L2 工具（写记忆 / 加自选）、预算 80% 预警、报告后写记忆，四类审批卡片；拒绝有明确后果（停止并出部分报告 / 跳过工具 / 停止）。
- **停止规则**：模型步数、工具调用数、成本、时长、连续工具失败、重复调用拦截、用户停止；触发后仍产出标注“未完成”的部分报告。
- **成本 / 时延 / 可观测性**：所有状态变化即事件（SSE 推送 + SQLite 落库 + `data/logs/events.jsonl`）；`/api/runs/{id}/trace` 聚合 span；UI「观测」页显示 token / 成本 / 时延 / 余量 / 熔断 / 压缩。
- **降级与恢复**：主模型 → 退避重试 → 备用模型 → 模板模式；数据源 → 重试 → 熔断 → 备用链 → 过期缓存（标 stale）→ 显式 `missing`；任何缺失都进入报告「数据缺失与限制」，绝不静默“正常”。熔断器分两级：数据源级只统计传输层失败（超时 / 网络 / 5xx / 429），端点级按“整次调用失败”统计，因此某个端点的业务错误（如未披露报告期返回 5003）不会连坐同数据源的其它端点。

## 5. 产品映射：线程 / 任务 / 证据 / 产物 / 动态 UI

| 投研流程 | 工作台 | 位置 |
|---|---|---|
| 一个研究课题（可多轮追问） | 线程 | 左栏 |
| 计划与分工 | 任务（状态 / 依赖 / 验收 / 工具） | 对话流计划卡片 + 右栏「任务」 |
| 取数与核验 | 证据 E#（来源 / 时点 / 单位口径 / 原始 JSON / 哈希 / request_id） | 右栏「证据」+ 点击任意 [E#] |
| 结论 | 产物（报告 .md、证据包 .json） | 右栏「产物」/ 对话流报告卡片 |
| 关键决策 | 审批卡片（批准 / 拒绝 + 理由） | 对话流 |
| 动态呈现 | metrics / table / kline / callout(事实·推断·不确定·缺失·错误) / plan / approval / artifact | 对话流 |
| 运行治理 | 状态芯片、成本 / 工具 / 模型步数计量条、停止 / 恢复、故障注入开关 | 顶栏 |

## 6. 合规闸门（`compliance/linter.py`）

1. 禁止词模式（必涨 / 必跌 / 稳赚 / 保证收益 / 建议买入 / 卖出 / 目标价 / 抄底 / 清仓 / 将大幅上涨 …）→ 模型重写一次 → 仍命中则替换为 `[已按合规规则移除]` 并记录。
2. 「核心结论」每条必须带 `[E#]` 且 E# 存在；无引用 → 自动前缀【不确定·无证据引用】；无效引用 → `[引用无效]`。
3. 固定章节补齐：数据口径与时点 / 数据缺失与限制（由证据状态自动生成，含 fixture 提示）/ 证据清单 / 免责声明。

## 7. 数据来源与口径

- **扶摇金融数据 API**（`https://fuyao.aicubes.cn`，`X-api-key`）：标的检索、交易日历、行情快照、日 K（前复权 / 后复权 / 不复权）、指数快照与日 K、估值快照（PE TTM/MRQ、PB、PS、PCF）、财务指标（五类）、三大报表、同花顺指数目录与成分股、涨跌停池、涨停统计、热股榜、飙升榜。所有金额单位元、时间戳毫秒（Asia/Shanghai）、涨跌幅为百分数值；每条证据记录 `data.timestamp` 作为数据时点，接口未提供时以抓取时间标注。
- **iFinD MCP**（可选）：配置后动态注册其全部工具（L1，外部源），返回原样入证据。
- **本地分析**：区间收益 / 最大回撤 / 年化波动（√242）/ MA20 偏离 / 涨跌天数，均为历史统计，口径写在证据 `unit_notes`。
- **演示数据（fixture）**：无扶摇 Key 时由标的代码种子的伪随机过程生成，仅用于功能演示，所有证据 `source` 含“演示数据”，报告自动提示；演示数据运行写入的长期记忆也带“演示数据运行”标注。
- **live 实测**：2026-10-25 用真实 Key 跑通全部 19 个扶摇工具（`python scripts/live_probe.py`），报告中的最新价 / PE(TTM) / 报告期与扶摇返回逐项一致；三大报表字段名以实测返回为准（见 `tools/fuyao.py` 注释）。

## 8. 已知边界与未做事项

- 扶摇当前仅日线 K 线、估值仅最新快照（无历史分位）；资金流向 / 高频等端外未开放，未接入。
- 扶摇对尚未披露的报告期返回 `code=5003 Data source unavailable`（实测 2026-10-25 请求 2026-3），无法区分“未披露”与“上游故障”；产品按错误如实记录并回退到上一报告期，报告中标注“无法确认三季报是否已披露”。
- 非交易日请求涨停池时自动回退到最近交易日，并把所用日期写入口径；其它“当日”类接口按扶摇返回的时点如实标注。
- iFinD MCP 需同花顺 iFinD 账号密钥；本仓库只实现通用 MCP 桥接（streamable HTTP / SSE 自动尝试），未与真实服务端联调，也未针对其工具做专门摘要与 UI 块。
- Anthropic 客户端按官方 SDK 编写并有单元覆盖，但 live 验证只用 DeepSeek（OpenAI 兼容）做过。
- 成本按配置单价估算，非账单口径；时延为服务端观测值。
- 单进程、SQLite；多实例调度、用户登录与多租户隔离未做。
- 模板模式的报告是证据摘要的确定性拼装，不做文字归纳（这是刻意的：模型不可用时不编造）。
- 长期记忆为关键词检索（FTS5），未做向量检索；记忆内容全部为匿名构造数据。
- 未做演示视频（可选项）。

## 9. 目录结构

```
backend/app/
  main.py            FastAPI 路由 + SSE + 静态托管     factory.py  装配
  config.py db.py events.py models.py
  harness/  runtime(状态机/检查点/审批) planner executor synthesizer context policy memory evidence blocks
  llm/      base router(重试/备用) openai_compat anthropic_client mock
  tools/    registry(注册/调用管线) fuyao fixtures analysis memory_tools ifind cache(缓存/熔断/故障注入)
  compliance/linter.py
backend/tests/       23 个用例（主链路 / 审批与停止 / 失败降级 / 检查点恢复 / 单元 / HTTP）
frontend/src/        React 工作台：App、Blocks(动态 UI)、Panels(任务/证据/产物/观测/记忆)、KlineChart、SSE hook
docs/                DESIGN.md AI_USAGE.md TESTING.md
Dockerfile docker-compose.yml scripts/
```

## 10. 部署

任何支持 Docker 的平台均可：`docker build -t xbuddy . && docker run -p 8000:8000 --env-file .env -v xbuddy-data:/data xbuddy`。
Zeabur / Railway / Render 选择「Dockerfile 部署」，在平台环境变量里填入 `.env` 中的键值（勿提交 `.env`），持久化 `/data` 目录。

## 11. 免责声明

本项目为技术评测作品，输出内容为数据整理与研究过程记录，不构成任何投资建议；数据以原始数据源与官方披露为准。
