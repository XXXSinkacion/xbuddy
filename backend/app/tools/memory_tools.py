"""长期记忆相关工具：检索为 L0，写入为 L2（需审批）。"""
from __future__ import annotations

from ..models import ToolResult
from .registry import ToolContext, ToolRegistry, ToolSpec


def register_memory_tools(registry: ToolRegistry) -> list[str]:
    names: list[str] = []

    def reg(spec: ToolSpec):
        registry.register(spec)
        names.append(spec.name)

    async def search_memory(args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.missing("记忆存储不可用", source="memory")
        rows = ctx.memory.recall(args["query"], limit=int(args.get("limit") or 5))
        if not rows:
            return ToolResult(ok=True, data={"item": []}, source="memory", summary=f"长期记忆中没有与“{args['query']}”相关的条目", meta={"no_evidence": True})
        return ToolResult(ok=True, data={"item": rows}, source="memory", summary="相关记忆：" + "；".join(f"[{r['kind']}] {r['content'][:60]}" for r in rows[:5]), meta={"no_evidence": True})

    reg(ToolSpec(name="search_memory", description="检索用户长期记忆（偏好、自选、历史研究结论）。", parameters={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, "required": ["query"]}, handler=search_memory, risk="L0", source="memory", tags=("memory",)))

    async def save_memory(args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.fail("记忆存储不可用", source="memory")
        row = ctx.memory.add(scope="user", kind=args.get("kind") or "note", content=args["content"], tags=args.get("tags") or "", source_run_id=ctx.run_id)
        return ToolResult(ok=True, data=row, source="memory", summary=f"已写入长期记忆 [{row['kind']}]：{row['content'][:80]}", meta={"no_evidence": True})

    reg(ToolSpec(name="save_memory", description="把一条结论 / 偏好 / 备注写入用户长期记忆（有副作用，需用户审批）。", parameters={"type": "object", "properties": {"kind": {"type": "string", "enum": ["preference", "conclusion", "note", "watchlist"], "default": "note"}, "content": {"type": "string"}, "tags": {"type": "string", "description": "逗号分隔标签"}}, "required": ["content"]}, handler=save_memory, risk="L2", source="memory", tags=("memory",)))

    async def add_to_watchlist(args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.fail("记忆存储不可用", source="memory")
        code = str(args["thscode"]).strip().upper()
        content = f"{code} {args.get('name') or ''}".strip() + (f"：{args['note']}" if args.get("note") else "")
        row = ctx.memory.add(scope="user", kind="watchlist", content=content, tags="watchlist," + code, source_run_id=ctx.run_id)
        return ToolResult(ok=True, data=row, source="memory", summary=f"已加入自选关注：{content}", meta={"no_evidence": True})

    reg(ToolSpec(name="add_to_watchlist", description="把标的加入用户自选关注列表（有副作用，需用户审批；不构成买卖建议）。", parameters={"type": "object", "properties": {"thscode": {"type": "string"}, "name": {"type": "string"}, "note": {"type": "string"}}, "required": ["thscode"]}, handler=add_to_watchlist, risk="L2", source="memory", tags=("memory",)))

    return names
