import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PYTHONIOENCODING"] = "utf-8"

from app.config import Settings  # noqa: E402
from app.factory import build_app_state  # noqa: E402
from app.harness.planner import Planner  # noqa: E402
from app.harness.synthesizer import Synthesizer  # noqa: E402
from app.llm.base import LLMResponse, ToolCall  # noqa: E402
from app.llm.mock import MockLLM  # noqa: E402
from app.llm.router import LLMRouter  # noqa: E402

TERMINAL = ("completed", "failed", "stopped", "waiting_approval", "interrupted")


def make_settings(tmp_path, **kw) -> Settings:
    base = dict(llm_provider="none", llm_api_key=None, llm_fallback_provider=None, fuyao_mode="fixture", fuyao_api_key=None, ifind_mcp_url=None, data_dir=str(tmp_path), harness_plan_approval="never", harness_memory_write_approval=True, enable_fault_injection=True, harness_max_duration_s=120)
    base.update(kw)
    return Settings(**base)


def make_state(tmp_path, **kw):
    st = build_app_state(make_settings(tmp_path, **kw), db_path=str(tmp_path / "t.sqlite"))
    return st


def install_llm(state, primary, fallback=None, pricing=None):
    router = LLMRouter(primary, fallback, pricing=pricing or {"default": (1.0, 2.0)}, faults=state.faults)
    state.llm = router
    state.harness.llm = router
    state.harness.planner = Planner(router, state.registry)
    state.harness.synthesizer = Synthesizer(router)
    return router


async def wait_run(harness, run_id, timeout=30, until=TERMINAL):
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    while True:
        r = harness.get_run(run_id)
        if r["status"] in until:
            # 让后台任务收尾
            await asyncio.sleep(0.05)
            return harness.get_run(run_id)
        if loop.time() - t0 > timeout:
            raise TimeoutError(f"run {run_id} still {r['status']}")
        await asyncio.sleep(0.05)


# ------------------------------------------------------------- 脚本化 LLM
PLAN_JSON = {
    "title": "贵州茅台研究（测试）",
    "assumptions": ["测试计划"],
    "tasks": [
        {"id": "t1", "title": "标的识别", "objective": "用 search_ticker 确认贵州茅台的 thscode", "tools": ["search_ticker", "note_fact"], "depends_on": [], "acceptance": "拿到 thscode"},
        {"id": "t2", "title": "行情与估值", "objective": "获取行情快照与估值快照", "tools": ["get_quote_snapshot", "get_valuation_snapshot", "get_kline", "compute_price_stats"], "depends_on": ["t1"], "acceptance": "快照与估值证据"},
    ],
}

REPORT_WITH_VIOLATION = """# 贵州茅台研究
## 摘要
基于 {labels} 的测试报告。
## 核心结论
- 【事实】贵州茅台最新价与估值已获取 [{first}]
- 【推断】该股有望上涨，建议买入 [{first}]
- 结论无引用的句子
## 数据口径与时点
见证据。
## 数据缺失与限制
无。
"""

REPORT_CLEAN = """# 贵州茅台研究
## 摘要
基于 {labels} 的测试报告（已重写）。
## 核心结论
- 【事实】贵州茅台最新价与估值已获取 [{first}]
- 【推断】估值倍数处于证据给出的区间，口径为 TTM [{first}]
- 【不确定】无更多信息
## 数据口径与时点
见证据。
## 数据缺失与限制
无。
"""


def labels_in(messages):
    import re

    labels = []
    for m in messages:
        if m.get("role") == "tool":
            mm = re.search(r"证据编号:\s*(E\d+)", str(m.get("content")))
            if mm:
                labels.append(mm.group(1))
    return labels


def scripted_handler(*, tool_sequence=None, report_first=REPORT_WITH_VIOLATION, report_second=REPORT_CLEAN):
    """按消息内容分发：规划 / 执行 / 报告。tool_sequence 覆盖执行器的工具调用顺序。"""
    seq = tool_sequence or {
        "t1": [("search_ticker", {"q": "贵州茅台"}), ("note_fact", {"key": "target", "value": "600519.SH"})],
        "t2": [("get_quote_snapshot", {"thscodes": "600519.SH"}), ("get_valuation_snapshot", {"thscodes": "600519.SH"})],
    }
    calls = {"synth": 0}

    def handler(messages, tools):
        system = str(messages[0].get("content", ""))
        if "规划器" in system:
            return LLMResponse(content=json.dumps(PLAN_JSON, ensure_ascii=False))
        if "撰写器" in system:
            calls["synth"] += 1
            labels = labels_in(messages)  # 报告阶段没有 tool 消息；从用户消息里抓 E#
            import re

            found = re.findall(r"\[(E\d+)\]", str(messages[1].get("content", "")))
            first = found[0] if found else "E1"
            tpl = report_first if calls["synth"] == 1 else report_second
            return LLMResponse(content=tpl.format(labels=", ".join(found[:5]), first=first))
        # 执行器：根据当前任务 id 与已完成的工具调用数决定下一步
        user = str(messages[1].get("content", ""))
        import re

        tid = None
        for k in seq:
            if f"当前任务：" in user:
                pass
        # 任务标题映射
        title_map = {"标的识别": "t1", "行情与估值": "t2"}
        for title, k in title_map.items():
            if f"当前任务：{title}" in user:
                tid = k
        if tid is None:
            tid = list(seq.keys())[0]
        done = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
        steps = seq[tid]
        if done < len(steps):
            name, args = steps[done]
            return LLMResponse(content="", tool_calls=[ToolCall(id=f"call_{tid}_{done}", name=name, arguments=args)])
        labels = labels_in(messages)
        cites = " ".join(f"[{l}]" for l in labels) or "[E1]"
        return LLMResponse(content=f"【事实】任务 {tid} 完成，已获取证据 {cites}\n证据：{', '.join(labels)}")

    return handler


@pytest.fixture
def tmp_state(tmp_path):
    st = make_state(tmp_path)
    yield st
    asyncio.get_event_loop().run_until_complete(st.close()) if False else None
