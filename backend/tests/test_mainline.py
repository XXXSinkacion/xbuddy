"""主链路：模板模式与 LLM（mock）模式端到端。"""
import pytest

from app.llm.mock import MockLLM
from tests.conftest import install_llm, make_state, scripted_handler, wait_run

GOAL = "研究贵州茅台(600519.SH)近3个月行情、估值与最新财务指标，并与白酒板块对比"


async def test_template_mode_full_chain(tmp_path):
    st = make_state(tmp_path)
    try:
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], GOAL)
        assert run["mode"] == "template"
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "completed", r
        assert r["phase"] == "done"
        assert len(r["tasks"]) >= 4
        assert all(t["status"] in ("done", "partial") for t in r["tasks"]), [(t["title"], t["status"]) for t in r["tasks"]]
        ev = st.harness.evidence.list(run["id"])
        assert len(ev) >= 8
        assert {e["tool"] for e in ev} >= {"search_ticker", "get_kline", "compute_price_stats", "get_valuation_snapshot", "get_financial_indicators"}
        # 证据字段完整：来源 / 时点或抓取时间 / 单位口径
        for e in ev:
            assert e["source"] and e["fetched_at"] and e["label"].startswith("E")
        assert r["artifacts"], "应产出报告"
        art = st.db.get("artifacts", r["artifacts"][0]["id"])
        md = art["content_md"]
        for sec in ("## 核心结论", "## 数据口径与时点", "## 数据缺失与限制", "## 证据清单", "## 免责声明"):
            assert sec in md, sec
        assert "演示数据" in md  # fixture 必须被标注
        assert r["usage"]["tool_calls"] >= 8 and r["usage"]["llm_calls"] == 0
        # 记忆写入需审批
        pend = [a for a in r["approvals"] if a["kind"] == "memory_write" and a["status"] == "pending"]
        assert pend
        before = len(st.harness.memory.list())
        await st.harness.decide_approval(pend[0]["id"], True)
        assert len(st.harness.memory.list()) > before
        # 事件溯源可回放
        types = {e["type"] for e in st.bus.replay(thr["id"], 0)}
        assert {"run.created", "plan.created", "task.start", "task.end", "tool.end", "evidence.recorded", "run.checkpoint", "artifact.created", "run.completed"} <= types
    finally:
        await st.close()


async def test_llm_mode_with_tool_calls_and_compliance_rewrite(tmp_path):
    st = make_state(tmp_path)
    try:
        mock = MockLLM(handler=scripted_handler())
        install_llm(st, mock, pricing={"default": (1.0, 2.0)})
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        assert run["mode"] == "llm"
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "completed", r
        assert [t["status"] for t in r["tasks"]] == ["done", "done"]
        assert r["usage"]["llm_calls"] >= 4  # plan + 2 tasks(各≥2 步) + synth
        assert r["usage"]["cost_usd"] > 0
        ev = st.harness.evidence.list(run["id"])
        tools = [e["tool"] for e in ev]
        assert "search_ticker" in tools and "get_valuation_snapshot" in tools
        art = st.db.get("artifacts", r["artifacts"][0]["id"])
        md = art["content_md"]
        assert "建议买入" not in md and "有望上涨" not in md
        assert "【不确定·无证据引用】" in md or "结论无引用的句子" not in md
        types = [e["type"] for e in st.bus.replay(thr["id"], 0)]
        assert "compliance.rewrite" in types
        assert any(t == "llm.end" for t in types)
        # 任务消息里应有动态 UI 块
        msgs = st.db.fetchall("SELECT blocks_json FROM messages WHERE run_id = ?", [run["id"]])
        joined = " ".join(m["blocks_json"] for m in msgs)
        assert '"type": "metrics"' in joined and '"type": "table"' in joined and '"type": "plan"' in joined and '"type": "artifact_ref"' in joined
    finally:
        await st.close()


async def test_thread_history_feeds_next_run(tmp_path):
    st = make_state(tmp_path)
    try:
        thr = st.harness.create_thread("t")
        run1 = await st.harness.start_run(thr["id"], "研究贵州茅台估值")
        await wait_run(st.harness, run1["id"])
        hist = st.harness.memory.thread_history(thr["id"])
        assert hist and hist[0]["goal"] == "研究贵州茅台估值" and hist[0]["summary"]
    finally:
        await st.close()
