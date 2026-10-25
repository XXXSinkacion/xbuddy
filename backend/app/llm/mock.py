"""测试 / 离线演示用的脚本化 LLM。"""
from __future__ import annotations

from typing import Callable, Optional, Union

from .base import LLMClient, LLMError, LLMResponse

Scripted = Union[LLMResponse, Exception, Callable[[list[dict], Optional[list[dict]]], LLMResponse]]


class MockLLM(LLMClient):
    """按顺序返回预设响应；也可传入函数按消息动态生成。用尽后返回空内容。"""

    def __init__(self, script: Optional[list[Scripted]] = None, *, handler: Optional[Callable] = None, name: str = "mock", model: str = "mock-1"):
        self.script = list(script or [])
        self.handler = handler
        self.name = name
        self.model = model
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, *, temperature=0.2, max_tokens=2048, json_mode=False) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "json_mode": json_mode})
        if self.handler is not None:
            out = self.handler(messages, tools)
        elif self.script:
            out = self.script.pop(0)
        else:
            out = LLMResponse(content="", provider=self.name, model=self.model)
        if isinstance(out, Exception):
            raise out
        if callable(out):
            out = out(messages, tools)
        assert isinstance(out, LLMResponse)
        out.provider = out.provider or self.name
        out.model = out.model or self.model
        out.input_tokens = out.input_tokens or sum(len(str(m.get("content", ""))) for m in messages) // 3
        out.output_tokens = out.output_tokens or max(1, len(out.content or "") // 3)
        return out


class AlwaysFailLLM(LLMClient):
    def __init__(self, retryable: bool = True, name: str = "broken"):
        self.retryable = retryable
        self.name = name
        self.model = "broken-1"
        self.calls = 0

    async def chat(self, messages, tools=None, **kw) -> LLMResponse:
        self.calls += 1
        raise LLMError("模拟模型故障 503", retryable=self.retryable, status=503)
