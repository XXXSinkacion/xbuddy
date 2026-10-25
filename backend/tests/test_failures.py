"""数据缺失 / 接口失败 / 模型失败的降级与恢复。"""
from app.llm.mock import AlwaysFailLLM, MockLLM
from tests.conftest import install_llm, make_state, scripted_handler, wait_run


async def test_fuyao_outage_is_reported_not_hidden(tmp_path):
    st = make_state(tmp_path)
    try:
        st.faults.set("fuyao", "error")
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)行情与估值，并复盘今日市场情绪")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "completed"  # 不崩溃，产出带缺失说明的报告
        ev = st.harness.evidence.list(run["id"])
        assert ev and all(e["status"] == "error" for e in ev)
        md = st.db.get("artifacts", r["artifacts"][0]["id"])["content_md"]
        assert "调用失败" in md or "数据缺失" in md
        assert "【事实】" not in md.split("## 核心结论")[1].split("## ")[0]  # 没有任何事实结论
        # 熔断器应已打开
        assert st.harness.breaker.state().get("fuyao", {}).get("open") is True
        types = {e["type"] for e in st.bus.replay(thr["id"], 0)}
        assert "tool.retry" in types
        # 失败在 UI 块中显式呈现
        msgs = st.db.fetchall("SELECT blocks_json FROM messages WHERE run_id = ?", [run["id"]])
        assert any('"level": "error"' in m["blocks_json"] for m in msgs)
    finally:
        await st.close()


async def test_stale_cache_fallback_after_success(tmp_path):
    st = make_state(tmp_path)
    try:
        thr = st.harness.create_thread("t")
        run1 = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)估值")
        await wait_run(st.harness, run1["id"])
        # 让缓存全部“过期”，再注入故障：应回退到过期缓存并标注 stale
        for k, (ts, v) in list(st.harness.cache._store.items()):
            st.harness.cache._store[k] = (ts - 10 * 86400, v)
        st.faults.set("fuyao", "error")
        run2 = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)估值")
        r2 = await wait_run(st.harness, run2["id"])
        ev = st.harness.evidence.list(run2["id"])
        assert any(e["status"] == "stale" for e in ev), [(e["tool"], e["status"]) for e in ev]
        md = st.db.get("artifacts", r2["artifacts"][0]["id"])["content_md"]
        assert "过期缓存" in md
    finally:
        await st.close()


async def test_missing_ticker_marked_uncertain(tmp_path):
    st = make_state(tmp_path)
    try:
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究不存在公司甲乙丙的估值")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "completed"
        ev = st.harness.evidence.list(run["id"])
        assert any(e["tool"] == "search_ticker" and e["status"] == "missing" for e in ev)
        md = st.db.get("artifacts", r["artifacts"][0]["id"])["content_md"]
        assert "【不确定】" in md and "未检索到" in md
    finally:
        await st.close()


async def test_llm_outage_degrades_to_template(tmp_path):
    st = make_state(tmp_path)
    try:
        install_llm(st, AlwaysFailLLM())
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)行情")
        assert run["mode"] == "llm"
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "completed"
        assert r["mode"] == "template"
        assert r["degrade_reasons"]
        types = [e["type"] for e in st.bus.replay(thr["id"], 0)]
        assert "run.degraded" in types and "llm.error" in types
        assert st.harness.evidence.list(run["id"])
    finally:
        await st.close()


async def test_llm_primary_fails_fallback_succeeds(tmp_path):
    st = make_state(tmp_path)
    try:
        install_llm(st, AlwaysFailLLM(), MockLLM(handler=scripted_handler(), name="fallback"))
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "completed" and r["mode"] == "llm"
        types = [e["type"] for e in st.bus.replay(thr["id"], 0)]
        assert "llm.fallback" in types
        ends = [e for e in st.bus.replay(thr["id"], 0) if e["type"] == "llm.end"]
        assert ends and all(e["payload"]["provider"] == "fallback" for e in ends)
    finally:
        await st.close()


async def test_llm_outage_midway_switches_to_pipeline(tmp_path):
    """前两次模型调用成功（规划 + 第一步），随后模型故障 → 执行阶段降级到流水线并完成。"""
    from app.llm.base import LLMError

    h = scripted_handler()
    counter = {"n": 0}

    def flaky(messages, tools):
        counter["n"] += 1
        if counter["n"] > 2:
            raise LLMError("boom", retryable=False, status=500)
        return h(messages, tools)

    st = make_state(tmp_path)
    try:
        install_llm(st, MockLLM(handler=flaky))
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)行情与估值")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "completed", r
        assert r["mode"] == "template"
        assert any("不可用" in d for d in r["degrade_reasons"])
        assert r["artifacts"]
    finally:
        await st.close()
