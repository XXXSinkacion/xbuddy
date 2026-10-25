"""HTTP 层：线程 / 运行 / 证据 / 产物 / 审批 / 记忆 / 故障注入 / 事件回放。"""
import time

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_state


def _wait(c, run_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/runs/{run_id}").json()
        if r["status"] in ("completed", "failed", "stopped", "waiting_approval", "interrupted"):
            return r
        time.sleep(0.1)
    raise TimeoutError


def test_api_end_to_end(tmp_path):
    st = make_state(tmp_path)
    app = create_app(st)
    with TestClient(app) as c:
        assert c.get("/api/health").json()["ok"]
        cfg = c.get("/api/config").json()
        assert cfg["llm"]["mode"] == "template" and cfg["fuyao"]["mode"] == "fixture" and len(cfg["tools"]) >= 20
        assert c.get("/api/tools").json()["tools"][0]["risk_label"]

        thr = c.post("/api/threads", json={"title": "测试"}).json()
        run = c.post(f"/api/threads/{thr['id']}/runs", json={"goal": "研究贵州茅台(600519.SH)行情与估值", "budget": {"max_tool_calls": 50}}).json()
        assert run["status"] in ("planning", "running")
        # 并发保护：进行中的线程不允许再发起（fixture 模式极快，可能已完成则会被接受）
        second = c.post(f"/api/threads/{thr['id']}/runs", json={"goal": "研究五粮液(000858.SZ)估值"})
        assert second.status_code in (409, 200)
        expected_runs = 1 if second.status_code == 409 else 2
        r = _wait(c, run["id"])
        assert r["status"] == "completed"
        if second.status_code == 200:
            _wait(c, second.json()["id"])
        assert r["remaining"]["tool_calls"]["max"] == 50

        ev = c.get(f"/api/runs/{run['id']}/evidence").json()["evidence"]
        assert ev and "raw" not in ev[0]
        one = c.get(f"/api/evidence/{ev[0]['id']}").json()
        assert one["raw"] is not None and one["label"] == "E1"
        by_label = c.get(f"/api/evidence/E1?run_id={run['id']}").json()
        assert by_label["id"] == ev[0]["id"]

        art_id = r["artifacts"][0]["id"]
        art = c.get(f"/api/artifacts/{art_id}").json()
        assert "## 免责声明" in art["content_md"] and art["lint"]["ok"] is True
        dl = c.get(f"/api/artifacts/{art_id}/download.md")
        assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
        bundle = c.get(f"/api/artifacts/{art_id}/bundle.json").json()
        assert bundle["evidence"] and bundle["run"]["id"] == run["id"]

        trace = c.get(f"/api/runs/{run['id']}/trace").json()
        assert trace["spans"] and any(s["kind"] == "tool" for s in trace["spans"])
        evs = c.get(f"/api/runs/{run['id']}/events?since=0").json()["events"]
        assert evs and evs[0]["seq"] == 1
        th = c.get(f"/api/threads/{thr['id']}").json()
        assert len(th["messages"]) >= 3 and th["last_seq"] >= len(evs)
        assert c.get("/api/threads").json()["threads"][0]["run_count"] == expected_runs
        assert c.get(f"/api/runs/{run['id']}/checkpoints").json()["checkpoints"]

        aprs = c.get(f"/api/approvals?run_id={run['id']}&status=pending").json()["approvals"]
        assert aprs and aprs[0]["kind"] == "memory_write"
        assert c.post(f"/api/approvals/{aprs[0]['id']}/decide", json={"approved": True}).status_code == 200
        assert c.post(f"/api/approvals/{aprs[0]['id']}/decide", json={"approved": True}).status_code == 400

        mems = c.get("/api/memories").json()["memories"]
        n = len(mems)
        m = c.post("/api/memories", json={"kind": "note", "content": "测试记忆 茅台", "tags": "t"}).json()
        assert c.get("/api/memories?q=茅台").json()["memories"]
        assert c.delete(f"/api/memories/{m['id']}").json()["ok"]
        assert len(c.get("/api/memories").json()["memories"]) == n

        # 故障注入 + 指标
        assert c.post("/api/dev/faults", json={"target": "fuyao", "mode": "error"}).json()["faults"]["fuyao"]["mode"] == "error"
        assert c.post("/api/dev/faults", json={"target": "all", "mode": "off"}).json()["faults"] == {}
        met = c.get("/api/metrics").json()
        assert met["runs"] == expected_runs and met["tool_calls"] > 0

        assert c.post(f"/api/runs/{run['id']}/resume").status_code == 400  # 已完成不可恢复
        assert c.delete(f"/api/threads/{thr['id']}").json()["ok"]
        assert c.get(f"/api/threads/{thr['id']}").status_code == 404
