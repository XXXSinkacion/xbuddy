"""第二轮代码审查发现的问题的回归测试（每个用例对应 docs/AI_USAGE.md 第 3 节的一条修正）。"""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from app.compliance.linter import lint
from app.compliance.numcheck import extract_claim_numbers
from app.db import dumps, loads, now_ms
from app.harness.context import ContextManager
from app.harness.policy import Policy, active_seconds
from app.harness.runtime import CapacityError
from app.llm.base import LLMResponse, ToolCall
from app.llm.mock import MockLLM
from app.main import create_app
from app.models import Budget, RunState
from app.tools.cache import FaultInjector
from app.tools.fuyao import kline_gap
from tests.conftest import PLAN_JSON, install_llm, make_state, scripted_handler, wait_run

DONE = ("completed", "failed", "stopped")


# ------------------------------------------------------------------ 安全
def test_spa_route_blocks_path_traversal(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP-SECRET-KEY", encoding="utf-8")
    st = make_state(tmp_path)
    app = create_app(st, frontend_dist=dist)
    with TestClient(app) as c:
        for raw in ("/%2e%2e/secret.txt", "/..%2fsecret.txt", "/%2e%2e%2fsecret.txt"):
            r = c.get(raw)
            assert "TOP-SECRET-KEY" not in r.text, raw
        assert "spa" in c.get("/some/client/route").text  # SPA 回退仍然可用
        assert c.get("/api/not-exist").status_code == 404  # 未知 API 不回落到 index.html


# ------------------------------------------------------------------ 证据：非数据工具不生成证据
async def test_note_fact_and_get_evidence_do_not_create_evidence(tmp_path):
    seq = {
        "t1": [("search_ticker", {"q": "贵州茅台"}), ("note_fact", {"key": "target", "value": "600519.SH"}), ("get_evidence", {"evidence_label": "E1"})],
        "t2": [("get_quote_snapshot", {"thscodes": "600519.SH"})],
    }
    st = make_state(tmp_path)
    try:
        install_llm(st, MockLLM(handler=scripted_handler(tool_sequence=seq)))
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r = await wait_run(st.harness, run["id"], until=DONE)
        assert r["status"] == "completed"
        tools = [e["tool"] for e in st.harness.evidence.list(run["id"])]
        assert "note_fact" not in tools and "get_evidence" not in tools
        assert tools.count("search_ticker") == 1
    finally:
        await st.close()


# ------------------------------------------------------------------ 成本：全链路计量
async def test_usage_counts_plan_and_synthesis_llm_calls(tmp_path):
    st = make_state(tmp_path)
    try:
        install_llm(st, MockLLM(handler=scripted_handler()), pricing={"default": (1.0, 2.0)})
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r = await wait_run(st.harness, run["id"], until=DONE)
        ends = [e for e in st.bus.replay(thr["id"], 0, run_id=run["id"]) if e["type"] == "llm.end"]
        purposes = {e["payload"]["purpose"] for e in ends}
        assert {"plan", "synthesize"} <= purposes
        assert r["usage"]["llm_calls"] == len(ends)
        assert abs(r["usage"]["cost_usd"] - sum(e["payload"]["cost_usd"] for e in ends)) < 1e-6
        assert r["usage"]["input_tokens"] == sum(e["payload"]["input_tokens"] for e in ends)
    finally:
        await st.close()


# ------------------------------------------------------------------ 恢复 / 停止语义
async def test_stopped_run_cannot_be_resumed_and_late_approval_expires(tmp_path):
    st = make_state(tmp_path, harness_plan_approval="always")
    try:
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)估值")
        r = await wait_run(st.harness, run["id"])
        assert r["status"] == "waiting_approval"
        apr = [a for a in r["approvals"] if a["kind"] == "plan"][0]
        await st.harness.stop(run["id"])
        r = st.harness.get_run(run["id"])
        assert r["status"] == "stopped" and r["phase"] == "done"
        assert [a for a in r["approvals"] if a["id"] == apr["id"]][0]["status"] == "expired"
        try:
            await st.harness.decide_approval(apr["id"], True)
            raise AssertionError("迟到的审批不应被接受")
        except ValueError:
            pass
        try:
            await st.harness.resume(run["id"])
            raise AssertionError("已停止的运行不应可恢复")
        except ValueError:
            pass
        assert st.harness.get_run(run["id"])["status"] == "stopped"
    finally:
        await st.close()


async def test_failed_run_can_resume_from_checkpoint(tmp_path):
    st = make_state(tmp_path)
    try:
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)估值")
        await wait_run(st.harness, run["id"], until=DONE)
        # 伪造“执行阶段崩溃”：status=failed、phase=execute、最后一个任务回到 pending
        row = st.db.get("runs", run["id"])
        state = loads(row["state_json"], {})
        state["phase"] = "execute"
        st.db.update("runs", run["id"], {"status": "failed", "phase": "execute", "state_json": dumps(state), "error": "boom"})
        last = st.db.fetchall("SELECT id FROM tasks WHERE run_id = ? ORDER BY idx DESC LIMIT 1", [run["id"]])[0]
        st.db.update("tasks", last["id"], {"status": "pending"})
        await st.harness.resume(run["id"])
        r = await wait_run(st.harness, run["id"], until=DONE)
        assert r["status"] == "completed" and not r["error"]
    finally:
        await st.close()


def test_duration_budget_excludes_approval_wait():
    now = time.time() * 1000
    st = RunState(started_at=int(now - 1000_000), paused_ms=990_000)  # 墙钟 1000s，其中 990s 在等审批
    p = Policy(Budget(max_duration_s=120))
    assert abs(active_seconds(st, now) - 10) < 1
    assert p.check_stop(st) is None
    st2 = RunState(started_at=int(now - 1000_000))
    assert "时长上限" in (p.check_stop(st2) or "")
    st3 = RunState(started_at=int(now - 1000_000), paused_at=int(now - 995_000))  # 正在等待审批中
    assert p.check_stop(st3) is None


# ------------------------------------------------------------------ 执行器
async def test_task_summary_is_linted_like_the_report(tmp_path):
    def handler(messages, tools):
        system = str(messages[0].get("content", ""))
        if "规划器" in system:
            return LLMResponse(content=json.dumps({"title": "t", "tasks": [PLAN_JSON["tasks"][0]]}, ensure_ascii=False))
        if "撰写器" in system:
            return LLMResponse(content="## 摘要\n测试\n## 核心结论\n- 【不确定】无\n")
        done = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
        if done == 0:
            return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="search_ticker", arguments={"q": "贵州茅台"})])
        return LLMResponse(content="【推断】茅台有望大幅上涨，建议买入 [E1]\n【事实】代码 600519.SH [E99]")

    st = make_state(tmp_path)
    try:
        install_llm(st, MockLLM(handler=handler))
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r = await wait_run(st.harness, run["id"], until=DONE)
        summary = r["tasks"][0]["result"]["summary"]
        assert "建议买入" not in summary and "有望大幅上涨" not in summary
        assert "[E99]" not in summary and "[引用无效]" in summary
        assert "compliance.task_lint" in {e["type"] for e in st.bus.replay(thr["id"], 0)}
        joined = " ".join(m["blocks_json"] for m in st.db.fetchall("SELECT blocks_json FROM messages WHERE run_id = ?", [run["id"]]))
        assert "建议买入" not in joined
    finally:
        await st.close()


async def test_llm_task_without_successful_evidence_is_not_done(tmp_path):
    seq = {"t1": [("search_ticker", {"q": "贵州茅台"})], "t2": [("get_quote_snapshot", {"thscodes": "600519.SH"})]}
    st = make_state(tmp_path)
    try:
        install_llm(st, MockLLM(handler=scripted_handler(tool_sequence=seq)))
        st.faults.set("fuyao", "error")  # 数据源全部失败
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        r = await wait_run(st.harness, run["id"], until=DONE)
        assert all(t["status"] != "done" for t in r["tasks"]), [t["status"] for t in r["tasks"]]
        assert "【系统】" in r["tasks"][0]["result"]["summary"]
    finally:
        await st.close()


async def test_parallel_tool_calls_in_one_turn(tmp_path):
    def handler(messages, tools):
        system = str(messages[0].get("content", ""))
        if "规划器" in system:
            return LLMResponse(content=json.dumps({"title": "t", "tasks": [{"id": "t1", "title": "取数", "objective": "并行取数", "tools": ["get_quote_snapshot", "get_valuation_snapshot"]}]}, ensure_ascii=False))
        if "撰写器" in system:
            return LLMResponse(content="## 核心结论\n- 【不确定】无\n")
        if not any(m.get("role") == "tool" for m in messages):
            return LLMResponse(content="", tool_calls=[ToolCall(id="a", name="get_quote_snapshot", arguments={"thscodes": "600519.SH"}), ToolCall(id="b", name="get_valuation_snapshot", arguments={"thscodes": "600519.SH"})])
        return LLMResponse(content="【事实】已取得行情与估值 [E1][E2]")

    st = make_state(tmp_path)
    try:
        mock = MockLLM(handler=handler)
        install_llm(st, mock)
        thr = st.harness.create_thread("t")
        run = await st.harness.start_run(thr["id"], "茅台行情估值")
        r = await wait_run(st.harness, run["id"], until=DONE)
        assert r["status"] == "completed" and r["tasks"][0]["status"] == "done"
        assert {e["tool"] for e in st.harness.evidence.list(run["id"])} == {"get_quote_snapshot", "get_valuation_snapshot"}
        assert "tool.parallel" in {e["type"] for e in st.bus.replay(thr["id"], 0)}
        # 两个 tool_call 都有对应的 tool 消息，顺序与调用一致
        last_task_call = [c for c in mock.calls if "当前任务" in str(c["messages"][1].get("content", ""))][-1]
        tool_ids = [m["tool_call_id"] for m in last_task_call["messages"] if m.get("role") == "tool"]
        assert tool_ids == ["a", "b"]
    finally:
        await st.close()


def test_repeated_compression_keeps_earlier_evidence_clues():
    cm = ContextManager(budget_tokens=300)
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "goal"}]

    def grp(i):
        return [
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "get_kline", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": f"证据编号: E{i}\n状态: ok\n摘要: 第{i}条 " + "x" * 400},
        ]

    msgs, s1 = cm.compress_if_needed(head + grp(1) + grp(2) + grp(3))
    msgs, s2 = cm.compress_if_needed(msgs + grp(4) + grp(5))
    fold = [m for m in msgs if str(m.get("content", "")).startswith("【已压缩")]
    assert s1["mode"] == s2["mode"] == "fold" and len(fold) == 1
    assert "[E1]" in fold[0]["content"] and "[E3]" in fold[0]["content"]


# ------------------------------------------------------------------ 合规：失败证据 / 数值核验
EV = [
    {"label": "E1", "tool": "get_quote_snapshot", "status": "ok", "source": "fuyao", "summary": "600519.SH 最新 1335 元", "unit_notes": "元", "raw": {"item": [{"thscode": "600519.SH", "last_price": 1335.0, "price_change_ratio_pct": 1.25219, "turnover": 7677216300}]}},
    {"label": "E2", "tool": "get_financial_indicators", "status": "error", "source": "fuyao", "summary": "调用失败：code=5003", "raw": None},
    {"label": "E3", "tool": "get_kline", "status": "ok", "source": "fuyao", "summary": "K 线", "raw": {"item": [{"close_price": 1289.5, "high_price": 1363.35}, {"close_price": 1335.0, "high_price": 1340.0}]}},
    {"label": "E4", "tool": "compute_price_stats", "status": "ok", "source": "local", "summary": "统计", "raw": {"period_return_pct": 3.53, "max_drawdown_pct": -9.27, "based_on": "E3"}},
    {"label": "E5", "tool": "get_valuation_snapshot", "status": "stale", "source": "fuyao (过期缓存)", "summary": "PE 20.49", "raw": {"item": [{"pe_ttm": 20.493396}]}},
]


def _conclusions(md):
    i = md.find("## 核心结论")
    j = md.find("\n## ", i + 5)
    return md[i:j]


def test_numeric_verification_flags_numbers_not_in_cited_evidence():
    md = "## 核心结论\n- 【事实】最新价 1335.00 元，涨跌幅 +1.25219%，成交额约 76.77 亿元 [E1]\n- 【事实】最新价 1353.00 元 [E1]\n- 【事实】区间收益 +3.53%，最大回撤 9.27%，区间最高 1363.35 元 [E4]\n"
    res = lint(md, EV)
    sec = _conclusions(res.md)
    lines = [l for l in sec.splitlines() if l.startswith("- ")]
    assert "待核" not in lines[0]  # 单位换算（亿）、四舍五入、带符号百分比都能核到
    assert "待核数值：1353.00元" in lines[1]
    assert "待核" not in lines[2]  # 1363.35 来自 E4 based_on 的 E3
    assert res.stats["numbers_checked"] == 7 and res.stats["numbers_unverified"] == 1


def test_fact_citing_only_failed_evidence_is_downgraded():
    md = "## 核心结论\n- 【事实】ROE 为 16.75% [E2]\n- 【事实】2026-3 财务指标调用失败（code=5003）[E2]\n- 【事实】PE(TTM) 20.49 倍 [E5]\n"
    res = lint(md, EV)
    lines = [l for l in _conclusions(res.md).splitlines() if l.startswith("- ")]
    assert lines[0].startswith("- 【不确定·所引证据为失败/缺失】")
    assert lines[1].startswith("- 【事实】")  # 陈述失败本身是事实，不降级
    assert "过期缓存" in lines[2]
    assert res.stats["fact_on_failed"] == 1 and res.stats["stale_cited"] == 1 and not res.ok


def test_claim_number_extraction_skips_dates_codes_and_counts():
    nums = [c["raw"] for c in extract_claim_numbers("2026-07-27 至 2026-10-23 共 59 根，600519.SH 报告期 2026-2，MA20 为 1280.97 元，近 90 天上涨 29 日 [E3]")]
    assert nums == ["1280.97元"]


# ------------------------------------------------------------------ 过期数据 / 公开部署保护
def test_kline_gap_detects_suspension():
    day = 86_400_000
    end = 1_800_000_000_000
    assert kline_gap([{"date_ms": end - 2 * day}], end, now_ms=end) is None
    msg = kline_gap([{"date_ms": end - 30 * day}], end, now_ms=end)
    assert msg and "停牌" in msg


def test_fault_injection_expires():
    f = FaultInjector(enabled=True, ttl_s=0.05)
    f.set("fuyao", "error")
    assert f.active("fuyao") == "error"
    time.sleep(0.08)
    assert f.active("fuyao") is None and f.state() == {}


async def test_global_concurrency_limit_and_daily_cost_cap(tmp_path):
    st = make_state(tmp_path, max_concurrent_runs=1, daily_llm_cost_cap_usd=1.0)
    try:
        install_llm(st, MockLLM(handler=scripted_handler()))
        thr = st.harness.create_thread("t")
        fake = {"id": "run_fake", "thread_id": thr["id"], "goal": "x", "status": "running", "phase": "execute", "mode": "llm", "budget_json": "{}", "usage_json": dumps({"cost_usd": 5.0}), "state_json": "{}", "created_at": now_ms(), "updated_at": now_ms()}
        st.db.insert("runs", fake)
        try:
            await st.harness.start_run(thr["id"], "研究贵州茅台")
            raise AssertionError("应触发并发上限")
        except CapacityError:
            pass
        st.db.update("runs", "run_fake", {"status": "completed"})
        run = await st.harness.start_run(thr["id"], "研究贵州茅台")
        assert run["mode"] == "template"  # 今日成本 5.0 ≥ 上限 1.0 → 不再调用模型
        r = await wait_run(st.harness, run["id"], until=DONE)
        assert r["status"] == "completed" and r["usage"]["llm_calls"] == 0
        assert any("成本已达上限" in d for d in r["degrade_reasons"])
    finally:
        await st.close()


async def test_memory_recall_ranks_by_relevance_and_skips_demo(tmp_path):
    st = make_state(tmp_path)
    try:
        mem = st.harness.memory
        mem.add(scope="user", kind="conclusion", content="2026-10-25 研究「白酒」事实要点：演示数值（来源：演示数据运行，非真实行情）", tags="conclusion,demo")
        got = mem.recall("宁德时代 300750.SZ 盈利能力", limit=3)
        assert got[0]["content"].startswith("300750.SZ")
        assert any(m["kind"] == "preference" for m in got)
        assert any("演示" in m["content"] for m in mem.recall("白酒 研究", limit=5))
        assert not any("demo" in m["tags"] for m in mem.recall("白酒 研究", limit=5, exclude_demo=True))
    finally:
        await st.close()
