"""证据库：每次工具调用（无论成败）都生成一条可回溯的证据记录。"""
from __future__ import annotations

import hashlib
from typing import Optional

from ..db import Database, dumps, loads, new_id, now_ms
from ..events import EventBus
from ..models import ToolResult

MAX_RAW_CHARS = 2_000_000


class EvidenceStore:
    def __init__(self, db: Database, bus: Optional[EventBus] = None):
        self.db = db
        self.bus = bus
        self._thread_cache: dict[str, str] = {}

    def _thread_of(self, run_id: str) -> Optional[str]:
        if run_id in self._thread_cache:
            return self._thread_cache[run_id]
        row = self.db.fetchone("SELECT thread_id FROM runs WHERE id = ?", [run_id])
        if row:
            self._thread_cache[run_id] = row["thread_id"]
            return row["thread_id"]
        return None

    def next_label(self, run_id: str) -> str:
        row = self.db.fetchone("SELECT COUNT(*) AS c FROM evidence WHERE run_id = ?", [run_id])
        return f"E{int(row['c']) + 1 if row else 1}"

    def record(self, run_id: str, task_id: Optional[str], tool: str, args: dict, result: ToolResult, *, latency_ms: int = 0) -> dict:
        raw = dumps(result.data) if result.data is not None else None
        if raw and len(raw) > MAX_RAW_CHARS:
            raw = raw[:MAX_RAW_CHARS]
        label = self.next_label(run_id)
        row = {
            "id": new_id("evd"),
            "run_id": run_id,
            "task_id": task_id,
            "label": label,
            "tool": tool,
            "args_json": dumps(args),
            "source": result.source,
            "status": result.status if not result.ok or result.status != "ok" else "ok",
            "fetched_at": now_ms(),
            "data_timestamp": result.data_timestamp,
            "unit_notes": result.unit_notes,
            "summary": result.summary,
            "raw_json": raw,
            "raw_hash": hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:16] if raw else None,
            "request_id": result.request_id,
            "latency_ms": latency_ms,
            "error": result.error,
        }
        self.db.insert("evidence", row)
        pub = self._public(row)
        if self.bus:
            tid = self._thread_of(run_id)
            if tid:
                self.bus.emit(tid, run_id, "evidence.recorded", pub)
        return pub

    @staticmethod
    def _public(row: dict, with_raw: bool = False) -> dict:
        out = {k: v for k, v in row.items() if k not in ("raw_json", "args_json")}
        out["args"] = loads(row.get("args_json"), {})
        if with_raw:
            out["raw"] = loads(row.get("raw_json"))
        return out

    def get(self, id_or_label: str, run_id: Optional[str] = None) -> Optional[dict]:
        if id_or_label.startswith("evd_"):
            row = self.db.get("evidence", id_or_label)
        else:
            if not run_id:
                return None
            row = self.db.fetchone("SELECT * FROM evidence WHERE run_id = ? AND label = ?", [run_id, id_or_label.upper()])
        return self._public(row, with_raw=True) if row else None

    def list(self, run_id: str, with_raw: bool = False) -> list[dict]:
        rows = self.db.fetchall("SELECT * FROM evidence WHERE run_id = ? ORDER BY fetched_at, label", [run_id])
        return [self._public(r, with_raw=with_raw) for r in rows]

    def labels(self, run_id: str) -> set[str]:
        return {r["label"] for r in self.db.fetchall("SELECT label FROM evidence WHERE run_id = ?", [run_id])}

    def find_in_run(self, run_id: str, tool: str, args: dict) -> Optional[dict]:
        """恢复运行时复用同参数的成功证据，避免重复抓取。"""
        row = self.db.fetchone(
            "SELECT * FROM evidence WHERE run_id = ? AND tool = ? AND args_json = ? AND status IN ('ok','partial') ORDER BY fetched_at DESC LIMIT 1",
            [run_id, tool, dumps(args)],
        )
        return self._public(row, with_raw=True) if row else None
