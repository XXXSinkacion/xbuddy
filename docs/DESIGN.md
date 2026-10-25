# 投资 X Buddy — 架构设计

> 面向投资研究者的个人 Agent 工作台：输入研究目标 → 规划任务 → 调用金融工具 → 管理上下文与长期状态 → 产出可验证研究成果 → 需要时请求用户确认。
> 本文档说明 Harness（运行机制）与产品体验（线程 / 任务 / 证据 / 产物 / 动态 UI）如何映射到投资研究流程。

## 1. 设计目标与约束

| 维度 | 目标 |
|---|---|
| 可运行主链路 | 工具注册 → 计划 → 逐任务执行 → 上下文压缩 → 记忆 → 检查点/恢复 → 报告 |
| 治理 | 工具风险分级、审批门控、停止规则、成本/时延预算、全链路可观测 |
| 合规 | 不输出确定性涨跌预测 / 收益承诺 / 买卖建议；事实 / 推断 / 不确定分层；结论回溯到原始字段（来源、时点、单位、口径） |
| 韧性 | 模型失败 → 备用模型 → 模板模式；工具失败 → 备用源 → 过期缓存（标记）→ 显式“缺失”，绝不静默生成“正常”结论 |
| 交付 | 单容器部署；无 Key 也可演示（模板模式 + 演示数据，且全程标注） |

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Web 工作台 (React + Vite + TS)                                            │
│  线程列表 │ 对话流(动态 UI 块) │ 任务面板 │ 证据面板 │ 产物 │ 观测 │ 记忆    │
│  审批卡片(批准/拒绝) · 停止/恢复 · 成本计量条                              │
└──────────────▲────────────────────────────────────────┬──────────────────┘
               │ SSE: /api/threads/{id}/stream           │ REST (JSON)
┌──────────────┴────────────────────────────────────────▼──────────────────┐
│ Harness (FastAPI, Python)                                                 │
│  Runtime 循环: PLAN → EXECUTE(task*) → SYNTHESIZE → DONE                   │
│   ├ ToolRegistry   工具元数据: schema / 风险级 / 成本 / 超时 / 可缓存 / 备用链 │
│   ├ Planner        LLM 规划 ⇄ 模板规划(降级)；产出任务 DAG + 验收标准         │
│   ├ Executor       单任务 ReAct 循环，步数/工具调用上限，循环检测              │
│   ├ Policy         风险分级 → 自动 / 审批；预算门控；停止规则                   │
│   ├ ContextManager token 预算 → 观察压缩(保留证据 ID) → 跨任务只传摘要          │
│   ├ Memory         工作记忆(run 内) + 长期记忆(SQLite FTS5；写入需审批)         │
│   ├ Checkpoint     每步落盘；进程重启→标记中断→用户一键恢复                      │
│   ├ EvidenceStore  每次工具返回 → 证据记录(来源/时点/单位/口径/状态/哈希)        │
│   ├ Synthesizer    报告(含 [E#] 引用) → 合规 Linter → 产物                     │
│   ├ Degradation    LLM 路由(主→备→模板)；工具备用链；熔断器；重试退避             │
│   └ Tracing        span(llm/tool/task) 时延、token、成本；JSONL 日志           │
│  持久化: SQLite(threads/runs/tasks/events/evidence/artifacts/approvals/     │
│          memories/checkpoints/messages)                                    │
└──────────────┬────────────────────────────────┬──────────────────────────┘
               │ REST + X-api-key               │ MCP (streamable HTTP)
      扶摇金融数据 API                     iFinD MCP(可选，需密钥)
```

## 3. 技术选型与理由

- **后端 Python 3.11+ / FastAPI**：异步、SSE 简单、金融数据处理生态好；内置 `sqlite3` 持久化，零运维；`httpx` 调扶摇 REST；`openai` SDK 覆盖 OpenAI 兼容厂商（DeepSeek / Qwen / Kimi / GLM / OpenAI / OpenRouter），`anthropic` SDK 可选；`mcp` SDK 接 iFinD MCP。
- **前端 React 18 + Vite + TypeScript**：动态 UI 块按类型渲染；`lightweight-charts` 画 K 线；`react-markdown` 渲染报告。
- **单容器交付**：多阶段 Dockerfile 构建前端并由 FastAPI 托管静态文件；任何支持 Docker 的平台（Zeabur / Railway / Render / 自有服务器）均可一键部署。

## 4. 数据模型（SQLite）

| 表 | 关键字段 | 作用 |
|---|---|---|
| threads | id, title, settings_json | 研究线程（多轮目标共享上下文与记忆） |
| messages | thread_id, run_id, role, blocks_json | 对话流（用户目标 / 助手动态 UI 块） |
| runs | thread_id, goal, status, phase, budget_json, usage_json, stop_reason, state_json | 一次研究运行（state_json 即最新检查点） |
| tasks | run_id, idx, title, objective, depends_on, allowed_tools, acceptance, status, result_json | 计划中的任务 |
| events | run_id, seq, type, payload_json | 事件溯源日志（SSE 回放、观测） |
| evidence | run_id, task_id, label(E#), tool, args_json, source, fetched_at, data_timestamp, unit_notes, status, summary, raw_json, raw_hash, request_id | 证据记录（结论可回溯） |
| artifacts | run_id, type, title, content_md, blocks_json, lint_json, version | 产物（报告 / 表格 / 图） |
| approvals | run_id, task_id, kind, payload_json, status, reason | 审批请求与决定 |
| memories (+FTS5) | scope, kind, content, tags, source_run_id | 长期记忆 |
| checkpoints | run_id, step, state_json | 检查点历史 |

run.status: `planning | waiting_approval | running | interrupted | completed | failed | stopped`
run.phase: `plan | execute | synthesize | done`

## 5. 运行循环（Harness.run）

```
loop:
  check_stop_rules()                    # 预算 / 时长 / 步数 / 用户停止
  if phase == plan:
      memory_ctx = memory.recall(goal)  # 长期记忆检索
      plan = planner.plan(goal, memory_ctx)   # LLM；失败 → 模板规划
      save tasks; checkpoint()
      if policy.plan_needs_approval(plan): request_approval(kind=plan) → return(挂起)
      phase = execute
  if phase == execute:
      task = next_ready_task()          # 依赖满足的第一个 pending
      if none: phase = synthesize; continue
      executor.run(task):               # 有界 ReAct 循环
          llm → tool_calls → policy.check(tool) [可能挂起审批]
          → tool.invoke (缓存 / 熔断 / 重试 / 备用链) → evidence.record
          → context.append + compress_if_needed → checkpoint()
      task.result = {summary, evidence_ids, blocks}
  if phase == synthesize:
      report = synthesizer.write(goal, tasks, evidence)   # LLM；失败 → 模板报告
      report = linter.enforce(report, evidence)           # 合规与引用校验
      artifact.save; memory.propose_writes() [审批]
      phase = done; status = completed
```

挂起（审批 / 中断）后，`resume(run_id)` 从最近检查点继续；检查点包含 phase、当前任务、执行器消息列表、已用预算与工作记忆。

## 6. 权限、审批与停止规则

**工具风险分级**

| 级别 | 含义 | 处理 |
|---|---|---|
| L0 read | 轻量只读（检索、日历、快照） | 自动 |
| L1 read-costly | 大数据量 / 外部源（历史 K 线、MCP） | 自动，计入预算 |
| L2 write | 产生副作用（写长期记忆、加自选、导出） | **需审批** |
| L3 external | 未知 / 第三方 | 默认需审批 |

**审批类型**：`plan`（计划确认，模式 always / auto / never；auto = 任务数 > 6 或含 L2 工具时触发）、`tool`（L2 / L3 工具调用）、`budget`（成本达到预算 80% 时询问是否继续）、`memory_write`（报告后写入长期记忆）。

**停止规则**：max_llm_steps、max_tool_calls、max_cost_usd、max_duration_s、连续工具失败 N 次 → 任务失败并继续下一任务、重复相同工具 + 参数 → 拦截并提示模型、用户停止。触发后进入 `stopped / failed`，仍产出带“未完成”标记的部分报告。

## 7. 上下文管理与压缩

- 工具原始返回全部进入证据库；给模型看的只是**紧凑渲染**（前 N 行 + 统计摘要 + `E#` 引用），模型可用 `get_evidence(E#)` 按需取全文（渐进披露）。
- 每个任务独立消息列表；跨任务只传递任务结果摘要与工作记忆，不传原始观察。
- 每次追加后估算 token（中文 ≈ 1 token / 1.5 字，英文 ≈ 1 token / 4 字符），超过 `context_budget_tokens` 时把最旧的观察折叠为一条“已压缩观察摘要”（保留证据 ID 列表与一句话摘要），保留最近 4 条消息；发出 `context.compressed` 事件（压缩前后 token）。

## 8. 记忆

- **工作记忆**：run 内键值事实（任务摘要、已解析的标的代码等），随检查点持久化。
- **线程记忆**：同一线程历史运行的报告摘要，后续目标自动带入。
- **长期记忆**：SQLite + FTS5 关键词检索；类型 preference / watchlist / conclusion / note；写入走审批；UI 可查看 / 删除；全部为匿名构造数据，不含真实持仓。

## 9. 检查点与恢复

- 每个执行步骤后 `checkpoint()` 落盘（state_json）。
- 进程启动时把 `running` 的 run 标记为 `interrupted`，UI 显示“恢复”按钮；恢复从检查点继续，已完成任务不重跑，同参数证据命中缓存不重复抓取。
- 审批挂起本质也是检查点 + 状态机，决定后自动恢复。

## 10. 降级与恢复

| 故障 | 处理 |
|---|---|
| 主模型 429 / 5xx / 超时 | 指数退避重试 2 次 → 备用模型 → 模板模式（规则规划 + 确定性报告） |
| 模型未配置 | 直接模板模式，UI 显著提示 |
| 扶摇 429 / code=4001 | 退避重试；熔断器（60s 内 3 次失败 → 断开 30s） |
| 扶摇 5002 / 5003 / 网络 | 备用源（iFinD MCP 若配置）→ 过期缓存（标 `stale` + 原时点）→ `missing` 证据 |
| 数据缺失 / 字段 null | 证据状态 `partial / missing`，报告“数据缺失与限制”章节自动列出，结论不得引用缺失字段 |
| 工具参数错误 (1001-1004) | 错误原文回给模型一次自我修正；仍失败 → 任务失败 |

## 11. 可观测性

- 事件总线：所有状态变化即事件（`run.*`、`task.*`、`tool.*`、`llm.*`、`approval.*`、`context.*`、`evidence.*`、`artifact.*`），SSE 推送并落库，可回放。
- span：每次 LLM / 工具调用记录 duration_ms、tokens、估算成本、状态；run 聚合 usage。
- UI 观测页：时间线、各任务耗时 / 成本、停止规则余量；`/api/runs/{id}/trace`；`logs/events.jsonl`。

## 12. 合规闸门（Linter）

1. 禁止词模式（必涨 / 必跌 / 稳赚 / 保证收益 / 建议买入 / 卖出 / 目标价 / 抄底 / 清仓 / 一定能 …）→ 模型重写一次 → 仍命中则替换为“[已按合规规则移除]”并标记。
2. 结论每条必须带 `[E#]` 引用且 E# 存在；引用缺失的结论降级为“不确定”。
3. 报告固定章节：摘要 / 核心结论（事实 · 推断 · 不确定 分层）/ 数据口径与时点 / 数据缺失与限制 / 证据清单 / 免责声明。
4. 证据状态非 ok 的数据自动进入“缺失与限制”。

## 13. 产品映射：投资研究流程 ↔ 工作台

| 研究流程 | 工作台对象 | 体现 |
|---|---|---|
| 一个研究课题（多轮） | 线程 Thread | 左栏；共享记忆与历史报告 |
| 研究计划 / 分工 | 任务 Task | 计划卡片 + 任务面板（状态 / 依赖 / 验收） |
| 取数与核验 | 证据 Evidence | 工具调用芯片 → 证据面板（来源 / 时点 / 单位 / 原始 JSON） |
| 研究结论 | 产物 Artifact | 报告（可下载 md / json），引用可点回证据 |
| 关键决策点 | 审批 Approval | 计划确认、写记忆、预算延长 |
| 动态呈现 | UI 块 | metrics / table / kline / bar / callout(事实 · 推断 · 不确定 · 缺失) |

## 14. 已知边界

- 扶摇历史 K 线仅日线；资金流向 / 高频等端外未开放；估值仅最新快照无历史分位。
- iFinD MCP 需同花顺 iFinD 账号密钥，未配置时相关工具不注册。
- 成本按配置单价估算，非账单口径。
- 单进程内运行，重启靠检查点恢复；未做多实例调度。
