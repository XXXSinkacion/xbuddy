"""长期记忆（用户级）与线程记忆（历史运行摘要）。全部为匿名 / 构造数据。"""
from __future__ import annotations

from typing import Optional

from ..db import Database, loads, new_id, now_ms

SEED_MEMORIES = [
    {"kind": "preference", "content": "研究偏好（构造数据）：以基本面与估值对比为主，重视数据口径与时点；不需要任何买卖建议或涨跌预测。", "tags": "preference,style"},
    {"kind": "preference", "content": "呈现偏好（构造数据）：结论先分“事实 / 推断 / 不确定”，图表优先用 K 线与对比表。", "tags": "preference,format"},
    {"kind": "watchlist", "content": "600519.SH 贵州茅台：长期关注白酒龙头估值变化（构造数据）", "tags": "watchlist,600519.SH,白酒"},
    {"kind": "watchlist", "content": "300750.SZ 宁德时代：关注新能源电池盈利能力（构造数据）", "tags": "watchlist,300750.SZ,新能源"},
]


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def seed_defaults(self) -> None:
        row = self.db.fetchone("SELECT COUNT(*) AS c FROM memories")
        if row and int(row["c"]) == 0:
            for m in SEED_MEMORIES:
                self.add(scope="user", kind=m["kind"], content=m["content"], tags=m["tags"], source_run_id=None)

    def add(self, *, scope: str, kind: str, content: str, tags: str = "", source_run_id: Optional[str] = None) -> dict:
        row = {"id": new_id("mem"), "scope": scope, "kind": kind, "content": content.strip(), "tags": tags or "", "source_run_id": source_run_id, "created_at": now_ms()}
        self.db.memory_insert(row)
        return row

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        return self.db.memory_search(query, limit=limit)

    def list(self, limit: int = 200) -> list[dict]:
        return self.db.fetchall("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", [limit])

    def delete(self, id_: str) -> None:
        self.db.memory_delete(id_)

    def thread_history(self, thread_id: str, exclude_run_id: Optional[str] = None, limit: int = 3) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT r.id, r.goal, r.status, r.finished_at, a.content_md FROM runs r LEFT JOIN artifacts a ON a.run_id = r.id AND a.type = 'report' "
            "WHERE r.thread_id = ? AND r.status IN ('completed','stopped','failed') ORDER BY r.created_at DESC LIMIT ?",
            [thread_id, limit + 1],
        )
        out = []
        for r in rows:
            if r["id"] == exclude_run_id:
                continue
            md = r.get("content_md") or ""
            summary = _extract_summary(md)
            out.append({"run_id": r["id"], "goal": r["goal"], "status": r["status"], "summary": summary})
        return out[:limit]


def _extract_summary(md: str, max_chars: int = 400) -> str:
    if not md:
        return ""
    # 取「摘要」或「核心结论」章节前几行
    for key in ("## 摘要", "## 核心结论"):
        i = md.find(key)
        if i != -1:
            seg = md[i + len(key) :].strip()
            nxt = seg.find("\n## ")
            seg = seg if nxt == -1 else seg[:nxt]
            return seg.strip()[:max_chars]
    return md.strip()[:max_chars]
