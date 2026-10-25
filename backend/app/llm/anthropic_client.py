"""Anthropic 官方 SDK 客户端（可作为主模型或备用模型）。
把内部 OpenAI 风格消息转换为 Anthropic Messages API 形态；assistant 原始内容块（含 thinking）保存在 `_raw_content` 以便回放。"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from .base import LLMClient, LLMError, LLMResponse, ToolCall, parse_json_args


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _convert_messages(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
    system_parts: list[str] = []
    out: list[dict] = []

    def push(role: str, content: list[dict]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": role, "content": list(content)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(str(m.get("content") or ""))
        elif role == "user":
            push("user", [_text_block(str(m.get("content") or ""))])
        elif role == "assistant":
            raw = m.get("_raw_content")
            if raw:
                push("assistant", [dict(b) for b in raw])
                continue
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append(_text_block(str(m["content"])))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"), "input": parse_json_args(fn.get("arguments"))})
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": str(m.get("content") or "")}])
    # Anthropic 要求首条为 user
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [_text_block("（开始）")]})
    return ("\n\n".join(p for p in system_parts if p) or None), out


class AnthropicClient(LLMClient):
    def __init__(self, *, api_key: str, model: str = "claude-opus-5-5", base_url: Optional[str] = None, timeout: float = 120.0, effort: Optional[str] = None, name: str = "anthropic"):
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url or None, timeout=timeout, max_retries=0)
        self.model = model
        self.name = name
        self.effort = effort

    async def chat(self, messages, tools=None, *, temperature=0.2, max_tokens=2048, json_mode=False) -> LLMResponse:
        import anthropic

        system, conv = _convert_messages(messages)
        if json_mode:
            system = (system or "") + "\n\n只输出一个合法的 JSON 对象，不要任何其他文字。"
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": conv}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}} for t in tools]
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        t0 = time.time()
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError(f"限流 429: {e}", retryable=True, status=429) from e
        except anthropic.AuthenticationError as e:
            raise LLMError(f"鉴权失败: {e}", retryable=False, status=401) from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"请求错误 400: {e}", retryable=False, status=400) from e
        except anthropic.APIStatusError as e:
            retry = e.status_code >= 500 or e.status_code in (408, 409, 429)
            raise LLMError(f"API 错误 {e.status_code}: {e}", retryable=retry, status=e.status_code) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"网络/超时: {e}", retryable=True) from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"未知错误: {type(e).__name__}: {e}", retryable=False) from e

        if resp.stop_reason == "refusal":
            raise LLMError("模型拒绝了该请求（安全分类器）", retryable=False)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: list[dict] = []
        for b in resp.content:
            raw.append(b.model_dump(exclude_none=True))
            if b.type == "text":
                text_parts.append(b.text)
            elif b.type == "tool_use":
                tool_calls.append(ToolCall(id=b.id, name=b.name, arguments=dict(b.input) if isinstance(b.input, dict) else parse_json_args(b.input)))
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=int(getattr(resp.usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(resp.usage, "output_tokens", 0) or 0),
            model=resp.model or self.model,
            finish_reason=resp.stop_reason or "",
            provider=self.name,
            latency_ms=int((time.time() - t0) * 1000),
            raw_assistant_content=raw,
        )
