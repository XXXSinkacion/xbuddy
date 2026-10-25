"""审批（计划 / 工具 / 预算）、停止规则、用户停止。"""
import asyncio

from app.llm.mock import MockLLM
from tests.conftest import install_llm, make_state, scripted_handler, wait_run


async def test_plan_approval_reject_and_approve_with_edit(tmp_path):
    st = make_state(tmp_path, harness_plan_approval="always")
    try:
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)行情与估值")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "waiting_approval"
        plan_apr = [a for a in r["approvals"] if a["kind"] == "plan"][0]
        assert plan_apr["status"] == "pending"
        # 拒绝 → 停止并产出部分报告
        await st.harness.decide_approval(plan_apr["id"], False, "不需要")
        r = await wait_run(st.harness, run["id"], until=("stopped", "failed", "completed"))
        assert r["status"] == "stopped" and "拒绝" in (r["stop_reason"] or "")
        assert r["artifacts"]

        # 第二次：批准并移除 t3
        run2 = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)行情与估值")
        r2 = await wait_run(st.harness, run2["id"])
        apr = [a for a in r2["approvals"] if a["kind"] == "plan"][0]
        ids = [t["id"].split(":")[1] for t in r2["tasks"]]
        assert "t3" in ids
        await st.harness.decide_approval(apr["id"], True, "去掉 t3")
        r2 = await wait_run(st.harness, run2["id"], until=("completed", "failed", "stopped"))
        assert r2["status"] == "completed"
        t3 = [t for t in r2["tasks"] if t["id"].endswith(":t3")][0]
        assert t3["status"] == "skipped"
        assert not any(e["tool"] == "get_valuation_snapshot" for e in st.harness.evidence.list(run2["id"]))
    finally:
        await st.close()


async def test_tool_approval_l2_approve_and_reject(tmp_path):
    seq = {"t1": [("search_ticker", {"q": "贵州茅台"}), ("save_memory", {"kind": "note", "content": "测试写入", "tags": "test"})], "t2": [("get_quote_snapshot", {"thscodes": "600519.SH"})]}
    st = make_state(tmp_path)
    try:
        install_llm(st, MockLLM(handler=scripted_handler(tool_sequence=seq)))
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "waiting_approval"
        apr = [a for a in r["approvals"] if a["kind"] == "tool"][0]
        assert apr["payload"]["tool"] == "save_memory" and apr["payload"]["risk"] == "L2"
        before = len(st.harness.memory.list())
        await st.harness.decide_approval(apr["id"], True, "ok")
        r = await wait_run(st.harness, run["id"], until=("completed", "failed", "stopped"))
        assert r["status"] == "completed"
        assert len(st.harness.memory.list()) == before + 1
        # 写操作可审计（审批记录 + tool.end 事件），但不是数据，不生成可被 [E#] 引用的证据
        assert not any(e["tool"] == "save_memory" for e in st.harness.evidence.list(run["id"]))
        assert any(e["type"] == "tool.end" and e["payload"].get("tool") == "save_memory" and e["payload"].get("ok") for e in st.bus.replay(thr["id"], 0, run_id=run["id"]))

        # 拒绝路径
        run2 = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r2 = await wait_run(st.harness, run2["id"])
        apr2 = [a for a in r2["approvals"] if a["kind"] == "tool" and a["status"] == "pending"][0]
        before = len(st.harness.memory.list())
        await st.harness.decide_approval(apr2["id"], False, "不要写")
        r2 = await wait_run(st.harness, run2["id"], until=("completed", "failed", "stopped"))
        assert r2["status"] == "completed"
        assert len(st.harness.memory.list()) == before
        assert "tool.rejected" in {e["type"] for e in st.bus.replay(thr["id"], 0)}
    finally:
        await st.close()


async def test_stop_rule_max_tool_calls_yields_partial_report(tmp_path):
    st = make_state(tmp_path, harness_max_tool_calls=3)
    try:
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)行情、估值与财务")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "stopped", r
        assert "工具调用上限" in (r["stop_reason"] or "")
        assert r["usage"]["tool_calls"] <= 3
        assert r["artifacts"]
        md = st.db.get("artifacts", r["artifacts"][0]["id"])["content_md"]
        assert "运行未完成" in md
        assert any(t["status"] in ("skipped", "failed") for t in r["tasks"])
    finally:
        await st.close()


async def test_budget_warning_approval_extends_budget(tmp_path):
    # 固定每次调用 1000 input tokens × $1/M = $0.001；上限 $0.0025 → 第 2 次调用后达到 80% 触发预算审批
    st = make_state(tmp_path, harness_max_cost_usd=0.0025)
    try:
        h = scripted_handler()

        def fixed_usage(messages, tools):
            r = h(messages, tools)
            r.input_tokens, r.output_tokens = 1000, 1
            return r

        install_llm(st, MockLLM(handler=fixed_usage), pricing={"default": (1.0, 0.0)})
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "waiting_approval", r
        apr = [a for a in r["approvals"] if a["kind"] == "budget"][0]
        await st.harness.decide_approval(apr["id"], True)
        r = await wait_run(st.harness, run["id"], until=("completed", "failed", "stopped"))
        assert r["budget"]["max_cost_usd"] > 0.0025
        assert r["status"] in ("completed", "stopped")
        assert "budget.extended" in {e["type"] for e in st.bus.replay(thr["id"], 0)}
    finally:
        await st.close()


async def test_user_stop_while_waiting_approval(tmp_path):
    st = make_state(tmp_path, harness_plan_approval="always")
    try:
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)")
        await wait_run(st.harness, run["id"])
        r = await st.harness.stop(run["id"])
        r = await wait_run(st.harness, run["id"], until=("stopped", "failed", "completed"))
        assert r["status"] == "stopped"
    finally:
        await st.close()
