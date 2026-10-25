"""装配：配置 → 数据库 / 事件总线 / 工具注册 / LLM 路由 / Harness。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Settings
from .db import Database
from .events import EventBus
from .harness.runtime import Harness
from .llm.base import LLMClient
from .llm.router import LLMRouter
from .tools.analysis import register_analysis_tools
from .tools.cache import FaultInjector
from .tools.fuyao import FuyaoBackend, register_fuyao_tools
from .tools.memory_tools import register_memory_tools
from .tools.registry import ToolRegistry

log = logging.getLogger("xbuddy.factory")


def make_llm_client(provider: Optional[str], base_url: Optional[str], api_key: Optional[str], model: Optional[str], name: str) -> Optional[LLMClient]:
    if not provider or provider == "none" or not api_key:
        return None
    if provider == "openai_compat":
        from .llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(base_url=base_url, api_key=api_key, model=model or "deepseek-chat", name=name)
    if provider == "anthropic":
        from .llm.anthropic_client import AnthropicClient

        return AnthropicClient(api_key=api_key, model=model or "claude-opus-5-5", base_url=base_url, name=name)
    log.warning("unknown LLM provider %s", provider)
    return None


def build_llm_router(settings: Settings, faults: FaultInjector) -> LLMRouter:
    primary = make_llm_client(settings.llm_provider, settings.llm_base_url, settings.llm_api_key, settings.llm_model, "primary")
    fallback = make_llm_client(settings.llm_fallback_provider, settings.llm_fallback_base_url, settings.llm_fallback_api_key, settings.llm_fallback_model, "fallback")
    pricing = {"primary": (settings.llm_price_input_per_m, settings.llm_price_output_per_m), "fallback": (settings.llm_price_input_per_m, settings.llm_price_output_per_m), "default": (settings.llm_price_input_per_m, settings.llm_price_output_per_m)}
    return LLMRouter(primary, fallback, pricing=pricing, faults=faults)


def build_registry(settings: Settings) -> tuple[ToolRegistry, FuyaoBackend]:
    registry = ToolRegistry()
    backend = FuyaoBackend(mode=settings.fuyao_effective_mode, base_url=settings.fuyao_base_url, api_key=settings.fuyao_api_key)
    register_fuyao_tools(registry, backend)
    register_analysis_tools(registry)
    register_memory_tools(registry)
    return registry, backend


@dataclass
class AppState:
    settings: Settings
    db: Database
    bus: EventBus
    registry: ToolRegistry
    fuyao: FuyaoBackend
    llm: LLMRouter
    faults: FaultInjector
    harness: Harness
    ifind: dict = field(default_factory=lambda: {"ok": False, "count": 0, "error": None, "configured": False})

    def capabilities(self) -> dict:
        s = self.settings
        return {
            "llm": self.llm.describe() | {"mode": "llm" if self.llm.available else "template"},
            "fuyao": {"mode": s.fuyao_effective_mode, "base_url": s.fuyao_base_url, "configured": bool(s.fuyao_api_key)},
            "ifind": self.ifind,
            "tools": [t.public() for t in self.registry.specs()],
            "budget_defaults": self.harness.default_budget().model_dump(),
            "fault_injection": s.enable_fault_injection,
            "faults": self.faults.state(),
            "breaker": self.harness.breaker.state(),
        }

    async def close(self) -> None:
        await self.fuyao.aclose()
        self.db.close()


def build_app_state(settings: Optional[Settings] = None, *, db_path: Optional[str] = None) -> AppState:
    settings = settings or Settings()
    data_dir = settings.data_path
    db = Database(db_path or data_dir / "xbuddy.sqlite")
    bus = EventBus(db, log_dir=data_dir / "logs")
    faults = FaultInjector(enabled=settings.enable_fault_injection)
    registry, fuyao = build_registry(settings)
    llm = build_llm_router(settings, faults)
    harness = Harness(db=db, bus=bus, registry=registry, llm=llm, settings=settings, faults=faults)
    state = AppState(settings=settings, db=db, bus=bus, registry=registry, fuyao=fuyao, llm=llm, faults=faults, harness=harness)
    state.ifind["configured"] = settings.ifind_enabled
    return state


async def connect_ifind(state: AppState) -> dict:
    s = state.settings
    if not s.ifind_enabled:
        state.ifind = {"ok": False, "count": 0, "error": "未配置 IFIND_MCP_URL", "configured": False}
        return state.ifind
    from .tools.ifind import register_ifind_tools

    res = await register_ifind_tools(state.registry, s.ifind_mcp_url or "", s.ifind_mcp_authorization)
    state.ifind = res | {"configured": True}
    return state.ifind
