"""LLM 客户端抽象。消息采用 OpenAI 风格 dict：{role, content, tool_calls?, tool_call_id?, name?}。"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: Optional[str]
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    provider: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    raw_assistant_content: Any = None  # Anthropic 原始内容块（含 thinking），用于回放

    def to_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.raw_assistant_content:
            msg["_raw_content"] = self.raw_assistant_content
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in self.tool_calls
            ]
        return msg


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class LLMUnavailable(Exception):
    """所有模型（主 + 备）均不可用，调用方应降级到模板模式。"""


class LLMClient(ABC):
    name: str = "llm"
    model: str = ""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResponse: ...


def parse_json_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        # 容错：截断 / 单引号
        try:
            import ast

            v = ast.literal_eval(raw)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            return {"_raw": str(raw)}


def extract_json_object(text: str) -> Optional[dict]:
    """从模型输出中提取第一个 JSON 对象（兼容 ```json 包裹）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start : i + 1])
                        if isinstance(v, dict):
                            return v
                    except Exception:
                        break
        start = t.find("{", start + 1)
    return None
