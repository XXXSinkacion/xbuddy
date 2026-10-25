"""能力 / 工具注册与统一调用管线。

ToolSpec 携带：JSON Schema、风险级别（L0/L1 自动，L2/L3 需审批）、来源、超时、可缓存、备用链、相对成本。
ToolRunner.invoke 统一处理：参数校验 → 故障注入 → 缓存 → 熔断 → 超时/重试 → 备用链 → 过期缓存 → 证据记录 → 事件。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..models import ToolResult
from .cache import CircuitBreaker, FaultInjector, TTLCache

log = logging.getLogger("xbuddy.tools")

AUTO_RISK = {"L0", "L1"}
RISK_LABELS = {
    "L0": "只读·轻量（自动）",
    "L1": "只读·较重/外部源（自动，计入预算）",
    "L2": "写入·有副作用（需审批）",
    "L3": "外部·未知来源（需审批）",
}


class ToolError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, code: Optional[int] = None, request_id: Optional[str] = None, status: str = "error"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.request_id = request_id
        self.status = status


@dataclass
class ToolContext:
    run_id: str
    task_id: Optional[str]
    settings: Any
    evidence: Any  # EvidenceStore（duck typing，避免循环依赖）
    emit: Callable[[str, dict], None]
    working_memory: dict
    cache: TTLCache
    breaker: CircuitBreaker
    faults: FaultInjector
    registry: "ToolRegistry"
    memory: Any = None  # MemoryStore
    thread_id: Optional[str] = None


Handler = Callable[[dict, ToolContext], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Handler
    risk: str = "L0"
    source: str = "local"  # fuyao | ifind | local | memory
    timeout_s: float = 20.0
    cacheable: bool = False
    cache_ttl_s: float = 0.0
    fallbacks: tuple[str, ...] = ()
    est_cost: float = 1.0
    tags: tuple[str, ...] = ()
    max_retries: int = 2

    def llm_schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def public(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
            "risk_label": RISK_LABELS.get(self.risk, self.risk),
            "source": self.source,
            "timeout_s": self.timeout_s,
            "cacheable": self.cacheable,
            "cache_ttl_s": self.cache_ttl_s,
            "fallbacks": list(self.fallbacks),
            "est_cost": self.est_cost,
            "tags": list(self.tags),
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        self._tools[spec.name] = spec
        return spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def llm_tools(self, allowed: Optional[list[str]] = None) -> list[dict]:
        out = []
        for name, spec in self._tools.items():
            if allowed is None or name in allowed:
                out.append(spec.llm_schema())
        return out

    def catalog_text(self, allowed: Optional[list[str]] = None) -> str:
        lines = []
        for spec in self._tools.values():
            if allowed is not None and spec.name not in allowed:
                continue
            lines.append(f"- {spec.name} [{spec.risk}/{spec.source}]: {spec.description.splitlines()[0]}")
        return "\n".join(lines)


def args_signature(name: str, args: dict) -> str:
    return name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def _validate_args(spec: ToolSpec, args: dict) -> Optional[str]:
    schema = spec.parameters or {}
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args or args[req] in (None, ""):
            return f"缺少必填参数 `{req}`"
    for k, v in list(args.items()):
        if k not in props:
            continue
        t = props[k].get("type")
        if t == "integer" and isinstance(v, str) and v.strip().lstrip("-").isdigit():
            args[k] = int(v)
        elif t == "number" and isinstance(v, str):
            try:
                args[k] = float(v)
            except ValueError:
                return f"参数 `{k}` 需为数字"
        enum = props[k].get("enum")
        if enum and args[k] not in enum:
            return f"参数 `{k}` 取值须为 {enum}"
    return None


class ToolRunner:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def invoke(self, name: str, args: dict, ctx: ToolContext, *, allow_fallback: bool = True) -> tuple[ToolResult, Optional[dict]]:
        """返回 (结果, 证据记录)。无论成败都会生成证据（状态可为 error/missing）。"""
        spec = self.registry.get(name)
        started = time.time()
        args = dict(args or {})
        if spec is None:
            res = ToolResult.fail(f"未知工具 {name}", status="error")
            ev = ctx.evidence.record(ctx.run_id, ctx.task_id, name, args, res, latency_ms=0) if ctx.evidence else None
            return res, ev

        ctx.emit("tool.start", {"tool": name, "args": args, "risk": spec.risk, "source": spec.source, "task_id": ctx.task_id})

        err = _validate_args(spec, args)
        if err:
            res = ToolResult.fail(err, status="error", source=spec.source)
            res.meta["validation"] = True
            return self._finish(spec, args, res, ctx, started)

        res = await self._invoke_with_resilience(spec, args, ctx)

        # 备用链
        if not res.ok and allow_fallback and spec.fallbacks:
            for fb in spec.fallbacks:
                fb_spec = self.registry.get(fb)
                if fb_spec is None:
                    continue
                ctx.emit("tool.fallback", {"tool": name, "fallback": fb, "reason": res.error})
                fb_res = await self._invoke_with_resilience(fb_spec, args, ctx)
                if fb_res.ok:
                    fb_res.meta["fallback_of"] = name
                    fb_res.summary = f"[备用源 {fb}] " + fb_res.summary
                    res = fb_res
                    break

        # 过期缓存兜底（明确标注 stale 与原时点）
        if not res.ok and spec.cacheable:
            stale = ctx.cache.get_stale(args_signature(spec.name, args))
            if stale:
                stored_at, cached = stale
                res = ToolResult(**cached)
                res.status = "stale"
                res.ok = True
                res.source = f"{res.source} (过期缓存)"
                res.summary = f"[过期缓存，取自 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stored_at))}] " + res.summary
                res.meta["stale_from"] = int(stored_at * 1000)
                ctx.emit("tool.stale_cache", {"tool": name, "stored_at": int(stored_at * 1000)})

        return self._finish(spec, args, res, ctx, started)

    def _finish(self, spec: ToolSpec, args: dict, res: ToolResult, ctx: ToolContext, started: float) -> tuple[ToolResult, Optional[dict]]:
        latency_ms = int((time.time() - started) * 1000)
        res.meta.setdefault("latency_ms", latency_ms)
        ev = None
        if ctx.evidence is not None:
            ev = ctx.evidence.record(ctx.run_id, ctx.task_id, spec.name, args, res, latency_ms=latency_ms)
        ctx.emit(
            "tool.end",
            {
                "tool": spec.name,
                "ok": res.ok,
                "status": res.status,
                "source": res.source,
                "latency_ms": latency_ms,
                "summary": res.summary[:300],
                "evidence_label": ev["label"] if ev else None,
                "evidence_id": ev["id"] if ev else None,
                "error": res.error,
                "task_id": ctx.task_id,
                "cached": bool(res.meta.get("cached")),
            },
        )
        return res, ev

    async def _invoke_with_resilience(self, spec: ToolSpec, args: dict, ctx: ToolContext) -> ToolResult:
        key = args_signature(spec.name, args)

        # 缓存命中
        if spec.cacheable:
            cached = ctx.cache.get(key, spec.cache_ttl_s)
            if cached is not None:
                res = ToolResult(**cached)
                res.meta["cached"] = True
                return res

        # 熔断
        if ctx.breaker.is_open(spec.source):
            return ToolResult.fail(f"数据源 {spec.source} 熔断中（近期连续失败），已跳过调用", status="error", retryable=True, source=spec.source, circuit_open=True)

        last: Optional[ToolResult] = None
        for attempt in range(spec.max_retries + 1):
            # 故障注入（演示 / 测试）：模拟数据源错误 / 超时 / 限流，走与真实故障相同的重试-熔断-降级路径
            fault = ctx.faults.active(spec.source)
            if fault:
                msg = {"error": "注入故障：数据源返回 5003 数据源不可用", "timeout": "注入故障：数据源超时", "ratelimit": "注入故障：HTTP 429 / code=4001 频率超限"}.get(fault, f"注入故障：{fault}")
                res = ToolResult.fail(msg, status="error", retryable=True, source=spec.source, injected=True)
                last = res
                ctx.breaker.record_failure(spec.source)
                if attempt >= spec.max_retries:
                    break
                await asyncio.sleep(0.05)
                ctx.emit("tool.retry", {"tool": spec.name, "attempt": attempt + 1, "error": res.error})
                continue
            try:
                res = await asyncio.wait_for(spec.handler(args, ctx), timeout=spec.timeout_s)
            except asyncio.TimeoutError:
                res = ToolResult.fail(f"工具超时（>{spec.timeout_s}s）", status="error", retryable=True, source=spec.source)
            except ToolError as e:
                res = ToolResult.fail(str(e), status=e.status if e.status in ("error", "missing") else "error", retryable=e.retryable, source=spec.source, code=e.code)
                res.request_id = e.request_id
            except Exception as e:  # noqa: BLE001
                log.exception("tool %s crashed", spec.name)
                res = ToolResult.fail(f"工具异常：{type(e).__name__}: {e}", status="error", retryable=False, source=spec.source)
            last = res
            if res.ok:
                ctx.breaker.record_success(spec.source)
                if spec.cacheable:
                    ctx.cache.set(key, res.model_dump())
                res.meta["attempts"] = attempt + 1
                return res
            if res.status == "missing":
                # 数据缺失不是故障，不重试、不计熔断
                return res
            ctx.breaker.record_failure(spec.source)
            if not res.retryable or attempt >= spec.max_retries:
                break
            await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
            ctx.emit("tool.retry", {"tool": spec.name, "attempt": attempt + 1, "error": res.error})
        assert last is not None
        last.meta["attempts"] = spec.max_retries + 1
        return last
