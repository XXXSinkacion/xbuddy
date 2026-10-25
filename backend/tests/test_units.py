"""单元：上下文压缩、合规 Linter、工具管线（校验 / 缓存 / 重试 / 熔断 / 备用链）、模板规划。"""
import asyncio

import pytest

from app.compliance.linter import lint, scan_forbidden
from app.harness.context import ContextManager, estimate_tokens, messages_tokens, render_tool_result
from app.harness.planner import extract_targets, latest_report_period, template_plan
from app.models import RunState, ToolResult
from app.tools.cache import CircuitBreaker, FaultInjector, TTLCache
from app.tools.registry import ToolContext, ToolError, ToolRegistry, ToolRunner, ToolSpec


def _tool_msg(label, text, n=400):
    return {"role": "tool", "tool_call_id": f"c_{label}", "name": "x", "content": f"证据编号: {label}\n状态: ok\n摘要: {text}\n数据预览:\n" + ("数据" * n)}


def test_context_compression_folds_oldest_groups_and_keeps_pairs():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(1, 7):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c_E{i}", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
        msgs.append(_tool_msg(f"E{i}", f"第{i}条摘要"))
    before = messages_tokens(msgs)
    cm = ContextManager(budget_tokens=before // 2, keep_last_groups=2)
    out, stats = cm.compress_if_needed(msgs)
    assert stats and stats["after"] < stats["before"] and stats["mode"] == "fold"
    assert out[0]["role"] == "system" and out[1]["role"] == "user"
    assert "已压缩的早期观察摘要" in out[2]["content"] and "[E1]" in out[2]["content"] and "第1条摘要" in out[2]["content"]
    # 最后两组 assistant+tool 配对保留
    assert out[-4]["role"] == "assistant" and out[-3]["role"] == "tool" and out[-2]["role"] == "assistant" and out[-1]["role"] == "tool"
    assert "E5" in out[-3]["content"] and "E6" in out[-1]["content"]
    # 预算内不压缩
    out2, stats2 = ContextManager(budget_tokens=10**9).compress_if_needed(msgs)
    assert stats2 is None and out2 is msgs


def test_render_tool_result_mentions_label_and_units():
    res = ToolResult(ok=True, data={"item": [{"thscode": "600519.SH", "last_price": 1.0}]}, summary="s", source="fuyao", data_timestamp=1700000000000, unit_notes="元")
    txt = render_tool_result(res, "E3")
    assert "证据编号: E3" in txt and "单位/口径: 元" in txt and "get_evidence" in txt
    assert estimate_tokens("中文中文中文") > estimate_tokens("ab")


def test_linter_removes_forbidden_marks_uncited_and_adds_sections():
    ev = [{"label": "E1", "tool": "get_quote_snapshot", "source": "fuyao", "status": "ok", "fetched_at": 1, "data_timestamp": 1700000000000, "unit_notes": "元", "summary": "ok"}, {"label": "E2", "tool": "get_kline", "source": "fuyao", "status": "missing", "fetched_at": 1, "error": "无数据"}]
    md = "# r\n## 摘要\nx\n## 核心结论\n- 【事实】价格 100 元 [E1]\n- 该股必涨，建议买入 [E1]\n- 【推断】没有引用的推断\n- 【事实】引用不存在 [E9]\n"
    res = lint(md, ev)
    assert not res.ok
    assert "必涨" not in res.md and "建议买入" not in res.md and "已按合规规则移除" in res.md
    assert "【不确定·无证据引用】" in res.md
    assert "[引用无效]" in res.md
    for sec in ("## 数据口径与时点", "## 数据缺失与限制", "## 证据清单", "## 免责声明"):
        assert sec in res.md
    assert "[E2] get_kline — 数据缺失" in res.md
    assert res.stats["forbidden_removed"] == 1 and res.stats["uncited"] >= 1 and res.stats["bad_citations"] == 1
    assert scan_forbidden("预计将大幅上涨") and scan_forbidden("目标价 100 元") and not scan_forbidden("区间涨跌 5.64%")
    # 否定语境是合规声明，不是违规（live 实测：摘要里的“不输出…目标价”曾被误删）
    assert not scan_forbidden("本报告不输出涨跌预测、买卖建议或目标价，仅做数据整理")
    assert not scan_forbidden("本任务不构成交易建议，也不提供目标价 [E1]")
    assert scan_forbidden("不要犹豫，目标价 1500 元") and scan_forbidden("有望大幅上涨")


@pytest.mark.asyncio
async def test_tool_runner_validation_cache_retry_breaker_fallback():
    reg = ToolRegistry()
    calls = {"a": 0, "b": 0}

    async def a(args, ctx):
        calls["a"] += 1
        if args.get("fail"):
            raise ToolError("数据源超时", retryable=True, transport=True)
        return ToolResult(ok=True, data={"v": args["x"]}, summary="ok", source="src_a")

    async def b(args, ctx):
        calls["b"] += 1
        return ToolResult(ok=True, data={"v": "from_b"}, summary="b ok", source="src_b")

    reg.register(ToolSpec(name="a", description="", parameters={"type": "object", "properties": {"x": {"type": "integer"}, "fail": {"type": "integer"}}, "required": ["x"]}, handler=a, source="src_a", cacheable=True, cache_ttl_s=60, fallbacks=("b",), max_retries=1))
    reg.register(ToolSpec(name="b", description="", parameters={"type": "object", "properties": {}}, handler=b, source="src_b"))
    runner = ToolRunner(reg)
    events = []
    ctx = ToolContext(run_id="r", task_id=None, settings=None, evidence=None, emit=lambda t, p: events.append(t), working_memory={}, cache=TTLCache(), breaker=CircuitBreaker(threshold=2, window_s=60, open_s=60), faults=FaultInjector(), registry=reg)

    res, _ = await runner.invoke("a", {}, ctx)
    assert not res.ok and "缺少必填参数" in res.error
    res, _ = await runner.invoke("a", {"x": "7"}, ctx)  # 字符串整数被转换
    assert res.ok and res.data["v"] == 7 and calls["a"] == 1
    res, _ = await runner.invoke("a", {"x": 7}, ctx)
    assert res.ok and res.meta.get("cached") and calls["a"] == 1
    # 失败：重试 1 次后走备用链 b
    res, _ = await runner.invoke("a", {"x": 1, "fail": 1}, ctx)
    assert res.ok and res.data["v"] == "from_b" and res.meta.get("fallback_of") == "a"
    assert "tool.retry" in events and "tool.fallback" in events
    assert calls["a"] == 3  # 1 成功 + 2 次失败尝试
    # 熔断已打开（2 次失败）：直接短路
    assert ctx.breaker.is_open("src_a")
    n = calls["a"]
    res, _ = await runner.invoke("a", {"x": 2, "fail": 1}, ctx)
    assert calls["a"] == n and res.ok and res.data["v"] == "from_b"
    # 过期缓存兜底：同参数曾成功 + 无备用链
    reg.register(ToolSpec(name="c", description="", parameters={"type": "object", "properties": {"x": {"type": "integer"}}}, handler=a, source="src_c", cacheable=True, cache_ttl_s=0.0001, max_retries=0))
    res, _ = await runner.invoke("c", {"x": 1}, ctx)
    assert res.ok
    await asyncio.sleep(0.01)
    res, _ = await runner.invoke("c", {"x": 1, "fail": 1}, ctx)
    assert res.ok is True and res.status == "stale" or (not res.ok)
    # 故障注入
    ctx.faults.set("src_b", "timeout")
    res, _ = await runner.invoke("b", {}, ctx)
    assert not res.ok and res.meta.get("injected")


def test_template_plan_detects_targets_and_intents():
    codes, names = extract_targets("研究贵州茅台(600519.SH)近3个月行情、估值与最新财务指标，并与白酒板块对比")
    assert codes == ["600519.SH"] and names in ([], ["贵州茅台"])
    codes, names = extract_targets("对比宁德时代与比亚迪的估值与盈利能力")
    assert "宁德时代" in names and "比亚迪" in names
    reg = ToolRegistry()
    from app.tools.analysis import register_analysis_tools
    from app.tools.fuyao import FuyaoBackend, register_fuyao_tools

    register_fuyao_tools(reg, FuyaoBackend(mode="fixture", base_url="x", api_key=None))
    register_analysis_tools(reg)
    plan = template_plan("今日市场情绪复盘：涨停池、连板统计、热股榜、主要指数表现", reg)
    assert plan.mode == "template" and any("情绪" in t.title for t in plan.tasks)
    plan2 = template_plan("研究贵州茅台(600519.SH)估值并与白酒板块对比", reg)
    titles = [t.title for t in plan2.tasks]
    assert any("标的" in t for t in titles) and any("估值" in t for t in titles) and any("白酒" in t for t in titles)
    assert all(t.pipeline for t in plan2.tasks)
    assert len(latest_report_period()) == 3


def test_run_state_roundtrip():
    s = RunState(phase="execute", working_memory={"a": 1})
    s2 = RunState(**s.model_dump())
    assert s2.working_memory == {"a": 1} and s2.phase == "execute"


async def test_breaker_scopes_business_error_does_not_block_sibling_tools():
    """live 实测暴露的问题：某端点返回 5003（业务错误）后，同数据源其它端点不应被连坐熔断；
    传输层失败（超时 / 5xx）才计入数据源级熔断。"""
    reg = ToolRegistry()
    calls = {"bad": 0, "good": 0, "flaky": 0}

    async def bad(args, ctx):
        calls["bad"] += 1
        raise ToolError("扶摇 code=5003：Data source unavailable", retryable=True, code=5003)

    async def good(args, ctx):
        calls["good"] += 1
        return ToolResult(ok=True, data={"v": 1}, summary="ok", source="fuyao")

    async def flaky(args, ctx):
        calls["flaky"] += 1
        raise ToolError("扶摇请求超时", retryable=True, transport=True)

    for name, h in (("bad", bad), ("good", good), ("flaky", flaky)):
        reg.register(ToolSpec(name=name, description="", parameters={"type": "object", "properties": {"k": {"type": "integer"}}}, handler=h, source="fuyao", cacheable=False, max_retries=2))
    runner = ToolRunner(reg)
    ctx = ToolContext(run_id="r", task_id=None, settings=None, evidence=None, emit=lambda t, p: None, working_memory={}, cache=TTLCache(), breaker=CircuitBreaker(threshold=3, window_s=60, open_s=60), faults=FaultInjector(), registry=reg)

    # 一次业务失败（含 2 次重试）：端点级只记 1 次，不开；数据源级不计
    res, _ = await runner.invoke("bad", {"k": 1}, ctx)
    assert not res.ok and calls["bad"] == 3
    assert not ctx.breaker.is_open("fuyao") and not ctx.breaker.is_open("fuyao:bad")
    res, _ = await runner.invoke("good", {"k": 1}, ctx)
    assert res.ok and calls["good"] == 1
    # 三次独立失败调用后端点级熔断打开；其它端点仍可用
    await runner.invoke("bad", {"k": 2}, ctx)
    await runner.invoke("bad", {"k": 3}, ctx)
    assert ctx.breaker.is_open("fuyao:bad") and not ctx.breaker.is_open("fuyao")
    n = calls["bad"]
    res, _ = await runner.invoke("bad", {"k": 4}, ctx)
    assert calls["bad"] == n and res.meta.get("circuit_open") and "接口 bad 熔断中" in res.error
    res, _ = await runner.invoke("good", {"k": 2}, ctx)
    assert res.ok and calls["good"] == 2
    # 传输层失败：一次调用内 3 次超时即判定数据源不可用，所有端点短路
    res, _ = await runner.invoke("flaky", {"k": 1}, ctx)
    assert not res.ok and calls["flaky"] == 3 and ctx.breaker.is_open("fuyao")
    res, _ = await runner.invoke("good", {"k": 3}, ctx)
    assert not res.ok and res.meta.get("circuit_open") and calls["good"] == 2
