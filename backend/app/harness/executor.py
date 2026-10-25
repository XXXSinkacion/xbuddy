"""任务执行器：LLM 模式为有界 ReAct 循环；模板模式按确定性流水线执行。
两种模式都经过同一条工具管线（策略 → 调用 → 证据 → UI 块 → 检查点）。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from ..llm.base import LLMUnavailable
from ..llm.router import LLMRouter
from ..models import PlanTask, RunState, TaskResult, ToolResult
from ..tools.registry import ToolContext, ToolRegistry, ToolRunner, args_signature
from .blocks import blocks_from_result
from .context import ContextManager, render_tool_result
from .planner import date_days_ago, latest_report_period, today_str
from .policy import Policy

log = logging.getLogger("xbuddy.executor")

EXECUTOR_SYSTEM = """你是「投资 X Buddy」的研究执行器，负责完成一个具体研究任务。
工作方式：
- 先调用工具取数，再基于返回的证据写任务小结。每条工具返回都带有证据编号 E#，小结中引用数据时必须写明 [E#]。
- 不确定标的代码时先用 search_ticker；一次只做本任务范围内的事，不要越界。互不依赖的取数可以在同一轮里并行调用多个工具。
- 只有数据工具返回的 E# 才是证据；note_fact / get_evidence / 记忆工具不产生新证据，引用时写原始 E#。
- 工具返回“数据缺失 / 失败 / 过期缓存”时，如实写入小结，不得编造数值，不得默认“正常”。
- 严禁输出涨跌预测、目标价、收益承诺或买入/卖出/加减仓建议；只描述事实与可验证的推断，并用【事实】【推断】【不确定】标注。
- 小结格式：3~8 条要点，每条带 [E#]；最后一行写“证据：E1, E2, …”。
- 若已获得足够证据，直接给出小结，不要再调用工具。"""


class NeedsApproval(Exception):
    def __init__(self, approval: dict):
        super().__init__(approval.get("title", "需要审批"))
        self.approval = approval


class StopRun(Exception):
    def __init__(self, reason: str, *, status: str = "stopped"):
        super().__init__(reason)
        self.reason = reason
        self.status = status


GateFn = Callable[[RunState], Awaitable[None]]  # 预算 / 停止检查，可能抛出 NeedsApproval / StopRun
ApprovalFn = Callable[[str, str, dict], dict]  # (kind, title, payload) -> approval row
CheckpointFn = Callable[[RunState], None]


class TaskExecutor:
    def __init__(
        self,
        *,
        llm: Optional[LLMRouter],
        registry: ToolRegistry,
        runner: ToolRunner,
        policy: Policy,
        ctx_mgr: ContextManager,
        emit: Callable[[str, dict], None],
        make_ctx: Callable[[Optional[str], RunState], ToolContext],
        gate: GateFn,
        request_approval: ApprovalFn,
        checkpoint: CheckpointFn,
    ):
        self.llm = llm
        self.registry = registry
        self.runner = runner
        self.policy = policy
        self.ctx_mgr = ctx_mgr
        self.emit = emit
        self.make_ctx = make_ctx
        self.gate = gate
        self.request_approval = request_approval
        self.checkpoint = checkpoint

    # ------------------------------------------------------------------ public
    async def run_task(self, task: PlanTask, state: RunState, *, goal: str) -> TaskResult:
        if state.mode == "template" or self.llm is None or not self.llm.available:
            if task.pipeline:
                return await self._run_pipeline(task, state)
            # LLM 规划但模型后来不可用：没有流水线可跑
            return TaskResult(status="failed", summary=f"模型不可用且任务「{task.title}」无确定性流水线，已跳过。", evidence_labels=list(state.task_evidence), blocks=[{"type": "callout", "level": "error", "text": "模型不可用，任务跳过", "evidence": None}])
        try:
            return await self._run_llm(task, state, goal=goal)
        except LLMUnavailable as e:
            self.emit("run.degraded", {"reason": f"执行阶段模型不可用：{e}，切换为模板模式", "task_id": task.id})
            state.mode = "template"
            state.degrade_reasons.append(f"任务 {task.id}：{e}")
            if task.pipeline:
                return await self._run_pipeline(task, state)
            return TaskResult(status="partial", summary=f"模型不可用（{e}），仅保留已获取证据。", evidence_labels=list(state.task_evidence), blocks=list(state.task_blocks))

    # --------------------------------------------------------------- LLM 模式
    def _init_messages(self, task: PlanTask, state: RunState, goal: str) -> list[dict]:
        wm = "\n".join(f"- {k}: {str(v)[:200]}" for k, v in state.working_memory.items()) or "（空）"
        prior = "\n".join(f"- {s['title']}：{s['summary'][:300]}" for s in state.task_summaries) or "（无）"
        tools_txt = self.registry.catalog_text(task.tools or None)
        user = (
            f"今天：{today_str()}（Asia/Shanghai）\n总体研究目标：{goal}\n\n"
            f"当前任务：{task.title}\n目标：{task.objective}\n验收标准：{task.acceptance or '获得相关证据并给出小结'}\n\n"
            f"工作记忆：\n{wm}\n\n已完成任务摘要：\n{prior}\n\n本任务可用工具：\n{tools_txt}\n\n"
            "请开始执行：先调用必要的工具，然后给出带 [E#] 引用的任务小结。"
        )
        return [{"role": "system", "content": EXECUTOR_SYSTEM}, {"role": "user", "content": user}]

    async def _run_llm(self, task: PlanTask, state: RunState, *, goal: str) -> TaskResult:
        assert self.llm is not None
        if not state.task_messages:
            state.task_messages = self._init_messages(task, state, goal)
        allowed = task.tools or None
        tools_schema = self.registry.llm_tools(allowed) if allowed else self.registry.llm_tools()
        nudged = False
        while True:
            await self.gate(state)
            # 恢复：有等待审批的工具调用
            if state.pending_tool_call:
                await self._resume_pending(task, state)
                continue
            if self.policy.task_step_exceeded(state):
                return self._finalize(task, state, status="partial", note=f"任务步数达到上限 {self.policy.budget.max_task_steps}，基于现有证据结束。")
            if self.policy.consecutive_errors_exceeded(state):
                if nudged:
                    return self._finalize(task, state, status="failed", note="连续工具失败，任务失败。")
                state.task_messages.append({"role": "user", "content": "连续多次工具调用失败。请不要再调用工具，基于已有证据（如有）直接给出小结，并明确说明缺失。"})
                nudged = True
                state.task_consecutive_errors = 0

            # token / 成本 / 时延由 runtime 的统一 emitter 依据 llm.end 事件计入，此处不重复累计
            resp = await self.llm.chat(state.task_messages, tools_schema, purpose=f"task:{task.id}", on_event=self.emit, temperature=0.2, max_tokens=1800)
            state.task_steps += 1
            state.step += 1
            state.task_messages.append(resp.to_message())

            if not resp.tool_calls:
                summary = (resp.content or "").strip() or "（模型未给出小结）"
                status = "done"
                # 不得静默生成“正常”结论：没有任何成功证据的任务不能标为完成
                if not state.task_ok_evidence:
                    status = "partial"
                    if state.task_call_signatures:
                        summary += "\n【系统】本任务的数据工具调用均未成功获取数据（失败 / 缺失），上文中的事实性表述缺少证据支撑。"
                    else:
                        summary += "\n【系统】本任务未调用任何数据工具，小结没有证据支撑，仅供参考。"
                self.checkpoint(state)
                return TaskResult(status=status, summary=summary, evidence_labels=list(state.task_evidence), blocks=list(state.task_blocks))

            if resp.content:
                self.emit("task.note", {"task_id": task.id, "text": resp.content[:400]})
            pending = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in resp.tool_calls]
            await self._process_tool_calls(task, state, pending)
            self._after_tools(state)

    async def _resume_pending(self, task: PlanTask, state: RunState) -> None:
        p = state.pending_tool_call or {}
        decision = p.get("decision")
        remaining = list(p.get("remaining") or [])
        if decision is None:
            # 不应发生：没有决定却在运行。视为拒绝以保证安全。
            decision = False
        state.pending_tool_call = None
        if decision:
            await self._execute_one(task, state, p)
        else:
            state.task_messages.append({"role": "tool", "tool_call_id": p["id"], "name": p["name"], "content": f"用户拒绝执行工具 {p['name']}（{p.get('reason') or '无说明'}）。请不要重试该操作，继续完成任务。"})
            self.emit("tool.rejected", {"tool": p["name"], "task_id": task.id, "reason": p.get("reason")})
        if remaining:
            await self._process_tool_calls(task, state, remaining)
        self._after_tools(state)

    async def _process_tool_calls(self, task: PlanTask, state: RunState, calls: list[dict]) -> None:
        """同一轮的自动放行（L0/L1）调用并发执行以降低时延；遇到需审批的调用先把前面的批次执行完再挂起。"""
        batch: list[dict] = []
        for i, tc in enumerate(calls):
            spec = self.registry.get(tc["name"])
            if self.policy.tool_needs_approval(spec):
                await self._execute_batch(task, state, batch)
                batch = []
                state.pending_tool_call = {**tc, "remaining": calls[i + 1 :], "decision": None}
                self.checkpoint(state)
                approval = self.request_approval(
                    "tool",
                    f"工具 {tc['name']} 需要你的确认",
                    {"tool": tc["name"], "arguments": tc["arguments"], "risk": spec.risk if spec else "L3", "risk_label": spec.public()["risk_label"] if spec else "", "description": spec.description if spec else "", "task_id": task.id},
                )
                raise NeedsApproval(approval)
            batch.append(tc)
        await self._execute_batch(task, state, batch)

    async def _execute_batch(self, task: PlanTask, state: RunState, calls: list[dict]) -> None:
        if not calls:
            return
        # 1) 按原顺序做循环拦截（确定性）；2) 可执行的并发调用；3) 按原顺序回填 tool 消息与证据块
        plan: list[tuple[dict, Optional[tuple[str, dict]]]] = []
        for tc in calls:
            name, args = tc["name"], dict(tc.get("arguments") or {})
            sig = args_signature(name, args)
            if self.policy.is_loop(state, sig):
                plan.append((tc, None))
                continue
            state.task_call_signatures.append(sig)
            plan.append((tc, (name, args)))
        runnable = [p for p in plan if p[1] is not None]
        ctx = self.make_ctx(task.id, state)
        if len(runnable) > 1:
            self.emit("tool.parallel", {"task_id": task.id, "tools": [p[1][0] for p in runnable]})
        results = await asyncio.gather(*[self.runner.invoke(p[1][0], p[1][1], ctx) for p in runnable])
        it = iter(results)
        for tc, call in plan:
            if call is None:
                state.task_messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": "检测到与之前完全相同的工具调用（重复 ≥2 次），已拦截。请改用不同参数或基于已有证据作结。"})
                self.emit("tool.loop_blocked", {"tool": tc["name"], "task_id": task.id})
                state.task_consecutive_errors += 1
                continue
            name = call[0]
            result, ev = next(it)
            label = ev["label"] if ev else None
            self._account_tool(state, result, label)
            state.task_messages.append({"role": "tool", "tool_call_id": tc["id"], "name": name, "content": render_tool_result(result, label)})
            if not result.meta.get("no_evidence"):
                state.task_blocks.extend(blocks_from_result(name, result, label))

    async def _execute_one(self, task: PlanTask, state: RunState, tc: dict) -> None:
        await self._execute_batch(task, state, [tc])

    def _after_tools(self, state: RunState) -> None:
        msgs, stats = self.ctx_mgr.compress_if_needed(state.task_messages)
        if stats:
            state.task_messages = msgs
            state.usage.compressions += 1
            state.usage.tokens_saved += max(0, stats["saved"])
            self.emit("context.compressed", stats)
        self.checkpoint(state)

    def _account_tool(self, state: RunState, result: ToolResult, label: Optional[str]) -> None:
        state.usage.tool_calls += 1
        state.usage.tool_latency_ms += int(result.meta.get("latency_ms") or 0)
        if result.meta.get("cached"):
            state.usage.tool_cache_hits += 1
        if label:
            state.task_evidence.append(label)
            if result.ok:
                state.task_ok_evidence.append(label)
        if result.ok:
            state.task_consecutive_errors = 0
        else:
            state.usage.tool_errors += 1
            if result.status != "missing":
                state.task_consecutive_errors += 1

    def _finalize(self, task: PlanTask, state: RunState, *, status: str, note: str) -> TaskResult:
        labels = list(state.task_evidence)
        summary = note + ("\n证据：" + ", ".join(labels) if labels else "\n（无成功证据）")
        self.checkpoint(state)
        return TaskResult(status=status, summary=summary, evidence_labels=labels, blocks=list(state.task_blocks))

    # ------------------------------------------------------------- 模板模式
    def _resolve(self, value: Any, state: RunState) -> Any:
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        wm = state.working_memory
        key = value[1:]
        targets: list[dict] = wm.get("targets") or []
        if key == "today":
            return today_str()
        if key.startswith("d") and key[1:].isdigit():
            return date_days_ago(int(key[1:]))
        if key.startswith("report") and key[6:].isdigit():
            seq = latest_report_period()
            idx = int(key[6:])
            return seq[idx] if idx < len(seq) else None
        if key == "targets_csv":
            return ",".join(t["thscode"] for t in targets) or None
        if key.startswith("target") and key[6:].isdigit():
            idx = int(key[6:])
            return targets[idx]["thscode"] if idx < len(targets) else None
        if key == "constituents_csv":
            cons = wm.get("constituents") or []
            return ",".join(c["thscode"] for c in cons[:20]) or None
        return wm.get(key)

    def _resolve_args(self, args: dict, state: RunState) -> tuple[Optional[dict], Optional[str]]:
        out = {}
        for k, v in args.items():
            r = self._resolve(v, state)
            if r is None and isinstance(v, str) and v.startswith("$"):
                return None, f"参数 {k} 依赖的 {v} 尚未就绪"
            out[k] = r
        return out, None

    def _save_step_output(self, step: dict, result: ToolResult, label: Optional[str], state: RunState) -> None:
        save_as = step.get("save_as")
        if not save_as or not result.ok:
            return
        wm = state.working_memory
        data = result.data
        items = data.get("item") if isinstance(data, dict) else None
        if save_as == "targets":
            pick = None
            for it in items or []:
                if it.get("asset_type") in ("a-share", "a-share-index"):
                    pick = it
                    break
            if pick is None and items:
                pick = items[0]
            if pick:
                lst = wm.setdefault("targets", [])
                if all(t["thscode"] != pick["thscode"] for t in lst):
                    lst.append({"thscode": pick["thscode"], "name": pick.get("name")})
        elif save_as == "sector_index":
            if items:
                wm["sector_index"] = items[0]["thscode"]
                wm["sector_index_name"] = items[0].get("name")
        elif save_as == "constituents":
            wm["constituents"] = [{"thscode": it["thscode"], "name": it.get("name")} for it in (items or [])]
        else:
            wm[save_as] = label

    async def _run_pipeline(self, task: PlanTask, state: RunState) -> TaskResult:
        steps = task.pipeline or []
        notes = state.task_notes  # 持久化在检查点中：中途恢复不会丢掉已完成步骤的小结
        while state.pipeline_index < len(steps):
            await self.gate(state)
            step = steps[state.pipeline_index]
            name = step["tool"]
            spec = self.registry.get(name)
            if spec is None:
                notes.append(f"工具 {name} 不存在，跳过")
                state.pipeline_index += 1
                continue
            args, err = self._resolve_args(dict(step.get("args") or {}), state)
            if args is None:
                if not step.get("optional"):
                    notes.append(f"步骤 {name} 跳过：{err}")
                state.pipeline_index += 1
                continue
            if self.policy.tool_needs_approval(spec):
                notes.append(f"步骤 {name} 需审批，模板模式下跳过")
                state.pipeline_index += 1
                continue
            ctx = self.make_ctx(task.id, state)
            result, ev = await self.runner.invoke(name, args, ctx)
            label = ev["label"] if ev else None
            self._account_tool(state, result, label)
            # 缺失时按 fallback_args 回退一次（例如报告期尚未披露）
            if result.status == "missing" and step.get("fallback_args"):
                for fb in step["fallback_args"]:
                    fb_args, ferr = self._resolve_args({**dict(step.get("args") or {}), **fb}, state)
                    if fb_args is None:
                        continue
                    self.emit("tool.fallback_args", {"tool": name, "args": fb_args, "task_id": task.id})
                    result, ev = await self.runner.invoke(name, fb_args, ctx)
                    label = ev["label"] if ev else None
                    self._account_tool(state, result, label)
                    if result.ok:
                        break
            if not result.meta.get("no_evidence"):
                state.task_blocks.extend(blocks_from_result(name, result, label))
            self._save_step_output(step, result, label, state)
            tag = "【事实】" if result.ok and result.status == "ok" else "【不确定】"
            notes.append(f"- {tag} {result.summary[:260]} [{label}]" if label else f"- {tag} {result.summary[:260]}")
            state.pipeline_index += 1
            state.step += 1
            self.checkpoint(state)
        labels = list(state.task_evidence)
        ok_count = sum(1 for n in notes if n.startswith("- 【事实】"))
        status = "done" if ok_count and ok_count >= max(1, len(labels) // 2) else ("partial" if labels else "failed")
        summary = "\n".join(notes) + ("\n证据：" + ", ".join(labels) if labels else "\n（无证据）")
        return TaskResult(status=status, summary=summary, evidence_labels=labels, blocks=list(state.task_blocks))
