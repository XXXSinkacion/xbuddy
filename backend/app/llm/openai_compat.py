"""OpenAI 兼容客户端：DeepSeek / Qwen(DashScope) / Kimi / GLM / OpenAI / OpenRouter 等。"""
from __future__ import annotations

import time
from typing import Optional

from .base import LLMClient, LLMError, LLMResponse, ToolCall, parse_json_args


class OpenAICompatClient(LLMClient):
    def __init__(self, *, base_url: Optional[str], api_key: str, model: str, timeout: float = 90.0, name: str = "openai_compat"):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=base_url or None, api_key=api_key, timeout=timeout, max_retries=0)
        self.model = model
        self.name = name

    async def chat(self, messages, tools=None, *, temperature=0.2, max_tokens=2048, json_mode=False) -> LLMResponse:
        import openai

        clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        kwargs: dict = {"model": self.model, "messages": clean, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        t0 = time.time()
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            raise LLMError(f"限流 429: {e}", retryable=True, status=429) from e
        except (openai.APITimeoutError, openai.APIConnectionError) as e:
            raise LLMError(f"网络/超时: {e}", retryable=True) from e
        except openai.InternalServerError as e:
            raise LLMError(f"服务端错误: {e}", retryable=True, status=500) from e
        except openai.AuthenticationError as e:
            raise LLMError(f"鉴权失败: {e}", retryable=False, status=401) from e
        except openai.APIStatusError as e:
            retry = e.status_code in (408, 409, 425, 429, 500, 502, 503, 504)
            raise LLMError(f"API 错误 {e.status_code}: {e}", retryable=retry, status=e.status_code) from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"未知错误: {type(e).__name__}: {e}", retryable=False) from e

        latency = int((time.time() - t0) * 1000)
        if not resp.choices:
            raise LLMError("模型未返回 choices", retryable=True)
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            tool_calls.append(ToolCall(id=tc.id or f"call_{len(tool_calls)}", name=fn.name, arguments=parse_json_args(fn.arguments)))
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            model=resp.model or self.model,
            finish_reason=choice.finish_reason or "",
            provider=self.name,
            latency_ms=latency,
        )
