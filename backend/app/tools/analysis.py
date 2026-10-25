"""本地分析工具（L0，无外部依赖）：基于已有证据做确定性计算，并支持渐进披露与工作记忆。"""
from __future__ import annotations

import math
from typing import Any, Optional

from ..models import ToolResult
from .fuyao import fmt_ts
from .registry import ToolContext, ToolRegistry, ToolSpec


def _load_evidence(ctx: ToolContext, label: str) -> Optional[dict]:
    if ctx.evidence is None:
        return None
    return ctx.evidence.get(label.strip().upper(), run_id=ctx.run_id)


def price_stats(bars: list[dict]) -> dict:
    closes = [b["close_price"] for b in bars if b.get("close_price")]
    if len(closes) < 2:
        return {}
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    vol_annual = math.sqrt(var) * math.sqrt(242)
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    turn = [b.get("turnover") or 0 for b in bars]
    return {
        "bars": len(bars),
        "start_date": fmt_ts(bars[0]["date_ms"])[:10],
        "end_date": fmt_ts(bars[-1]["date_ms"])[:10],
        "start_close": closes[0],
        "end_close": closes[-1],
        "period_return_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "annualized_volatility_pct": round(vol_annual * 100, 2),
        "highest_close": max(closes),
        "lowest_close": min(closes),
        "avg_daily_turnover": round(sum(turn) / len(turn), 2) if turn else None,
        "ma20_last": round(sum(closes[-20:]) / min(20, len(closes)), 2),
        "pct_above_ma20": round((closes[-1] / (sum(closes[-20:]) / min(20, len(closes))) - 1) * 100, 2),
        "up_days": sum(1 for r in rets if r > 0),
        "down_days": sum(1 for r in rets if r < 0),
    }


def register_analysis_tools(registry: ToolRegistry) -> list[str]:
    names: list[str] = []

    def reg(spec: ToolSpec):
        registry.register(spec)
        names.append(spec.name)

    async def compute_price_stats(args: dict, ctx: ToolContext) -> ToolResult:
        ev = _load_evidence(ctx, args["evidence_label"])
        if not ev:
            return ToolResult.fail(f"证据 {args['evidence_label']} 不存在", status="error")
        raw = ev.get("raw") or {}
        bars = raw.get("item") if isinstance(raw, dict) else None
        if not bars or "close_price" not in bars[0]:
            return ToolResult.fail(f"证据 {ev['label']} 不是 K 线数据，无法计算", status="error")
        st = price_stats(bars)
        code = raw.get("thscode", "")
        return ToolResult(
            ok=True,
            data=st | {"thscode": code, "based_on": ev["label"]},
            source="local",
            data_timestamp=ev.get("data_timestamp"),
            unit_notes="基于证据 " + ev["label"] + " 的收盘价计算；区间收益=末收/首收-1；最大回撤=收盘价相对前高最大跌幅；年化波动=日对数收益标准差×√242；均为历史统计，不含预测",
            summary=f"{code} {st['start_date']}~{st['end_date']}：区间收益 {st['period_return_pct']}%，最大回撤 {st['max_drawdown_pct']}%，年化波动 {st['annualized_volatility_pct']}%，末收 {st['end_close']}（较 MA20 {st['pct_above_ma20']}%），上涨 {st['up_days']} 日 / 下跌 {st['down_days']} 日",
        )

    reg(ToolSpec(name="compute_price_stats", description="基于某条 K 线证据（如 E3）计算区间收益、最大回撤、年化波动率、MA20 偏离、涨跌天数等历史统计量（不含预测）。", parameters={"type": "object", "properties": {"evidence_label": {"type": "string", "description": "K 线证据编号，如 E3"}}, "required": ["evidence_label"]}, handler=compute_price_stats, risk="L0", source="local", tags=("analysis",)))

    async def build_comparison(args: dict, ctx: ToolContext) -> ToolResult:
        labels = [s.strip().upper() for s in str(args["evidence_labels"]).replace("，", ",").split(",") if s.strip()]
        rows: dict[str, dict] = {}
        used = []
        for lb in labels:
            ev = _load_evidence(ctx, lb)
            if not ev:
                continue
            raw = ev.get("raw")
            items = raw.get("item") if isinstance(raw, dict) else raw if isinstance(raw, list) else None
            if not items:
                continue
            used.append(lb)
            for it in items:
                if not isinstance(it, dict) or not it.get("thscode"):
                    continue
                row = rows.setdefault(it["thscode"], {"thscode": it["thscode"]})
                for k, v in it.items():
                    if k in ("thscode",):
                        continue
                    if isinstance(v, (int, float, str)) or v is None:
                        row.setdefault(k, v)
        if not rows:
            return ToolResult.fail("所给证据中没有可按 thscode 合并的记录", status="error")
        cols = ["thscode"] + sorted({k for r in rows.values() for k in r if k != "thscode"})
        table = [[r.get(c) for c in cols] for r in rows.values()]
        return ToolResult(ok=True, data={"columns": cols, "rows": table, "based_on": used}, source="local", unit_notes="按 thscode 合并多条证据的字段，单位沿用各证据", summary=f"合并 {len(used)} 条证据得到 {len(table)} 行 × {len(cols)} 列对比表（{', '.join(used)}）")

    reg(ToolSpec(name="build_comparison", description="把多条包含 item 列表的证据（估值快照、行情快照、成分股等）按 thscode 合并为一张对比表。", parameters={"type": "object", "properties": {"evidence_labels": {"type": "string", "description": "逗号分隔证据编号，如 E2,E4"}}, "required": ["evidence_labels"]}, handler=build_comparison, risk="L0", source="local", tags=("analysis",)))

    async def get_evidence(args: dict, ctx: ToolContext) -> ToolResult:
        ev = _load_evidence(ctx, args["evidence_label"])
        if not ev:
            return ToolResult.fail(f"证据 {args['evidence_label']} 不存在", status="error")
        raw = ev.get("raw")
        trimmed = raw
        if isinstance(raw, dict) and isinstance(raw.get("item"), list) and len(raw["item"]) > 200:
            trimmed = raw | {"item": raw["item"][:200], "_truncated": True}
        return ToolResult(ok=True, data=trimmed, source="local", data_timestamp=ev.get("data_timestamp"), unit_notes=ev.get("unit_notes") or "", summary=f"返回证据 {ev['label']}（{ev['tool']}）的原始数据", meta={"no_evidence": True})

    reg(ToolSpec(name="get_evidence", description="按编号取回某条证据的原始数据（渐进披露：上下文中只保留摘要时用它取全文）。", parameters={"type": "object", "properties": {"evidence_label": {"type": "string"}}, "required": ["evidence_label"]}, handler=get_evidence, risk="L0", source="local", tags=("context",)))

    async def note_fact(args: dict, ctx: ToolContext) -> ToolResult:
        key = str(args["key"]).strip()
        ctx.working_memory[key] = args.get("value")
        return ToolResult(ok=True, data={"key": key, "value": args.get("value")}, source="local", summary=f"已记入工作记忆：{key} = {str(args.get('value'))[:80]}", meta={"no_evidence": True})

    reg(ToolSpec(name="note_fact", description="把一个关键事实写入本次运行的工作记忆（供后续任务复用，如已确认的 thscode、报告期）。", parameters={"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}, handler=note_fact, risk="L0", source="local", tags=("context",)))

    return names
