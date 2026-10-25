"""核心数据结构（pydantic）。

Block（动态 UI 块）以 dict 表达，type 取值：
text | plan | metrics | table | kline | bar | callout | approval | artifact_ref | evidence_refs | tool_call | status
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EvidenceStatus = Literal["ok", "partial", "missing", "stale", "error"]


class ToolResult(BaseModel):
    ok: bool
    status: EvidenceStatus = "ok"
    data: Any = None
    summary: str = ""
    source: str = "local"
    data_timestamp: Optional[int] = None  # 数据时点（毫秒）
    unit_notes: str = ""  # 单位 / 口径说明
    request_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False
    meta: dict = Field(default_factory=dict)

    @staticmethod
    def fail(error: str, *, status: EvidenceStatus = "error", retryable: bool = False, source: str = "local", **meta) -> "ToolResult":
        return ToolResult(ok=False, status=status, error=error, retryable=retryable, source=source, summary=f"调用失败：{error}", meta=meta)

    @staticmethod
    def missing(reason: str, *, source: str = "local", **meta) -> "ToolResult":
        return ToolResult(ok=False, status="missing", error=reason, source=source, summary=f"数据缺失：{reason}", meta=meta)


class Budget(BaseModel):
    max_llm_steps: int = 40
    max_tool_calls: int = 60
    max_cost_usd: float = 0.5
    max_duration_s: int = 600
    context_budget_tokens: int = 24000
    plan_approval: str = "auto"  # always | auto | never
    memory_write_approval: bool = True
    max_task_steps: int = 10
    max_consecutive_tool_errors: int = 3


class Usage(BaseModel):
    llm_calls: int = 0
    llm_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_cache_hits: int = 0
    elapsed_ms: int = 0
    compressions: int = 0
    tokens_saved: int = 0
    llm_latency_ms: int = 0
    tool_latency_ms: int = 0


class PlanTask(BaseModel):
    id: str
    title: str
    objective: str
    depends_on: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    acceptance: str = ""
    # 模板模式下的确定性流水线：[{"tool": name, "args": {...}, "save_as": key}]
    pipeline: Optional[list[dict]] = None


class Plan(BaseModel):
    title: str
    tasks: list[PlanTask]
    assumptions: list[str] = Field(default_factory=list)
    mode: str = "llm"  # llm | template
    notes: str = ""


class TaskResult(BaseModel):
    status: str = "done"  # done | failed | partial
    summary: str = ""
    evidence_labels: list[str] = Field(default_factory=list)
    blocks: list[dict] = Field(default_factory=list)
    facts: dict = Field(default_factory=dict)


class RunState(BaseModel):
    """检查点内容：恢复运行所需的全部状态。"""

    phase: str = "plan"  # plan | execute | synthesize | done
    step: int = 0
    mode: str = "llm"  # llm | template（降级）
    started_at: int = 0
    current_task_id: Optional[str] = None
    task_messages: list[dict] = Field(default_factory=list)
    task_steps: int = 0
    task_consecutive_errors: int = 0
    task_call_signatures: list[str] = Field(default_factory=list)
    task_evidence: list[str] = Field(default_factory=list)
    task_ok_evidence: list[str] = Field(default_factory=list)  # 本任务成功（ok/partial/stale）的证据编号
    task_blocks: list[dict] = Field(default_factory=list)
    task_notes: list[str] = Field(default_factory=list)  # 模板流水线逐步小结（随检查点持久化，恢复后不丢）
    pipeline_index: int = 0
    working_memory: dict = Field(default_factory=dict)
    task_summaries: list[dict] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    budget_warned: bool = False
    pending_tool_call: Optional[dict] = None  # 等待审批的工具调用 {id,name,arguments}
    degrade_reasons: list[str] = Field(default_factory=list)
    stop_requested: bool = False
    # 等待人工审批 / 进程中断的时长不计入时长预算
    paused_ms: int = 0
    paused_at: Optional[int] = None
