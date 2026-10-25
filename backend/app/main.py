"""FastAPI 入口：REST + SSE + 静态前端托管。"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT_DIR, settings
from .db import loads, now_ms
from .factory import AppState, build_app_state, connect_ifind

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("xbuddy.main")

FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"


class ThreadIn(BaseModel):
    title: str = "新研究线程"


class RunIn(BaseModel):
    goal: str
    budget: Optional[dict] = None


class DecisionIn(BaseModel):
    approved: bool
    reason: str = ""


class MemoryIn(BaseModel):
    kind: str = "note"
    content: str
    tags: str = ""


class FaultIn(BaseModel):
    target: str  # fuyao | ifind | llm | local | memory
    mode: str  # error | timeout | ratelimit | off
    count: Optional[int] = None


def create_app(state: Optional[AppState] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = state or build_app_state(settings)
        app.state.x = st
        n = st.harness.recover_on_startup()
        if n:
            log.info("marked %d interrupted runs", n)
        try:
            info = await asyncio.wait_for(connect_ifind(st), timeout=20)
            log.info("iFinD MCP: %s", info)
        except Exception as e:  # noqa: BLE001
            st.ifind = {"ok": False, "count": 0, "error": f"连接超时/失败: {e}", "configured": st.settings.ifind_enabled}
        log.info("capabilities: llm=%s fuyao=%s tools=%d", st.llm.describe(), st.settings.fuyao_effective_mode, len(st.registry.names()))
        yield
        await st.close()

    app = FastAPI(title="投资 X Buddy", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def X(request: Request) -> AppState:
        return request.app.state.x

    # ------------------------------------------------------------- 基础
    @app.get("/api/health")
    async def health(request: Request):
        st = X(request)
        return {"ok": True, "time": now_ms(), "llm": st.llm.available, "fuyao_mode": st.settings.fuyao_effective_mode, "ifind": st.ifind.get("ok"), "tools": len(st.registry.names())}

    @app.get("/api/config")
    async def config(request: Request):
        return X(request).capabilities()

    @app.get("/api/tools")
    async def tools(request: Request):
        return {"tools": [t.public() for t in X(request).registry.specs()]}

    # ------------------------------------------------------------- 线程
    @app.get("/api/threads")
    async def list_threads(request: Request):
        st = X(request)
        rows = st.db.fetchall("SELECT t.*, (SELECT status FROM runs r WHERE r.thread_id = t.id ORDER BY r.created_at DESC LIMIT 1) AS last_status, (SELECT COUNT(*) FROM runs r WHERE r.thread_id = t.id) AS run_count FROM threads t ORDER BY updated_at DESC LIMIT 200")
        return {"threads": rows}

    @app.post("/api/threads")
    async def create_thread(body: ThreadIn, request: Request):
        return X(request).harness.create_thread(body.title)

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str, request: Request):
        st = X(request)
        t = st.db.get("threads", thread_id)
        if not t:
            raise HTTPException(404, "线程不存在")
        msgs = st.db.fetchall("SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at, id", [thread_id])
        runs = st.db.fetchall("SELECT id, goal, status, phase, mode, created_at, finished_at, stop_reason FROM runs WHERE thread_id = ? ORDER BY created_at", [thread_id])
        last_seq = st.db.fetchone("SELECT COALESCE(MAX(seq),0) AS s FROM events WHERE thread_id = ?", [thread_id])
        return {"thread": t, "messages": [{**m, "blocks": loads(m.pop("blocks_json"), [])} for m in msgs], "runs": runs, "last_seq": (last_seq or {}).get("s", 0)}

    @app.delete("/api/threads/{thread_id}")
    async def delete_thread(thread_id: str, request: Request):
        st = X(request)
        for table in ("messages", "events", "approvals", "artifacts"):
            st.db.execute(f"DELETE FROM {table} WHERE thread_id = ?", [thread_id])
        for r in st.db.fetchall("SELECT id FROM runs WHERE thread_id = ?", [thread_id]):
            for table in ("tasks", "evidence", "checkpoints"):
                st.db.execute(f"DELETE FROM {table} WHERE run_id = ?", [r["id"]])
        st.db.execute("DELETE FROM runs WHERE thread_id = ?", [thread_id])
        st.db.delete("threads", thread_id)
        return {"ok": True}

    # ------------------------------------------------------------- 运行
    @app.post("/api/threads/{thread_id}/runs")
    async def start_run(thread_id: str, body: RunIn, request: Request):
        if not body.goal.strip():
            raise HTTPException(400, "研究目标不能为空")
        active = X(request).db.fetchone("SELECT id FROM runs WHERE thread_id = ? AND status IN ('planning','running','waiting_approval')", [thread_id])
        if active:
            raise HTTPException(409, f"线程中已有进行中的运行 {active['id']}，请先停止或等待完成")
        try:
            return await X(request).harness.start_run(thread_id, body.goal, body.budget)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        r = X(request).harness.get_run(run_id)
        if not r:
            raise HTTPException(404, "运行不存在")
        return r

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str, request: Request):
        try:
            return await X(request).harness.stop(run_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str, request: Request):
        try:
            return await X(request).harness.resume(run_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/runs/{run_id}/evidence")
    async def run_evidence(run_id: str, request: Request):
        return {"evidence": X(request).harness.evidence.list(run_id)}

    @app.get("/api/runs/{run_id}/trace")
    async def run_trace(run_id: str, request: Request):
        t = X(request).harness.trace(run_id)
        if not t:
            raise HTTPException(404, "运行不存在")
        return t

    @app.get("/api/runs/{run_id}/checkpoints")
    async def run_checkpoints(run_id: str, request: Request):
        rows = X(request).db.fetchall("SELECT id, step, created_at, length(state_json) AS size FROM checkpoints WHERE run_id = ? ORDER BY id", [run_id])
        return {"checkpoints": rows}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, since: int = 0):
        st = X(request)
        run = st.db.get("runs", run_id)
        if not run:
            raise HTTPException(404, "运行不存在")
        return {"events": st.bus.replay(run["thread_id"], since, run_id=run_id)}

    # ------------------------------------------------------------- 证据 / 产物
    @app.get("/api/evidence/{evidence_id}")
    async def get_evidence(evidence_id: str, request: Request, run_id: Optional[str] = None):
        ev = X(request).harness.evidence.get(evidence_id, run_id=run_id)
        if not ev:
            raise HTTPException(404, "证据不存在")
        return ev

    @app.get("/api/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str, request: Request):
        a = X(request).db.get("artifacts", artifact_id)
        if not a:
            raise HTTPException(404, "产物不存在")
        a["lint"] = loads(a.pop("lint_json"), None)
        a["blocks"] = loads(a.pop("blocks_json"), [])
        return a

    @app.get("/api/artifacts/{artifact_id}/download.md")
    async def download_artifact(artifact_id: str, request: Request):
        a = X(request).db.get("artifacts", artifact_id)
        if not a:
            raise HTTPException(404, "产物不存在")
        return PlainTextResponse(a["content_md"] or "", media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{artifact_id}.md"'})

    @app.get("/api/artifacts/{artifact_id}/bundle.json")
    async def artifact_bundle(artifact_id: str, request: Request):
        st = X(request)
        a = st.db.get("artifacts", artifact_id)
        if not a:
            raise HTTPException(404, "产物不存在")
        return {"artifact": {**a, "lint": loads(a.pop("lint_json"), None)}, "evidence": st.harness.evidence.list(a["run_id"], with_raw=True), "run": st.harness.get_run(a["run_id"])}

    # ------------------------------------------------------------- 审批
    @app.get("/api/approvals")
    async def list_approvals(request: Request, run_id: Optional[str] = None, status: Optional[str] = None):
        st = X(request)
        sql, params = "SELECT * FROM approvals", []
        conds = []
        if run_id:
            conds.append("run_id = ?")
            params.append(run_id)
        if status:
            conds.append("status = ?")
            params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = st.db.fetchall(sql + " ORDER BY created_at DESC LIMIT 200", params)
        return {"approvals": [st.harness._public_approval(r) for r in rows]}

    @app.post("/api/approvals/{approval_id}/decide")
    async def decide(approval_id: str, body: DecisionIn, request: Request):
        try:
            return await X(request).harness.decide_approval(approval_id, body.approved, body.reason)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------- 记忆
    @app.get("/api/memories")
    async def list_memories(request: Request, q: Optional[str] = None):
        mem = X(request).harness.memory
        return {"memories": mem.recall(q, limit=50) if q else mem.list()}

    @app.post("/api/memories")
    async def add_memory(body: MemoryIn, request: Request):
        if not body.content.strip():
            raise HTTPException(400, "内容不能为空")
        return X(request).harness.memory.add(scope="user", kind=body.kind, content=body.content, tags=body.tags)

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: str, request: Request):
        X(request).harness.memory.delete(memory_id)
        return {"ok": True}

    # ------------------------------------------------------------- 观测 / 演示
    @app.get("/api/metrics")
    async def metrics(request: Request):
        st = X(request)
        runs = st.db.fetchall("SELECT status, usage_json, created_at, finished_at FROM runs ORDER BY created_at DESC LIMIT 500")
        agg = {"runs": len(runs), "by_status": {}, "llm_calls": 0, "tool_calls": 0, "tool_errors": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "compressions": 0, "avg_duration_ms": 0}
        durs = []
        for r in runs:
            agg["by_status"][r["status"]] = agg["by_status"].get(r["status"], 0) + 1
            u = loads(r.get("usage_json"), {}) or {}
            for k in ("llm_calls", "tool_calls", "tool_errors", "cost_usd", "input_tokens", "output_tokens", "compressions"):
                agg[k] += u.get(k, 0) or 0
            if r.get("finished_at"):
                durs.append(r["finished_at"] - r["created_at"])
        agg["avg_duration_ms"] = int(sum(durs) / len(durs)) if durs else 0
        agg["cost_usd"] = round(agg["cost_usd"], 4)
        agg["breaker"] = st.harness.breaker.state()
        agg["faults"] = st.faults.state()
        agg["evidence_total"] = (st.db.fetchone("SELECT COUNT(*) AS c FROM evidence") or {}).get("c", 0)
        return agg

    @app.get("/api/dev/faults")
    async def get_faults(request: Request):
        st = X(request)
        return {"enabled": st.settings.enable_fault_injection, "faults": st.faults.state(), "breaker": st.harness.breaker.state()}

    @app.post("/api/dev/faults")
    async def set_fault(body: FaultIn, request: Request):
        st = X(request)
        if not st.settings.enable_fault_injection:
            raise HTTPException(403, "故障注入已禁用")
        if body.mode == "off" and body.target == "all":
            st.faults.clear()
            st.harness.breaker.reset()
            return {"faults": st.faults.state()}
        return {"faults": st.faults.set(body.target, body.mode, body.count)}

    # ------------------------------------------------------------- SSE
    @app.get("/api/threads/{thread_id}/stream")
    async def stream(thread_id: str, request: Request, since: int = Query(0)):
        st = X(request)
        if not st.db.get("threads", thread_id):
            raise HTTPException(404, "线程不存在")

        async def gen():
            q = st.bus.subscribe(thread_id)
            try:
                last = since
                for evt in st.bus.replay(thread_id, since):
                    last = evt["seq"]
                    yield _sse(evt)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if evt["seq"] <= last:
                        continue
                    last = evt["seq"]
                    yield _sse(evt)
            finally:
                st.bus.unsubscribe(thread_id, q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------- 静态前端
    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            target = FRONTEND_DIST / path
            if path and target.is_file():
                return FileResponse(target)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


def _sse(evt: dict) -> str:
    return f"id: {evt['seq']}\nevent: {evt['type']}\ndata: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"


app = create_app()
