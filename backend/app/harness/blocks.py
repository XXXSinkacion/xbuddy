"""把工具结果 / 证据映射为前端动态 UI 块（与模型是否可用无关，保证可视化稳定）。"""
from __future__ import annotations

from typing import Any, Optional

from ..models import ToolResult
from ..tools.fuyao import fmt_ts

STATEMENT_NAMES = {
    "assets_total": "总资产", "total_current_assets": "流动资产", "non_current_nets_total": "非流动资产", "cash": "货币资金", "accounts_receivable": "应收账款",
    "total_debt": "总负债", "holder_equity_total": "所有者权益",
    "act_cash_flow_net": "经营现金流净额", "invest_cash_flow_net": "投资现金流净额", "financing_cash_flow_net": "筹资现金流净额",
    "pay_fixed_assets_etc_cash": "购建固定资产支付", "pay_dividends_profits_interest_cash": "分配股利/付息支付", "cash_equivalents_net_addition": "现金净增加额",
}

INDICATOR_NAMES = {
    "total_assets_growth_ratio": "总资产增长率%",
    "net_profit_yoy_growth_ratio": "净利润同比%",
    "operating_income_yoy_growth_ratio": "营收同比%",
    "operating_profit_yoy_growth_ratio": "营业利润同比%",
    "sale_gross_margin": "销售毛利率%",
    "sale_net_interest_ratio": "销售净利率%",
    "total_assets_net_ratio": "总资产收益率%",
    "index_deduct_weighted_avg_roe": "扣非加权ROE%",
    "index_weighted_avg_roe": "加权ROE%",
    "current_ratio": "流动比率",
    "quick_ratio": "速动比率",
    "assets_debt_ratio": "资产负债率%",
    "cash_ratio": "现金比率",
    "earned_interest_multiple": "已获利息倍数",
    "long_term_debt_equity_ratio": "长期负债权益比%",
    "total_assets_turnover_ratio": "总资产周转率(次)",
    "inventory_turnover_ratio": "存货周转率(次)",
    "current_assets_turnover_ratio": "流动资产周转率(次)",
    "receive_account_turnover_ratio": "应收账款周转率(次)",
    "cash_operating_index": "现金营运指数",
    "operating_cash_flow_net_divide_income": "销售现金比率%",
    "net_profit_cash_content": "净利润现金含量",
    "operating_cash_net_yoy_growth_ratio": "现金流量净额增长率%",
    "cash_meet_invest_ratio": "现金满足投资比率",
}


def _num(v: Any, nd: int = 2) -> Any:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _callout(level: str, text: str, label: Optional[str]) -> dict:
    return {"type": "callout", "level": level, "text": text, "evidence": label}


def blocks_from_result(tool: str, result: ToolResult, label: Optional[str]) -> list[dict]:
    """返回 0..n 个 UI 块。失败 / 缺失 / 过期都要显式呈现。"""
    if not result.ok:
        lvl = "missing" if result.status == "missing" else "error"
        return [_callout(lvl, f"{tool}：{result.error or result.summary}", label)]
    blocks: list[dict] = []
    if result.status == "stale":
        blocks.append(_callout("warning", f"{tool} 使用了过期缓存（原时点 {fmt_ts(result.meta.get('stale_from'))}），数值可能滞后", label))
    if "演示数据" in (result.source or ""):
        blocks.append(_callout("warning", f"{tool} 来自演示数据（fixture），非真实行情", label))
    data = result.data
    items = data.get("item") if isinstance(data, dict) else None
    ts = fmt_ts(result.data_timestamp) if result.data_timestamp else None

    if tool in ("get_kline", "get_index_kline") and items:
        series = [
            {"time": fmt_ts(b["date_ms"])[:10], "open": b.get("open_price"), "high": b.get("high_price"), "low": b.get("low_price"), "close": b.get("close_price"), "volume": b.get("volume")}
            for b in items
            if b.get("date_ms")
        ]
        blocks.append({"type": "kline", "title": f"{data.get('thscode', '')} 日K（{data.get('adjust') or '指数'}）", "evidence": label, "series": series, "unit": result.unit_notes, "timestamp": ts})
    elif tool in ("get_quote_snapshot", "get_index_snapshot") and items:
        blocks.append(
            {
                "type": "metrics",
                "title": "行情快照" if tool == "get_quote_snapshot" else "指数快照",
                "evidence": label,
                "timestamp": ts,
                "items": [
                    {"label": it.get("thscode"), "value": it.get("last_price"), "sub": f"{_num(it.get('price_change_ratio_pct'))}%", "trend": "up" if (it.get("price_change") or 0) > 0 else "down" if (it.get("price_change") or 0) < 0 else "flat", "note": f"成交额 {_num((it.get('turnover') or 0) / 1e8)} 亿"}
                    for it in items[:8]
                ],
            }
        )
    elif tool == "get_valuation_snapshot" and items:
        blocks.append({"type": "table", "title": "估值快照（倍）", "evidence": label, "timestamp": ts, "columns": ["名称", "代码", "PE(TTM)", "PE(MRQ)", "PB(MRQ)", "PS(TTM)", "PCF(TTM)"], "rows": [[it.get("name"), it.get("thscode"), _num(it.get("pe_ttm")), _num(it.get("pe_mrq")), _num(it.get("pb_mrq")), _num(it.get("ps_ttm")), _num(it.get("pcf_ttm"))] for it in items]})
    elif tool == "get_financial_indicators" and isinstance(data, dict):
        flat = data.get("flat") or {}
        rows = [[INDICATOR_NAMES.get(k, k), _num(v) if v is not None else "--"] for k, v in flat.items()]
        blocks.append({"type": "table", "title": f"财务指标 {data.get('thscode')} {data.get('report')}", "evidence": label, "columns": ["指标", "值"], "rows": rows})
    elif tool in ("get_income_statements", "get_balance_sheets", "get_cash_flow_statements") and items:
        if tool == "get_income_statements":
            cols = ["报告期", "营收(亿)", "净利润(亿)", "归母净利(亿)", "EPS(元)"]
            rows = [[f"{it.get('fiscal_year')}{it.get('fiscal_period')}", _num((it.get("operating_income") or 0) / 1e8), _num((it.get("net_profit") or 0) / 1e8), _num((it.get("parent_holder_net_profit") or 0) / 1e8), _num(it.get("basic_eps"), 4)] for it in items]
        else:
            keys = [k for k, v in items[0].items() if isinstance(v, (int, float)) and k not in ("fiscal_year", "report_date_ms", "period_end_ms")][:5]
            cols = ["报告期"] + [STATEMENT_NAMES.get(k, k) + "(亿)" for k in keys]
            rows = [[f"{it.get('fiscal_year')}{it.get('fiscal_period')}"] + [_num((it.get(k) or 0) / 1e8) for k in keys] for it in items]
        blocks.append({"type": "table", "title": {"get_income_statements": "利润表", "get_balance_sheets": "资产负债表", "get_cash_flow_statements": "现金流量表"}[tool], "evidence": label, "columns": cols, "rows": rows})
    elif tool == "get_index_constituents" and items:
        blocks.append({"type": "table", "title": f"成分股 {data.get('thscode', '')}（{len(items)} 只）", "evidence": label, "columns": ["代码", "名称"], "rows": [[it.get("thscode"), it.get("name")] for it in items[:40]]})
    elif tool == "find_ths_index" and items:
        blocks.append({"type": "table", "title": "匹配的同花顺指数", "evidence": label, "columns": ["代码", "名称"], "rows": [[it.get("thscode"), it.get("name")] for it in items[:20]]})
    elif tool == "search_ticker" and items:
        blocks.append({"type": "table", "title": "标的检索", "evidence": label, "columns": ["代码", "名称", "类型"], "rows": [[it.get("thscode"), it.get("name"), it.get("asset_type")] for it in items[:10]]})
    elif tool in ("get_limit_up_pool", "get_limit_down_pool") and items:
        blocks.append({"type": "table", "title": "涨停池" if tool == "get_limit_up_pool" else "跌停池", "evidence": label, "timestamp": ts, "columns": ["名称", "代码", "连板", "涨停时间", "封单(亿)", "原因"], "rows": [[it.get("name"), it.get("thscode"), it.get("continue_day_text"), it.get("limit_up_time"), _num((it.get("seal_money") or 0) / 1e8), it.get("limit_up_reason") or "--"] for it in items[:30]]})
    elif tool == "get_limit_up_statistics" and isinstance(data, dict):
        row = (items or [data])[0] if (items or data) else {}
        if isinstance(row, dict):
            blocks.append({"type": "metrics", "title": "涨停市场统计", "evidence": label, "timestamp": ts, "items": [{"label": k, "value": v} for k, v in row.items() if not isinstance(v, (dict, list))][:10]})
    elif tool in ("get_hot_stock_list", "get_skyrocket_list") and items:
        blocks.append({"type": "table", "title": "热股榜" if tool == "get_hot_stock_list" else "飙升榜", "evidence": label, "timestamp": ts, "columns": ["排名", "名称", "代码", "热度", "变化"], "rows": [[it.get("rank"), it.get("name"), it.get("thscode"), it.get("heat"), it.get("rank_change")] for it in items[:20]]})
    elif tool == "compute_price_stats" and isinstance(data, dict):
        blocks.append({"type": "metrics", "title": f"{data.get('thscode', '')} 区间统计（{data.get('start_date')}~{data.get('end_date')}）", "evidence": label, "items": [{"label": "区间收益", "value": f"{data.get('period_return_pct')}%", "trend": "up" if (data.get("period_return_pct") or 0) > 0 else "down"}, {"label": "最大回撤", "value": f"{data.get('max_drawdown_pct')}%"}, {"label": "年化波动", "value": f"{data.get('annualized_volatility_pct')}%"}, {"label": "较MA20", "value": f"{data.get('pct_above_ma20')}%"}, {"label": "涨/跌天数", "value": f"{data.get('up_days')}/{data.get('down_days')}"}]})
    elif tool == "build_comparison" and isinstance(data, dict) and data.get("rows"):
        blocks.append({"type": "table", "title": "对比表", "evidence": label, "columns": data.get("columns"), "rows": [[_num(c) if isinstance(c, float) else c for c in r] for r in data["rows"]]})
    elif tool.startswith("ifind_"):
        text = data if isinstance(data, str) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            cols = list(data[0].keys())[:8]
            blocks.append({"type": "table", "title": f"iFinD {tool[6:]}", "evidence": label, "columns": cols, "rows": [[r.get(c) for c in cols] for r in data[:30]]})
        else:
            blocks.append({"type": "text", "text": f"**iFinD {tool[6:]}** [{label}]\n\n" + (str(text or result.summary))[:1500]})
    if result.status == "partial":
        blocks.append(_callout("warning", f"{tool} 部分数据缺失：{result.summary[-160:]}", label))
    return blocks
