"""事件总线：落库（事件溯源）+ JSONL 日志 + SSE 扇出。"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from .db import Database, dumps, now_ms

log = logging.getLogger("xbuddy.events")


class EventBus:
    def __init__(self, db: Database, log_dir: Optional[Path] = None):
        self.db = db
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._log_path: Optional[Path] = None
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_path = log_dir / "events.jsonl"

    def emit(self, thread_id: str, run_id: Optional[str], type_: str, payload: Optional[dict] = None) -> dict:
        payload = payload or {}
        seq = self.db.next_event_seq(thread_id)
        evt = {
            "thread_id": thread_id,
            "run_id": run_id,
            "seq": seq,
            "type": type_,
            "payload": payload,
            "created_at": now_ms(),
        }
        self.db.insert(
            "events",
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "seq": seq,
                "type": type_,
                "payload_json": dumps(payload),
                "created_at": evt["created_at"],
            },
        )
        if self._log_path is not None:
            try:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(evt, ensure_ascii=False, default=str) + "\n")
            except Exception:  # 日志失败不影响主流程
                log.debug("failed to append event log", exc_info=True)
        for q in list(self._subs.get(thread_id, ())):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass
        return evt

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(thread_id, set()).add(q)
        return q

    def unsubscribe(self, thread_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(thread_id)
        if subs and q in subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(thread_id, None)

    def replay(self, thread_id: str, since_seq: int = 0, run_id: Optional[str] = None) -> list[dict]:
        if run_id:
            rows = self.db.fetchall(
                "SELECT * FROM events WHERE thread_id = ? AND run_id = ? AND seq > ? ORDER BY seq",
                [thread_id, run_id, since_seq],
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM events WHERE thread_id = ? AND seq > ? ORDER BY seq", [thread_id, since_seq]
            )
        out = []
        for r in rows:
            out.append(
                {
                    "thread_id": r["thread_id"],
                    "run_id": r["run_id"],
                    "seq": r["seq"],
                    "type": r["type"],
                    "payload": json.loads(r["payload_json"]),
                    "created_at": r["created_at"],
                }
            )
        return out
