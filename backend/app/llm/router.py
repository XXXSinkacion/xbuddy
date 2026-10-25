"""LLM 路由：主模型 → 指数退避重试 → 备用模型 → 抛出 LLMUnavailable（调用方降级到模板模式）。
同时负责 token / 成本 / 时延统计与事件。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from ..tools.cache import FaultInjector
from .base import LLMClient, LLMError, LLMResponse, LLMUnavailable

log = logging.getLogger("xbuddy.llm")


class LLMRouter:
    def __init__(
        self,
        primary: Optional[LLMClient],
        fallback: Optional[LLMClient] = None,
        *,
        pricing: Optional[dict[str, tuple[float, float]]] = None,  # name -> (input $/M, output $/M)
        faults: Optional[FaultInjector] = None,
        max_attempts: int = 3,
        base_backoff_s: float = 0.6,
    ):
        self.primary = primary
        self.fallback = fallback
        self.pricing = pricing or {}
        self.faults = faults
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s

    @property
    def available(self) -> bool:
        return self.primary is not None or self.fallback is not None

    def describe(self) -> dict:
        return {
            "available": self.available,
            "primary": {"name": self.primary.name, "model": self.primary.model} if self.primary else None,
            "fallback": {"name": self.fallback.name, "model": self.fallback.model} if self.fallback else None,
        }

    def _cost(self, client: LLMClient, inp: int, out: int) -> float:
        price = self.pricing.get(client.name) or self.pricing.get("default") or (0.0, 0.0)
        return inp / 1_000_000 * price[0] + out / 1_000_000 * price[1]

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        *,
        purpose: str = "",
        on_event: Optional[Callable[[str, dict], None]] = None,
        **kw,
    ) -> LLMResponse:
        if not self.available:
            raise LLMUnavailable("未配置任何模型")
        if self.faults and self.faults.active("llm"):
            if on_event:
                on_event("llm.error", {"purpose": purpose, "provider": "injected", "error": "注入故障：模型不可用", "attempt": 1})
            raise LLMUnavailable("注入故障：模型不可用")

        last_err: Optional[Exception] = None
        clients = [c for c in (self.primary, self.fallback) if c is not None]
        for idx, client in enumerate(clients):
            for attempt in range(1, self.max_attempts + 1):
                t0 = time.time()
                if on_event:
                    on_event("llm.start", {"purpose": purpose, "provider": client.name, "model": client.model, "attempt": attempt, "messages": len(messages), "tools": len(tools or [])})
                try:
                    resp = await client.chat(messages, tools, **kw)
                    resp.latency_ms = resp.latency_ms or int((time.time() - t0) * 1000)
                    resp.cost_usd = self._cost(client, resp.input_tokens, resp.output_tokens)
                    resp.provider = client.name
                    if on_event:
                        on_event(
                            "llm.end",
                            {
                                "purpose": purpose,
                                "provider": client.name,
                                "model": resp.model,
                                "attempt": attempt,
                                "input_tokens": resp.input_tokens,
                                "output_tokens": resp.output_tokens,
                                "cost_usd": round(resp.cost_usd, 6),
                                "latency_ms": resp.latency_ms,
                                "tool_calls": [tc.name for tc in resp.tool_calls],
                                "finish_reason": resp.finish_reason,
                                "fallback": idx > 0,
                            },
                        )
                    return resp
                except LLMError as e:
                    last_err = e
                    if on_event:
                        on_event("llm.error", {"purpose": purpose, "provider": client.name, "attempt": attempt, "error": str(e), "retryable": e.retryable})
                    if e.retryable and attempt < self.max_attempts:
                        await asyncio.sleep(self.base_backoff_s * (2 ** (attempt - 1)))
                        continue
                    break  # 换下一个模型
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.exception("llm client crashed")
                    if on_event:
                        on_event("llm.error", {"purpose": purpose, "provider": client.name, "attempt": attempt, "error": repr(e), "retryable": False})
                    break
            if idx + 1 < len(clients) and on_event:
                on_event("llm.fallback", {"purpose": purpose, "from": client.name, "to": clients[idx + 1].name, "reason": str(last_err)})
        raise LLMUnavailable(f"模型均不可用：{last_err}")
