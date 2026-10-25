"""上下文管理：token 估算、工具结果紧凑渲染、超预算时的观察压缩。"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..models import ToolResult
from ..tools.fuyao import fmt_ts

_CJK = re.compile(r"[一-鿿　-〿＀-￯]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


def message_tokens(msg: dict) -> int:
    n = estimate_tokens(str(msg.get("content") or ""))
    for tc in msg.get("tool_calls") or []:
        n += estimate_tokens(json.dumps(tc, ensure_ascii=False))
    return n + 4


def messages_tokens(messages: list[dict]) -> int:
    return sum(message_tokens(m) for m in messages)


PREVIEW_FIELDS = ("thscode", "name", "date_ms", "date", "last_price", "price_change_ratio_pct", "close_price", "open_price", "high_price", "low_price", "volume", "turnover", "pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm", "rank", "heat", "continue_day_text", "limit_up_reason", "seal_money", "fiscal_year", "fiscal_period", "operating_income", "net_profit", "parent_holder_net_profit", "basic_eps")


def _preview_items(items: list[Any], max_rows: int) -> str:
    rows = []
    for it in items[:max_rows]:
        if isinstance(it, dict):
            keep = {k: it[k] for k in PREVIEW_FIELDS if k in it}
            if not keep:
                keep = dict(list(it.items())[:8])
            if "date_ms" in keep:
                keep["date"] = fmt_ts(keep.pop("date_ms"))[:10]
            rows.append(json.dumps(keep, ensure_ascii=False, default=str))
        else:
            rows.append(json.dumps(it, ensure_ascii=False, default=str)[:200])
    more = f"\n…（共 {len(items)} 条，仅显示前 {min(max_rows, len(items))} 条）" if len(items) > max_rows else ""
    return "\n".join(rows) + more


def render_tool_result(result: ToolResult, label: Optional[str], *, max_rows: int = 12, max_chars: int = 2500) -> str:
    head = [
        f"证据编号: {label or '-'}",
        f"状态: {result.status}{'' if result.ok else '（失败）'}",
        f"来源: {result.source}",
        f"数据时点: {fmt_ts(result.data_timestamp) if result.data_timestamp else '未提供'}",
    ]
    if result.unit_notes:
        head.append(f"单位/口径: {result.unit_notes}")
    if result.error:
        head.append(f"错误: {result.error}")
    head.append(f"摘要: {result.summary}")
    body = ""
    data = result.data
    if isinstance(data, dict):
        items = data.get("item") or data.get("items") or data.get("rows")
        if isinstance(items, list) and items:
            cols = data.get("columns")
            body = ("列: " + ", ".join(map(str, cols)) + "\n" if cols else "") + _preview_items(items, max_rows)
        else:
            flat = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
            body = json.dumps(flat, ensure_ascii=False, default=str)
            if "flat" in data and isinstance(data["flat"], dict):
                body += "\n指标: " + json.dumps(data["flat"], ensure_ascii=False)
            if "abilities" in data:
                pass
    elif isinstance(data, list):
        body = _preview_items(data, max_rows)
    elif data is not None:
        body = str(data)
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n…（已截断，完整数据请调用 get_evidence(\"{label}\")）"
    text = "\n".join(head)
    if body:
        text += "\n数据预览:\n" + body
    if label and result.data is not None:
        text += f"\n（完整原始数据可通过 get_evidence(\"{label}\") 获取）"
    return text


def _groups(body: list[dict]) -> list[list[dict]]:
    """把 assistant(tool_calls)+tool 消息分组，保证压缩不破坏 tool_call 配对。"""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for m in body:
        if m.get("role") == "tool" and cur:
            cur.append(m)
            continue
        if cur:
            groups.append(cur)
        cur = [m]
    if cur:
        groups.append(cur)
    return groups


class ContextManager:
    def __init__(self, budget_tokens: int = 24000, keep_last_groups: int = 2, head_messages: int = 2):
        self.budget = budget_tokens
        self.keep_last_groups = keep_last_groups
        self.head_messages = head_messages

    def compress_if_needed(self, messages: list[dict]) -> tuple[list[dict], Optional[dict]]:
        before = messages_tokens(messages)
        if before <= self.budget:
            return messages, None
        head = messages[: self.head_messages]
        body = messages[self.head_messages :]
        groups = _groups(body)
        if len(groups) <= self.keep_last_groups:
            # 只能截断保护区内的长工具结果
            out = head + [self._truncate(m) for m in body]
            after = messages_tokens(out)
            return out, {"before": before, "after": after, "saved": before - after, "folded_messages": 0, "mode": "truncate"}
        fold = groups[: -self.keep_last_groups]
        keep = groups[-self.keep_last_groups :]
        lines = []
        folded = 0
        for g in fold:
            for m in g:
                folded += 1
                role = m.get("role")
                if role == "tool":
                    content = str(m.get("content") or "")
                    label = re.search(r"证据编号:\s*(E\d+)", content)
                    summ = re.search(r"摘要:\s*(.+)", content)
                    status = re.search(r"状态:\s*(\S+)", content)
                    lines.append(f"- [{label.group(1) if label else '-'}] {status.group(1) if status else ''} {summ.group(1)[:160] if summ else content[:160]}")
                elif role == "assistant":
                    names = [tc["function"]["name"] for tc in m.get("tool_calls") or [] if "function" in tc]
                    if names:
                        lines.append(f"- 调用工具: {', '.join(names)}")
                    if m.get("content"):
                        lines.append(f"- 助手: {str(m['content'])[:160]}")
                else:
                    lines.append(f"- {role}: {str(m.get('content') or '')[:160]}")
        folded_msg = {
            "role": "user",
            "content": "【已压缩的早期观察摘要】以下为本任务更早步骤的精简记录；原始数据可用 get_evidence(E#) 取回：\n" + "\n".join(lines),
        }
        out = head + [folded_msg] + [m for g in keep for m in g]
        after = messages_tokens(out)
        return out, {"before": before, "after": after, "saved": before - after, "folded_messages": folded, "mode": "fold"}

    @staticmethod
    def _truncate(m: dict, max_chars: int = 1200) -> dict:
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > max_chars:
            return m | {"content": str(m["content"])[:max_chars] + "\n…（已截断）"}
        return m
