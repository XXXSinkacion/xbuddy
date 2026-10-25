"""Harness 运行时：状态机（plan → execute → synthesize → done）、检查点 / 恢复、审批、停止、降级、观测。"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from ..compliance.linter import LintResult
from ..config import Settings
from ..db import Database, dumps, loads, new_id, now_ms
from ..events import EventBus
from ..llm.router import LLMRouter
from ..models import Budget, Plan, PlanTask, RunState, TaskResult
from ..tools.cache import CircuitBreaker, FaultInjector, TTLCache
from ..tools.registry import ToolContext, ToolRegistry, ToolRunner
from .context import ContextManager
from .evidence import EvidenceStore
from .executor import NeedsApproval, StopRun, TaskExecutor
from .memory import MemoryStore
from .planner import Planner
from .policy import Policy
from .synthesizer import Synthesizer, propose_memories

log = logging.getLogger("xbuddy.harness")

ACTIVE_STATUSES = ("planning", "running")


class Harness:
    def __init__(
        self,
        *,
        db: Database,
        bus: EventBus,
        registry: ToolRegistry,
        llm: Optional[LLMRouter],
        settings: Settings,
        faults: Optional[FaultInjector] = None,
    ):
        self.db = db
        self.bus = bus
        self.registry = registry
        self.runner = ToolRunner(registry)
        self.llm = llm
        self.settings = settings
        self.faults = faults or FaultInjector(enabled=settings.enable_fault_injection)
        self.cache = TTLCache()
        self.breaker = CircuitBreaker()
        self.evidence = EvidenceStore(db, bus)
        self.memory = MemoryStore(db)
        self.memory.seed_defaults()
        self.planner = Planner(llm, registry)
        self.synthesizer = Synthesizer(llm)
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_flags: set[str] = set()

    # ------------------------------------------------------------- 基础对象
    def default_budget(self) -> Budget:
        s = self.settings
        return Budget(max_llm_steps=s.harness_max_llm_steps, max_tool_calls=s.harness_max_tool_calls, max_cost_usd=s.harness_max_cost_usd, max_duration_s=s.harness_max_duration_s, context_budget_tokens=s.harness_context_budget_tokens, plan_approval=s.harness_plan_approval, memory_write_approval=s.harness_memory_write_approval)

    def create_thread(self, title: str) -> dict:
        row = {"id": new_id("thr"), "title": title.strip()[:80] or "新研究线程", "settings_json": "{}", "created_at": now_ms(), "updated_at": now_ms()}
        self.db.insert("threads", row)
        return row

    def add_message(self, thread_id: str, run_id: Optional[str], role: str, blocks: list[dict]) -> dict:
        row = {"id": new_id("msg"), "thread_id": thread_id, "run_id": run_id, "role": role, "blocks_json": dumps(blocks), "created_at": now_ms()}
        self.db.insert("messages", row)
        self.db.update("threads", thread_id, {"updated_at": now_ms()})
        pub = {**row, "blocks": blocks}
        pub.pop("blocks_json", None)
        self.bus.emit(thread_id, run_id, "message.created", pub)
        return pub

    def _emit(self, run: dict, type_: str, payload: Optional[dict] = None) -> None:
        self.bus.emit(run["thread_id"], run["id"], type_, payload or {})

    def get_run(self, run_id: str) -> Optional[dict]:
        run = self.db.get("runs", run_id)
        if not run:
            return None
        return self._public_run(run)

    def _public_run(self, run: dict) -> dict:
        state = RunState(**loads(run.get("state_json"), {}))
        budget = Budget(**loads(run.get("budget_json"), {}))
        policy = Policy(budget)
        tasks = self.db.fetchall("SELECT * FROM tasks WHERE run_id = ? ORDER BY idx", [run["id"]])
        approvals = self.db.fetchall("SELECT * FROM approvals WHERE run_id = ? ORDER BY created_at", [run["id"]])
        artifacts = self.db.fetchall("SELECT id, run_id, thread_id, type, title, version, created_at, lint_json FROM artifacts WHERE run_id = ? ORDER BY created_at", [run["id"]])
        return {
            "id": run["id"],
            "thread_id": run["thread_id"],
            "goal": run["goal"],
            "status": run["status"],
            "phase": run["phase"],
            "mode": state.mode,
            "stop_reason": run.get("stop_reason"),
            "error": run.get("error"),
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "finished_at": run.get("finished_at"),
            "budget": budget.model_dump(),
            "usage": state.usage.model_dump() | {"elapsed_ms": (run.get("finished_at") or now_ms()) - run["created_at"]},
            "remaining": policy.remaining(state),
            "degrade_reasons": state.degrade_reasons,
            "working_memory": state.working_memory,
            "step": state.step,
            "tasks": [self._public_task(t) for t in tasks],
            "approvals": [self._public_approval(a) for a in approvals],
            "artifacts": [a | {"lint": loads(a.pop("lint_json"), None)} for a in artifacts],
        }

    @staticmethod
    def _public_task(t: dict) -> dict:
        return {
            "id": t["id"],
            "run_id": t["run_id"],
            "idx": t["idx"],
            "title": t["title"],
            "objective": t["objective"],
            "depends_on": loads(t.get("depends_on_json"), []),
            "tools": loads(t.get("allowed_tools_json"), []),
            "acceptance": t.get("acceptance"),
            "status": t["status"],
            "result": loads(t.get("result_json"), None),
            "error": t.get("error"),
            "started_at": t.get("started_at"),
            "ended_at": t.get("ended_at"),
            "has_pipeline": bool(t.get("pipeline_json")),
        }

    @staticmethod
    def _public_approval(a: dict) -> dict:
        out = dict(a)
        out["payload"] = loads(out.pop("payload_json"), {})
        return out

    # ----------------------------------------------------------- 运行生命周期
    async def start_run(self, thread_id: str, goal: str, overrides: Optional[dict] = None) -> dict:
        thread = self.db.get("threads", thread_id)
        if not thread:
            raise ValueError("线程不存在")
        budget = self.default_budget().model_copy(update={k: v for k, v in (overrides or {}).items() if k in Budget.model_fields})
        mode = "llm" if (self.llm and self.llm.available) else "template"
        state = RunState(phase="plan", mode=mode, started_at=now_ms())
        run = {
            "id": new_id("run"),
            "thread_id": thread_id,
            "goal": goal.strip(),
            "status": "planning",
            "phase": "plan",
            "mode": mode,
            "budget_json": dumps(budget.model_dump()),
            "usage_json": dumps(state.usage.model_dump()),
            "state_json": dumps(state.model_dump()),
            "created_at": now_ms(),
            "updated_at": now_ms(),
        }
        self.db.insert("runs", run)
        if (self.db.fetchone("SELECT COUNT(*) AS c FROM runs WHERE thread_id = ?", [thread_id]) or {}).get("c") == 1 and thread["title"].startswith("新研究"):
            self.db.update("threads", thread_id, {"title": goal.strip()[:40]})
        self.add_message(thread_id, run["id"], "user", [{"type": "text", "text": goal.strip()}])
        self._emit(run, "run.created", {"goal": goal, "mode": mode, "budget": budget.model_dump()})
        if mode == "template":
            self._emit(run, "run.degraded", {"reason": "未配置可用模型：以模板模式运行（规则规划 + 确定性报告）"})
        self._spawn(run["id"])
        return self._public_run(run)

    def _spawn(self, run_id: str) -> None:
        old = self._tasks.get(run_id)
        if old and not old.done():
            return
        self._tasks[run_id] = asyncio.create_task(self._loop(run_id), name=f"run:{run_id}")

    async def stop(self, run_id: str) -> dict:
        run = self.db.get("runs", run_id)
        if not run:
            raise ValueError("运行不存在")
        self._stop_flags.add(run_id)
        t = self._tasks.get(run_id)
        if t and not t.done():
            t.cancel()
        else:
            # 挂起中（等待审批 / 中断）：直接收尾
            if run["status"] in ("waiting_approval", "interrupted", "planning", "running"):
                await self._finalize_stopped(run, "用户请求停止")
        return self.get_run(run_id) or {}

    async def resume(self, run_id: str) -> dict:
        run = self.db.get("runs", run_id)
        if not run:
            raise ValueError("运行不存在")
        if run["status"] in ("completed", "failed"):
            raise ValueError(f"运行已{run['status']}，无法恢复")
        pending = self.db.fetchone("SELECT id FROM approvals WHERE run_id = ? AND status = 'pending' AND kind != 'memory_write'", [run_id])
        if pending:
            raise ValueError("仍有待处理的审批，请先决定")
        self._stop_flags.discard(run_id)
        self.db.update("runs", run_id, {"status": "running", "updated_at": now_ms(), "stop_reason": None})
        self._emit(run, "run.resumed", {"from_status": run["status"], "phase": run["phase"]})
        self._spawn(run_id)
        return self.get_run(run_id) or {}

    def recover_on_startup(self) -> int:
        rows = self.db.fetchall("SELECT * FROM runs WHERE status IN ('planning','running')")
        for r in rows:
            self.db.update("runs", r["id"], {"status": "interrupted", "updated_at": now_ms(), "stop_reason": "进程重启，运行被中断；可从检查点恢复"})
            self._emit(r, "run.interrupted", {"reason": "进程重启", "phase": r["phase"]})
        return len(rows)

    # ------------------------------------------------------------- 主循环
    async def _loop(self, run_id: str) -> None:
        run = self.db.get("runs", run_id)
        if not run:
            return
        try:
            await self._execute(run)
        except NeedsApproval as na:
            self.db.update("runs", run_id, {"status": "waiting_approval", "updated_at": now_ms()})
            self._emit(run, "run.waiting_approval", {"approval": na.approval})
        except StopRun as sr:
            await self._finalize_stopped(self.db.get("runs", run_id) or run, sr.reason, status=sr.status)
        except asyncio.CancelledError:
            await self._finalize_stopped(self.db.get("runs", run_id) or run, "用户请求停止")
        except Exception as e:  # noqa: BLE001
            log.exception("run %s crashed", run_id)
            self.db.update("runs", run_id, {"status": "failed", "error": f"{type(e).__name__}: {e}", "updated_at": now_ms(), "finished_at": now_ms()})
            self._emit(run, "run.failed", {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})
            self.add_message(run["thread_id"], run_id, "assistant", [{"type": "callout", "level": "error", "text": f"运行失败：{type(e).__name__}: {e}。可点击「恢复」从最近检查点重试。"}])
        finally:
            self._stop_flags.discard(run_id)

    def _load_state(self, run: dict) -> RunState:
        return RunState(**loads(run.get("state_json"), {}))

    def checkpoint(self, run: dict, state: RunState, *, note: str = "") -> None:
        state.step += 0
        self.db.update("runs", run["id"], {"state_json": dumps(state.model_dump()), "usage_json": dumps(state.usage.model_dump()), "phase": state.phase, "mode": state.mode, "updated_at": now_ms()})
        self.db.insert("checkpoints", {"run_id": run["id"], "step": state.step, "state_json": dumps(state.model_dump()), "created_at": now_ms()})
        self._emit(run, "run.checkpoint", {"step": state.step, "phase": state.phase, "task_id": state.current_task_id, "usage": state.usage.model_dump(), "note": note})

    def _request_approval(self, run: dict, kind: str, title: str, payload: dict, task_id: Optional[str] = None) -> dict:
        row = {"id": new_id("apr"), "run_id": run["id"], "thread_id": run["thread_id"], "task_id": task_id, "kind": kind, "title": title, "payload_json": dumps(payload), "status": "pending", "created_at": now_ms()}
        self.db.insert("approvals", row)
        pub = self._public_approval(row)
        self._emit(run, "approval.requested", pub)
        self.add_message(run["thread_id"], run["id"], "assistant", [{"type": "approval", "approval": pub}])
        return pub

    def _make_ctx(self, run: dict, task_id: Optional[str], state: RunState) -> ToolContext:
        return ToolContext(run_id=run["id"], task_id=task_id, settings=self.settings, evidence=self.evidence, emit=lambda t, p: self._emit(run, t, p), working_memory=state.working_memory, cache=self.cache, breaker=self.breaker, faults=self.faults, registry=self.registry, memory=self.memory, thread_id=run["thread_id"])

    async def _execute(self, run: dict) -> None:
        state = self._load_state(run)
        budget = Budget(**loads(run.get("budget_json"), {}))
        policy = Policy(budget)
        if run["status"] != "running":
            self.db.update("runs", run["id"], {"status": "running", "updated_at": now_ms()})
            run["status"] = "running"
            self._emit(run, "run.started", {"phase": state.phase, "mode": state.mode})

        async def gate(st: RunState) -> None:
            st.stop_requested = run["id"] in self._stop_flags
            reason = policy.check_stop(st)
            if reason:
                raise StopRun(reason)
            warn = policy.budget_warning(st)
            if warn:
                st.budget_warned = True
                self.checkpoint(run, st, note="budget warning")
                approval = self._request_approval(run, "budget", "预算即将用尽，是否继续？", {"warning": warn, "usage": st.usage.model_dump(), "budget": budget.model_dump(), "proposal": "批准后预算上限提高到当前的 2 倍；拒绝则停止并输出部分报告。"}, task_id=st.current_task_id)
                raise NeedsApproval(approval)

        executor = TaskExecutor(
            llm=self.llm,
            registry=self.registry,
            runner=self.runner,
            policy=policy,
            ctx_mgr=ContextManager(budget.context_budget_tokens),
            emit=lambda t, p: self._emit(run, t, p),
            make_ctx=lambda task_id, st: self._make_ctx(run, task_id, st),
            gate=gate,
            request_approval=lambda kind, title, payload: self._request_approval(run, kind, title, payload, task_id=state.current_task_id),
            checkpoint=lambda st: self.checkpoint(run, st),
        )

        while True:
            await gate(state)
            if state.phase == "plan":
                await self._phase_plan(run, state, policy)
            elif state.phase == "execute":
                await self._phase_execute(run, state, executor)
            elif state.phase == "synthesize":
                await self._phase_synthesize(run, state, status="completed")
            else:
                return

    # -------------------------------------------------------------- 阶段
    async def _phase_plan(self, run: dict, state: RunState, policy: Policy) -> None:
        goal = run["goal"]
        memories = self.memory.recall(goal, limit=5)
        history = self.memory.thread_history(run["thread_id"], exclude_run_id=run["id"])
        self._emit(run, "memory.recalled", {"count": len(memories), "items": [{"kind": m["kind"], "content": m["content"][:120]} for m in memories], "thread_history": len(history)})
        t0 = time.time()
        plan, degrade = await self.planner.plan(goal, memories=memories, history=history, on_event=lambda t, p: self._emit(run, t, p))
        if degrade:
            state.mode = "template"
            state.degrade_reasons.append(degrade)
            self._emit(run, "run.degraded", {"reason": degrade})
        # LLM 计划里没有流水线；给每个任务挂一个模板流水线作为后备（模型中途不可用时可继续）
        if plan.mode == "llm":
            from .planner import template_plan

            tpl = template_plan(goal, self.registry)
            fallback_steps = [s for t in tpl.tasks for s in (t.pipeline or [])]
            for t in plan.tasks:
                if not t.pipeline:
                    t.pipeline = [s for s in fallback_steps if s["tool"] in (t.tools or [])] or None
        # 落库任务
        self.db.execute("DELETE FROM tasks WHERE run_id = ?", [run["id"]])
        for idx, t in enumerate(plan.tasks):
            self.db.insert("tasks", {"id": f"{run['id']}:{t.id}", "run_id": run["id"], "idx": idx, "title": t.title, "objective": t.objective, "depends_on_json": dumps(t.depends_on), "allowed_tools_json": dumps(t.tools), "acceptance": t.acceptance, "pipeline_json": dumps(t.pipeline) if t.pipeline else None, "status": "pending"})
        state.working_memory["plan_title"] = plan.title
        if plan.assumptions:
            state.working_memory["assumptions"] = plan.assumptions
        state.step += 1
        tasks_pub = [self._public_task(t) for t in self.db.fetchall("SELECT * FROM tasks WHERE run_id = ? ORDER BY idx", [run["id"]])]
        self._emit(run, "plan.created", {"title": plan.title, "mode": plan.mode, "assumptions": plan.assumptions, "tasks": tasks_pub, "latency_ms": int((time.time() - t0) * 1000)})
        self.add_message(run["thread_id"], run["id"], "assistant", [{"type": "plan", "title": plan.title, "mode": plan.mode, "assumptions": plan.assumptions, "tasks": tasks_pub}])
        state.phase = "execute"
        self.checkpoint(run, state, note="plan")
        need, reason = policy.plan_needs_approval(plan, self.registry)
        if need:
            approval = self._request_approval(run, "plan", "请确认研究计划", {"reason": reason, "title": plan.title, "tasks": [{"id": t.id, "title": t.title, "tools": t.tools} for t in plan.tasks], "hint": "可在拒绝理由中说明要删掉的任务编号，如：去掉 t5"})
            raise NeedsApproval(approval)

    def _next_task(self, run: dict) -> Optional[dict]:
        tasks = self.db.fetchall("SELECT * FROM tasks WHERE run_id = ? ORDER BY idx", [run["id"]])
        done = {t["id"].split(":", 1)[1] for t in tasks if t["status"] in ("done", "failed", "skipped", "partial")}
        for t in tasks:
            if t["status"] in ("pending", "running"):
                deps = loads(t.get("depends_on_json"), [])
                if all(d in done for d in deps):
                    return t
        # 依赖永远无法满足（循环）：取第一个 pending
        for t in tasks:
            if t["status"] in ("pending", "running"):
                return t
        return None

    async def _phase_execute(self, run: dict, state: RunState, executor: TaskExecutor) -> None:
        trow = self._next_task(run)
        if trow is None:
            state.phase = "synthesize"
            self.checkpoint(run, state, note="all tasks done")
            return
        short_id = trow["id"].split(":", 1)[1]
        task = PlanTask(id=short_id, title=trow["title"], objective=trow["objective"], depends_on=loads(trow.get("depends_on_json"), []), tools=loads(trow.get("allowed_tools_json"), []), acceptance=trow.get("acceptance") or "", pipeline=loads(trow.get("pipeline_json"), None))
        if state.current_task_id != trow["id"]:
            state.current_task_id = trow["id"]
            state.task_messages, state.task_steps, state.task_consecutive_errors = [], 0, 0
            state.task_call_signatures, state.task_evidence, state.task_blocks = [], [], []
            state.pipeline_index, state.pending_tool_call = 0, None
            self.db.update("tasks", trow["id"], {"status": "running", "started_at": now_ms()})
            self._emit(run, "task.start", {"task_id": trow["id"], "title": task.title, "idx": trow["idx"], "mode": state.mode})
        try:
            result = await executor.run_task(task, state, goal=run["goal"])
        except StopRun:
            self.db.update("tasks", trow["id"], {"status": "failed", "error": "运行停止", "ended_at": now_ms()})
            raise
        self.db.update("tasks", trow["id"], {"status": result.status, "result_json": dumps(result.model_dump()), "ended_at": now_ms()})
        state.task_summaries.append({"task_id": trow["id"], "title": task.title, "status": result.status, "summary": result.summary, "evidence": result.evidence_labels})
        state.working_memory[f"task_{short_id}_evidence"] = result.evidence_labels
        self._emit(run, "task.end", {"task_id": trow["id"], "title": task.title, "status": result.status, "summary": result.summary[:500], "evidence": result.evidence_labels})
        blocks: list[dict] = [{"type": "status", "text": f"任务「{task.title}」{ {'done': '完成', 'partial': '部分完成', 'failed': '失败'}.get(result.status, result.status) }", "task_id": trow["id"], "status": result.status}]
        blocks += result.blocks[:12]
        blocks.append({"type": "text", "text": result.summary, "evidence_refs": result.evidence_labels})
        self.add_message(run["thread_id"], run["id"], "assistant", blocks)
        state.current_task_id = None
        state.task_messages, state.task_blocks, state.task_evidence, state.pending_tool_call = [], [], [], None
        state.step += 1
        self.checkpoint(run, state, note=f"task {short_id} {result.status}")

    async def _phase_synthesize(self, run: dict, state: RunState, *, status: str, status_note: str = "") -> None:
        evidence = self.evidence.list(run["id"])
        plan = Plan(title=state.working_memory.get("plan_title") or run["goal"][:40], tasks=[], mode=state.mode)
        force_template = status != "completed" or state.mode == "template"
        md, lint_res, gen_mode = await self.synthesizer.write(goal=run["goal"], plan=plan, task_summaries=state.task_summaries, evidence=evidence, run_meta={"status_note": status_note, "mode_note": "；".join(state.degrade_reasons) or "模型未配置"}, on_event=lambda t, p: self._emit(run, t, p), force_template=force_template)
        if gen_mode == "llm":
            # 成本已在 on_event 之外；此处从事件无法取回，所以在 router 层记录到 state：简化为按最近 llm.end 事件累计
            pass
        art = {"id": new_id("art"), "run_id": run["id"], "thread_id": run["thread_id"], "type": "report", "title": f"研究报告：{plan.title}", "content_md": md, "blocks_json": dumps([]), "lint_json": dumps(lint_res.public()), "version": 1, "created_at": now_ms()}
        self.db.insert("artifacts", art)
        self._emit(run, "artifact.created", {"id": art["id"], "title": art["title"], "type": "report", "lint": lint_res.public(), "gen_mode": gen_mode, "chars": len(md)})
        self._emit(run, "compliance.lint", lint_res.public())
        state.phase = "done"
        state.step += 1
        finished = now_ms()
        self.db.update("runs", run["id"], {"status": status, "phase": "done", "finished_at": finished, "updated_at": finished, "state_json": dumps(state.model_dump()), "usage_json": dumps(state.usage.model_dump()), "stop_reason": status_note or None})
        self.checkpoint(run, state, note="done")
        blocks = [
            {"type": "artifact_ref", "artifact_id": art["id"], "title": art["title"], "gen_mode": gen_mode, "lint": lint_res.public()},
            {"type": "text", "text": _excerpt(md)},
        ]
        if not lint_res.ok or lint_res.stats.get("forbidden_removed"):
            blocks.append({"type": "callout", "level": "warning", "text": f"合规检查：移除 {lint_res.stats.get('forbidden_removed', 0)} 处违规表述，{lint_res.stats.get('uncited', 0)} 条结论无引用已标为不确定。"})
        self.add_message(run["thread_id"], run["id"], "assistant", blocks)
        self._emit(run, "run.completed" if status == "completed" else "run.stopped", {"status": status, "usage": state.usage.model_dump(), "artifact_id": art["id"], "reason": status_note})
        # 记忆写入提案（需审批）
        proposals = propose_memories(run["goal"], md, evidence, run["id"])
        if proposals:
            budget = Budget(**loads(run.get("budget_json"), {}))
            if budget.memory_write_approval:
                self._request_approval(run, "memory_write", "是否把本次研究要点写入长期记忆？", {"proposals": proposals})
            else:
                for p in proposals:
                    self.memory.add(scope="user", kind=p["kind"], content=p["content"], tags=p.get("tags", ""), source_run_id=run["id"])
                self._emit(run, "memory.written", {"count": len(proposals)})

    async def _finalize_stopped(self, run: dict, reason: str, *, status: str = "stopped") -> None:
        state = self._load_state(run)
        self._emit(run, "run.stopping", {"reason": reason})
        # 当前任务标记失败
        if state.current_task_id:
            self.db.update("tasks", state.current_task_id, {"status": "failed", "error": reason, "ended_at": now_ms()})
        for t in self.db.fetchall("SELECT id FROM tasks WHERE run_id = ? AND status IN ('pending','running')", [run["id"]]):
            self.db.update("tasks", t["id"], {"status": "skipped", "error": reason})
        try:
            await self._phase_synthesize(run, state, status=status, status_note=f"运行未完成：{reason}。以下为基于已获得证据的部分报告。")
        except Exception as e:  # noqa: BLE001
            log.exception("finalize_stopped failed")
            self.db.update("runs", run["id"], {"status": status, "stop_reason": reason, "error": str(e), "finished_at": now_ms(), "updated_at": now_ms()})
            self._emit(run, "run.stopped", {"status": status, "reason": reason, "error": str(e)})

    # ------------------------------------------------------------- 审批
    async def decide_approval(self, approval_id: str, approved: bool, reason: str = "") -> dict:
        a = self.db.get("approvals", approval_id)
        if not a:
            raise ValueError("审批不存在")
        if a["status"] != "pending":
            raise ValueError("该审批已处理")
        run = self.db.get("runs", a["run_id"])
        if not run:
            raise ValueError("运行不存在")
        self.db.update("approvals", approval_id, {"status": "approved" if approved else "rejected", "reason": reason, "decided_at": now_ms()})
        pub = self._public_approval(self.db.get("approvals", approval_id) or a)
        self._emit(run, "approval.decided", pub)
        state = self._load_state(run)
        kind = a["kind"]
        payload = loads(a.get("payload_json"), {})
        if kind == "plan":
            if approved:
                removed = _parse_removed_tasks(reason)
                if removed:
                    for t in self.db.fetchall("SELECT id FROM tasks WHERE run_id = ?", [run["id"]]):
                        if t["id"].split(":", 1)[1] in removed:
                            self.db.update("tasks", t["id"], {"status": "skipped", "error": "用户在计划确认时移除"})
                    self._emit(run, "plan.edited", {"removed": sorted(removed)})
                state.phase = "execute"
                self.checkpoint(run, state, note="plan approved")
                return await self.resume(run["id"])
            await self._finalize_stopped(run, f"用户拒绝了研究计划：{reason or '无说明'}")
            return self.get_run(run["id"]) or {}
        if kind == "tool":
            if state.pending_tool_call:
                state.pending_tool_call["decision"] = approved
                state.pending_tool_call["reason"] = reason
            self.checkpoint(run, state, note="tool approval")
            return await self.resume(run["id"])
        if kind == "budget":
            if approved:
                budget = Budget(**loads(run.get("budget_json"), {}))
                budget.max_cost_usd *= 2
                budget.max_tool_calls *= 2
                budget.max_llm_steps *= 2
                self.db.update("runs", run["id"], {"budget_json": dumps(budget.model_dump())})
                state.budget_warned = True
                self.checkpoint(run, state, note="budget extended")
                self._emit(run, "budget.extended", budget.model_dump())
                return await self.resume(run["id"])
            self._stop_flags.add(run["id"])
            await self._finalize_stopped(run, "用户拒绝扩大预算")
            return self.get_run(run["id"]) or {}
        if kind == "memory_write":
            if approved:
                for p in payload.get("proposals", []):
                    self.memory.add(scope="user", kind=p["kind"], content=p["content"], tags=p.get("tags", ""), source_run_id=run["id"])
                self._emit(run, "memory.written", {"count": len(payload.get("proposals", []))})
            return self.get_run(run["id"]) or {}
        return self.get_run(run["id"]) or {}

    # ------------------------------------------------------------- 观测
    def trace(self, run_id: str) -> dict:
        run = self.db.get("runs", run_id)
        if not run:
            return {}
        events = self.bus.replay(run["thread_id"], 0, run_id=run_id)
        spans = []
        for e in events:
            t = e["type"]
            p = e["payload"]
            if t == "llm.end":
                spans.append({"kind": "llm", "name": p.get("purpose"), "at": e["created_at"], "duration_ms": p.get("latency_ms"), "tokens_in": p.get("input_tokens"), "tokens_out": p.get("output_tokens"), "cost_usd": p.get("cost_usd"), "status": "ok", "provider": p.get("provider"), "fallback": p.get("fallback")})
            elif t == "llm.error":
                spans.append({"kind": "llm", "name": p.get("purpose"), "at": e["created_at"], "status": "error", "error": p.get("error"), "provider": p.get("provider")})
            elif t == "tool.end":
                spans.append({"kind": "tool", "name": p.get("tool"), "at": e["created_at"], "duration_ms": p.get("latency_ms"), "status": p.get("status"), "evidence": p.get("evidence_label"), "cached": p.get("cached"), "task_id": p.get("task_id")})
            elif t in ("task.start", "task.end"):
                spans.append({"kind": "task", "name": p.get("title"), "at": e["created_at"], "status": p.get("status") or "start", "task_id": p.get("task_id"), "event": t})
            elif t in ("context.compressed", "run.degraded", "tool.fallback", "tool.stale_cache", "tool.retry", "approval.requested", "approval.decided", "run.checkpoint", "compliance.lint", "tool.loop_blocked"):
                spans.append({"kind": "system", "name": t, "at": e["created_at"], "detail": p})
        state = self._load_state(run)
        return {"run_id": run_id, "usage": state.usage.model_dump(), "spans": spans, "breaker": self.breaker.state(), "faults": self.faults.state(), "degrade_reasons": state.degrade_reasons, "checkpoints": self.db.fetchall("SELECT step, created_at FROM checkpoints WHERE run_id = ? ORDER BY id", [run_id])}


def _excerpt(md: str, max_chars: int = 1200) -> str:
    i = md.find("## 核心结论")
    seg = md[i:] if i != -1 else md
    nxt = seg.find("\n## ", 5)
    seg = seg if nxt == -1 else seg[:nxt]
    return seg.strip()[:max_chars]


def _parse_removed_tasks(reason: str) -> set[str]:
    import re

    if not reason:
        return set()
    if not any(k in reason for k in ("去掉", "删除", "移除", "不要", "跳过", "remove", "skip")):
        return set()
    return set(re.findall(r"\bt\d+\b", reason))
