"""SQLite 持久化层：表结构、通用读写、ID 生成。

设计要点
- 事件溯源：所有状态变化写入 events（含 seq），SSE 可按 seq 回放。
- 检查点：runs.state_json 为最新检查点，checkpoints 表保留历史。
- 长期记忆：memories + FTS5 虚表（不可用时回退 LIKE）。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def now_ms() -> int:
    return int(time.time() * 1000)


SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  run_id TEXT,
  role TEXT NOT NULL,
  blocks_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'llm',
  budget_json TEXT NOT NULL DEFAULT '{}',
  usage_json TEXT NOT NULL DEFAULT '{}',
  state_json TEXT NOT NULL DEFAULT '{}',
  stop_reason TEXT,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id, created_at);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  depends_on_json TEXT NOT NULL DEFAULT '[]',
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  acceptance TEXT,
  pipeline_json TEXT,
  status TEXT NOT NULL,
  result_json TEXT,
  error TEXT,
  started_at INTEGER,
  ended_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, idx);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT NOT NULL,
  run_id TEXT,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT,
  label TEXT NOT NULL,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  data_timestamp INTEGER,
  unit_notes TEXT,
  summary TEXT,
  raw_json TEXT,
  raw_hash TEXT,
  request_id TEXT,
  latency_ms INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_tool ON evidence(tool, args_json);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  content_md TEXT,
  blocks_json TEXT,
  lint_json TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  task_id TEXT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  created_at INTEGER NOT NULL,
  decided_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  source_run_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  step INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id, step);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  id UNINDEXED, content, tags, tokenize='unicode61'
);
"""


class Database:
    """线程安全的轻量 SQLite 封装（单进程）。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.fts_enabled = False
        self.init_schema()

    # ---------- schema ----------
    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            try:
                self._conn.executescript(FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False

    # ---------- generic ----------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row.keys())
        qs = ", ".join("?" for _ in row)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))

    def update(self, table: str, id_: str, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*fields.values(), id_])

    def get(self, table: str, id_: str) -> Optional[dict]:
        return self.fetchone(f"SELECT * FROM {table} WHERE id = ?", [id_])

    def delete(self, table: str, id_: str) -> None:
        self.execute(f"DELETE FROM {table} WHERE id = ?", [id_])

    # ---------- events ----------
    def next_event_seq(self, thread_id: str) -> int:
        row = self.fetchone("SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE thread_id = ?", [thread_id])
        return int(row["m"]) + 1 if row else 1

    # ---------- memories (FTS) ----------
    def memory_insert(self, row: dict) -> None:
        self.insert("memories", row)
        if self.fts_enabled:
            self.execute(
                "INSERT INTO memories_fts (id, content, tags) VALUES (?, ?, ?)",
                [row["id"], row["content"], row.get("tags", "")],
            )

    def memory_delete(self, id_: str) -> None:
        self.delete("memories", id_)
        if self.fts_enabled:
            self.execute("DELETE FROM memories_fts WHERE id = ?", [id_])

    def memory_search(self, query: str, limit: int = 5) -> list[dict]:
        terms = [t for t in _tokenize(query) if t]
        if not terms:
            return self.fetchall("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", [limit])
        if self.fts_enabled:
            try:
                fts_q = " OR ".join(f'"{t}"' for t in terms[:12])
                rows = self.fetchall(
                    "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.id "
                    "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",
                    [fts_q, limit],
                )
                if rows:
                    return rows
            except sqlite3.OperationalError:
                pass
        # 回退：LIKE 匹配
        clauses = " OR ".join("content LIKE ? OR tags LIKE ?" for _ in terms[:8])
        params: list[Any] = []
        for t in terms[:8]:
            params += [f"%{t}%", f"%{t}%"]
        params.append(limit)
        return self.fetchall(f"SELECT * FROM memories WHERE {clauses} ORDER BY created_at DESC LIMIT ?", params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _tokenize(text: str) -> list[str]:
    """极简分词：英文/数字按词，中文按 2-gram，供 FTS / LIKE 检索。"""
    import re

    out: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9_.]+|[一-鿿]+", text):
        tok = m.group(0)
        if re.match(r"[一-鿿]+", tok):
            if len(tok) <= 2:
                out.append(tok)
            else:
                out.extend(tok[i : i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok)
    # 去重保序
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: Optional[str], default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default
