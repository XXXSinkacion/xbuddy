"""运行配置：全部来自环境变量 / .env，不在代码中硬编码任何密钥。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    # ---- LLM ----
    llm_provider: str = "none"  # openai_compat | anthropic | none
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: str = "deepseek-chat"
    llm_price_input_per_m: float = 0.27
    llm_price_output_per_m: float = 1.10

    llm_fallback_provider: Optional[str] = None
    llm_fallback_base_url: Optional[str] = None
    llm_fallback_api_key: Optional[str] = None
    llm_fallback_model: Optional[str] = None

    # ---- 扶摇 ----
    fuyao_api_key: Optional[str] = None
    fuyao_base_url: str = "https://fuyao.aicubes.cn"
    fuyao_mode: str = "auto"  # auto | live | fixture

    # ---- iFinD MCP ----
    ifind_mcp_url: Optional[str] = None
    ifind_mcp_authorization: Optional[str] = None

    # ---- Harness 默认预算 ----
    harness_max_llm_steps: int = 40
    harness_max_tool_calls: int = 60
    harness_max_cost_usd: float = 0.5
    harness_max_duration_s: int = 600
    harness_context_budget_tokens: int = 24000
    harness_plan_approval: str = "auto"  # always | auto | never
    harness_memory_write_approval: bool = True

    # ---- 服务 ----
    port: int = 8000
    data_dir: str = "./data"
    log_level: str = "INFO"
    enable_fault_injection: bool = True
    fault_ttl_s: int = 300  # 注入的故障自动失效（秒）；0 表示不过期
    # 公开部署保护：全局并发运行上限、每日模型成本上限（超出后新运行降级为模板模式，不再调用模型）
    max_concurrent_runs: int = 3
    daily_llm_cost_cap_usd: float = 2.0

    model_config = SettingsConfigDict(
        env_file=(str(ROOT_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 派生属性 ----
    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider in ("openai_compat", "anthropic") and bool(self.llm_api_key)

    @property
    def fallback_llm_enabled(self) -> bool:
        return bool(self.llm_fallback_provider) and bool(self.llm_fallback_api_key)

    @property
    def fuyao_effective_mode(self) -> str:
        if self.fuyao_mode == "live":
            return "live"
        if self.fuyao_mode == "fixture":
            return "fixture"
        return "live" if self.fuyao_api_key else "fixture"

    @property
    def ifind_enabled(self) -> bool:
        return bool(self.ifind_mcp_url)

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = ROOT_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
