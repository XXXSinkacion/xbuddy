"""检查点与恢复：进程重启后从检查点继续，已完成任务不重跑。"""
import asyncio

from app.db import dumps, loads
from app.factory import build_app_state
from tests.conftest import make_settings, make_state, wait_run


async def test_interrupted_run_resumes_from_checkpoint(tmp_path):
    st = make_state(tmp_path, harness_max_tool_calls=4)
    thr = st.harness.create_thread("t")
    run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)行情、估值与财务")
    r = await wait_run(st.harness, run["id"])
    # 用极小预算先跑到“停止”，模拟进程中途挂掉：手工把状态改回 running 并恢复更大预算
    assert r["status"] == "stopped"
    done_before = [t["id"] for t in r["tasks"] if t["status"] in ("done", "partial")]
    ev_before = len(st.harness.evidence.list(run["id"]))
    assert done_before and ev_before >= 3
    # 伪造“进程中途崩溃”：状态 running、phase execute、把 skipped 任务改回 pending
    state = loads(st.db.get("runs", run["id"])["state_json"], {})
    state["phase"] = "execute"
    budget = loads(st.db.get("runs", run["id"])["budget_json"], {})
    budget["max_tool_calls"] = 100
    st.db.update("runs", run["id"], {"status": "running", "phase": "execute", "state_json": dumps(state), "budget_json": dumps(budget), "finished_at": None, "stop_reason": None})
    for t in st.db.fetchall("SELECT id, status FROM tasks WHERE run_id = ?", [run["id"]]):
        if t["status"] in ("skipped", "failed"):
            st.db.update("tasks", t["id"], {"status": "pending", "error": None})
    await st.close()

    # 新进程：同一个数据库
    st2 = build_app_state(make_settings(tmp_path), db_path=str(tmp_path / "t.sqlite"))
    try:
        n = st2.harness.recover_on_startup()
        assert n == 1
        r2 = st2.harness.get_run(run["id"])
        assert r2["status"] == "interrupted"
        await st2.harness.resume(run["id"])
        r3 = await wait_run(st2.harness, run["id"], until=("completed", "failed", "stopped"))
        assert r3["status"] == "completed", r3
        # 已完成任务保持原状（未重跑：证据只增加未完成任务的）
        for t in r3["tasks"]:
            if t["id"] in done_before:
                assert t["status"] in ("done", "partial")
        ev_after = st2.harness.evidence.list(run["id"])
        assert len(ev_after) > ev_before
        # 证据编号连续
        labels = [e["label"] for e in ev_after]
        assert labels == [f"E{i}" for i in range(1, len(labels) + 1)]
        types = {e["type"] for e in st2.bus.replay(thr["id"], 0)}
        assert {"run.interrupted", "run.resumed", "run.completed"} <= types
        cps = st2.db.fetchall("SELECT step FROM checkpoints WHERE run_id = ? ORDER BY id", [run["id"]])
        assert len(cps) >= 5
    finally:
        await st2.close()


async def test_waiting_approval_survives_restart(tmp_path):
    st = make_state(tmp_path, harness_plan_approval="always")
    thr = st.harness.create_thread("t")
    run = await st.harness.start_run(thr["id"], "研究贵州茅台(600519.SH)估值")
    r = await wait_run(st.harness, run["id"])
    assert r["status"] == "waiting_approval"
    await st.close()
    st2 = build_app_state(make_settings(tmp_path, harness_plan_approval="always"), db_path=str(tmp_path / "t.sqlite"))
    try:
        st2.harness.recover_on_startup()
        r2 = st2.harness.get_run(run["id"])
        assert r2["status"] == "waiting_approval"
        apr = [a for a in r2["approvals"] if a["kind"] == "plan"][0]
        await st2.harness.decide_approval(apr["id"], True)
        r3 = await wait_run(st2.harness, run["id"], until=("completed", "failed", "stopped"))
        assert r3["status"] == "completed"
    finally:
        await st2.close()
