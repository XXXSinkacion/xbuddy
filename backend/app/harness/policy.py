"""策略：权限分级、审批门控、预算与停止规则、循环检测。"""
from __future__ import annotations

import time
from typing import Optional

from ..models import Budget, Plan, RunState
from ..tools.registry import AUTO_RISK, ToolRegistry, ToolSpec


class Policy:
    def __init__(self, budget: Budget):
        self.budget = budget

    # ---- 审批 ----
    def plan_needs_approval(self, plan: Plan, registry: ToolRegistry) -> tuple[bool, str]:
        mode = self.budget.plan_approval
        if mode == "always":
            return True, "设置为始终确认计划"
        if mode == "never":
            return False, ""
        if len(plan.tasks) > 6:
            return True, f"计划包含 {len(plan.tasks)} 个任务（>6）"
        for t in plan.tasks:
            for name in t.tools:
                spec = registry.get(name)
                if spec and spec.risk not in AUTO_RISK:
                    return True, f"任务「{t.title}」使用了需审批的工具 {name}（{spec.risk}）"
        est = sum((registry.get(n).est_cost if registry.get(n) else 1) for t in plan.tasks for n in t.tools)
        if est > 24:
            return True, f"预估工具成本 {est} 超过阈值"
        return False, ""

    def tool_needs_approval(self, spec: Optional[ToolSpec]) -> bool:
        if spec is None:
            return False
        return spec.risk not in AUTO_RISK

    # ---- 停止规则 ----
    def check_stop(self, state: RunState) -> Optional[str]:
        b = self.budget
        u = state.usage
        if state.stop_requested:
            return "用户请求停止"
        if u.llm_calls >= b.max_llm_steps:
            return f"达到模型调用上限 {b.max_llm_steps}"
        if u.tool_calls >= b.max_tool_calls:
            return f"达到工具调用上限 {b.max_tool_calls}"
        if u.cost_usd >= b.max_cost_usd:
            return f"达到成本上限 ${b.max_cost_usd:.2f}"
        if state.started_at and (time.time() * 1000 - state.started_at) / 1000 >= b.max_duration_s:
            return f"达到时长上限 {b.max_duration_s}s"
        return None

    def budget_warning(self, state: RunState) -> Optional[str]:
        if state.budget_warned:
            return None
        u, b = state.usage, self.budget
        if b.max_cost_usd > 0 and u.cost_usd >= 0.8 * b.max_cost_usd:
            return f"成本已达预算 80%（${u.cost_usd:.3f} / ${b.max_cost_usd:.2f}）"
        if u.tool_calls >= 0.8 * b.max_tool_calls:
            return f"工具调用已达预算 80%（{u.tool_calls} / {b.max_tool_calls}）"
        return None

    def task_step_exceeded(self, state: RunState) -> bool:
        return state.task_steps >= self.budget.max_task_steps

    def consecutive_errors_exceeded(self, state: RunState) -> bool:
        return state.task_consecutive_errors >= self.budget.max_consecutive_tool_errors

    @staticmethod
    def is_loop(state: RunState, signature: str) -> bool:
        return state.task_call_signatures.count(signature) >= 2

    def remaining(self, state: RunState) -> dict:
        b, u = self.budget, state.usage
        elapsed = (time.time() * 1000 - state.started_at) / 1000 if state.started_at else 0
        return {
            "llm_steps": {"used": u.llm_calls, "max": b.max_llm_steps},
            "tool_calls": {"used": u.tool_calls, "max": b.max_tool_calls},
            "cost_usd": {"used": round(u.cost_usd, 4), "max": b.max_cost_usd},
            "duration_s": {"used": round(elapsed, 1), "max": b.max_duration_s},
        }
