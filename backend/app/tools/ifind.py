"""iFinD MCP 桥接：启动时列出远端工具并注册为 ifind_* 工具（L1，外部源）。
未配置或连接失败时静默跳过（降级），不影响其他能力。"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from ..models import ToolResult
from .registry import ToolContext, ToolError, ToolRegistry, ToolSpec

log = logging.getLogger("xbuddy.ifind")


@asynccontextmanager
async def _session(url: str, headers: dict, transport: str):
    from mcp import ClientSession

    if transport == "sse":
        from mcp.client.sse import sse_client

        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _headers(authorization: Optional[str]) -> dict:
    h: dict[str, str] = {}
    if authorization:
        h["Authorization"] = authorization
    return h


async def register_ifind_tools(registry: ToolRegistry, url: str, authorization: Optional[str], *, transport: str = "auto", max_tools: int = 60) -> dict:
    """连接 iFinD MCP，注册工具。返回 {ok, count, transport, error}。"""
    transports = ["streamable_http", "sse"] if transport == "auto" else [transport]
    last_err: Optional[str] = None
    for tr in transports:
        try:
            async with _session(url, _headers(authorization), tr) as session:
                listed = await session.list_tools()
                tools = list(listed.tools)[:max_tools]
                for t in tools:
                    name = ("ifind_" + t.name)[:64]
                    schema = t.inputSchema or {"type": "object", "properties": {}}
                    registry.register(
                        ToolSpec(
                            name=name,
                            description=f"[iFinD MCP] {t.description or t.name}",
                            parameters=schema,
                            handler=_make_handler(url, authorization, tr, t.name),
                            risk="L1",
                            source="ifind",
                            timeout_s=45,
                            cacheable=True,
                            cache_ttl_s=300,
                            est_cost=3,
                            tags=("ifind",),
                            max_retries=1,
                        )
                    )
                log.info("iFinD MCP connected via %s, %d tools", tr, len(tools))
                return {"ok": True, "count": len(tools), "transport": tr, "error": None}
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            log.warning("iFinD MCP connect failed via %s: %s", tr, last_err)
    return {"ok": False, "count": 0, "transport": None, "error": last_err}


def _make_handler(url: str, authorization: Optional[str], transport: str, tool_name: str):
    async def handler(args: dict, ctx: ToolContext) -> ToolResult:
        try:
            async with _session(url, _headers(authorization), transport) as session:
                result = await session.call_tool(tool_name, args or {})
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"iFinD MCP 调用失败：{type(e).__name__}: {e}", retryable=True) from e
        texts: list[str] = []
        for c in result.content or []:
            txt = getattr(c, "text", None)
            if txt:
                texts.append(txt)
        joined = "\n".join(texts).strip()
        data: Any = joined
        try:
            data = json.loads(joined)
        except Exception:
            pass
        if getattr(result, "isError", False):
            raise ToolError(f"iFinD MCP 返回错误：{joined[:300]}", retryable=False)
        if not joined:
            return ToolResult.missing("iFinD MCP 返回空内容", source="ifind")
        return ToolResult(ok=True, data=data, source="ifind-mcp", unit_notes="来自同花顺 iFinD MCP 工具 " + tool_name + "，单位/口径以返回字段为准", summary=f"iFinD {tool_name} 返回 {len(joined)} 字符：" + joined[:200].replace("\n", " "))

    return handler
