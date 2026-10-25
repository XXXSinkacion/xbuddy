"""规划器：LLM 规划（JSON）→ 校验；失败时降级为规则模板规划（带确定性流水线）。"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..llm.base import LLMUnavailable, extract_json_object
from ..llm.router import LLMRouter
from ..models import Plan, PlanTask
from ..tools.registry import ToolRegistry

log = logging.getLogger("xbuddy.planner")
TZ = timezone(timedelta(hours=8))

PLANNER_SYSTEM = """你是「投资 X Buddy」研究工作台的规划器。把用户的研究目标拆成 2~6 个可执行、可验收的任务，每个任务指定可用工具。
原则：
1. 第一个任务通常是用 search_ticker 消歧标的，得到完整 thscode；后续任务依赖它。
2. 只规划能用现有工具完成的事；不要规划“预测涨跌”“给出买卖建议”类任务。
3. 每个任务写清 objective（做什么、取什么数据、算什么）与 acceptance（验收：拿到哪些字段/证据）。
4. 任务数 ≤ 6，依赖关系用 depends_on 表示（引用任务 id）。
5. 只输出 JSON，不要多余文字。格式：
{"title": "研究标题", "assumptions": ["假设1"], "tasks": [{"id": "t1", "title": "...", "objective": "...", "tools": ["search_ticker"], "depends_on": [], "acceptance": "..."}]}"""

STOPWORDS = ["研究一下", "请研究", "研究", "走势统计", "统计", "一年", "三个月", "半年", "一个月", "分析一下", "分析", "帮我", "请", "看看", "一下", "对比", "比较", "近期", "最近", "最新", "近三个月", "近3个月", "近一个月", "近1个月", "近半年", "近一年", "行情", "估值", "财务", "基本面", "走势", "表现", "情况", "并", "与", "和", "及", "的", "进行", "给出", "列出", "总结", "报告", "复盘", "今日", "今天", "市场", "情绪", "板块", "行业", "同业", "指标", "数据", "盈利能力", "盈利", "成长", "现金流", "风险", "事实", "不确定", "结论", "怎么样", "如何", "是否", "值不值得", "值得", "买", "卖", "涨", "跌", "预测", "建议", "持有", "股票", "公司", "个股", "龙头", "整体", "A股", "大盘", "主要", "指数"]

SECTOR_KEYWORDS = ["白酒", "新能源", "光伏", "银行", "半导体", "芯片", "医药", "券商", "汽车", "电池", "锂电", "保险", "地产", "房地产", "煤炭", "有色", "军工", "消费", "食品饮料", "家电", "通信", "计算机", "软件", "人工智能", "AI", "机器人", "储能", "风电", "钢铁", "化工", "电力", "航空", "旅游", "零售", "传媒", "游戏", "医疗", "生物", "环保", "建筑", "建材", "农业", "养殖", "物流", "港口", "电子", "光模块", "算力", "低空经济", "核电", "可控核聚变", "黄金", "证券"]

CODE_RE = re.compile(r"\b(\d{6})\.(SH|SZ|BJ|TI)\b", re.I)
BARE_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def date_days_ago(days: int) -> str:
    return (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")


def latest_report_period(now: Optional[datetime] = None) -> list[str]:
    """按披露节奏推测最可能已披露的报告期（含回退序列）。"""
    now = now or datetime.now(TZ)
    y, m = now.year, now.month
    if m >= 11:
        seq = [f"{y}-3", f"{y}-2", f"{y - 1}-4"]
    elif m >= 9:
        seq = [f"{y}-2", f"{y}-1", f"{y - 1}-4"]
    elif m >= 5:
        seq = [f"{y}-1", f"{y - 1}-4", f"{y - 1}-3"]
    else:
        seq = [f"{y - 1}-3", f"{y - 1}-4", f"{y - 1}-2"]
    return seq


def extract_targets(goal: str) -> tuple[list[str], list[str]]:
    """返回 (显式代码列表, 候选名称列表)。"""
    codes = [f"{m.group(1)}.{m.group(2).upper()}" for m in CODE_RE.finditer(goal)]
    text = CODE_RE.sub(" ", goal)
    bare = [c for c in BARE_CODE_RE.findall(text) if c not in {c.split(".")[0] for c in codes}]
    text = BARE_CODE_RE.sub(" ", text)
    text = re.sub(r"[（(][^）)]*[）)]", " ", text)
    for sw in sorted(STOPWORDS, key=len, reverse=True):
        text = text.replace(sw, " ")
    for sk in SECTOR_KEYWORDS:
        text = text.replace(sk, " ")
    names = [t for t in re.findall(r"[一-鿿A-Za-z]{2,8}", text)]
    seen: set[str] = set()
    uniq = []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    # 已给出明确代码时不再用名称片段检索，避免“招商”这类残片命中错误标的
    if codes or bare:
        return codes + bare, []
    return [], uniq[:3]


def detect_intents(goal: str) -> dict:
    g = goal
    intents = {
        "prices": any(k in g for k in ("行情", "走势", "K线", "k线", "股价", "涨跌", "表现", "波动", "回撤")),
        "valuation": any(k in g for k in ("估值", "PE", "PB", "市盈", "市净", "贵", "便宜")),
        "financials": any(k in g for k in ("财务", "财报", "盈利", "营收", "利润", "ROE", "roe", "毛利", "净利", "现金流", "负债", "基本面", "业绩")),
        "sector": any(k in g for k in ("板块", "行业", "同业", "竞品", "对比", "比较", "横向")) or any(k in g for k in SECTOR_KEYWORDS),
        "sentiment": any(k in g for k in ("情绪", "涨停", "跌停", "热榜", "热股", "复盘", "大盘", "市场宽度", "连板", "炸板")),
        "index": any(k in g for k in ("指数", "沪深300", "上证", "创业板", "深成指", "沪深 300")),
    }
    if not any(intents.values()):
        intents.update(prices=True, valuation=True, financials=True)
    return intents


def detect_sector_keyword(goal: str) -> Optional[str]:
    for k in SECTOR_KEYWORDS:
        if k in goal:
            return k
    return None


def template_plan(goal: str, registry: ToolRegistry) -> Plan:
    codes, names = extract_targets(goal)
    intents = detect_intents(goal)
    sector_kw = detect_sector_keyword(goal)
    tasks: list[PlanTask] = []
    assumptions: list[str] = ["模板规划（模型不可用或未配置）：按关键词识别研究意图与标的，流程为确定性流水线。"]
    has = lambda n: registry.get(n) is not None  # noqa: E731

    queries = codes + names
    have_targets = bool(queries) and has("search_ticker")
    if have_targets:
        steps = [{"tool": "search_ticker", "args": {"q": q, "limit": 5}, "save_as": "targets", "pick": "first_a_share_or_index"} for q in queries[:3]]
        tasks.append(PlanTask(id="t1", title="标的识别与消歧", objective=f"用 search_ticker 解析研究对象（{', '.join(queries[:3])}）得到完整 thscode 与名称。", tools=["search_ticker"], acceptance="工作记忆 targets 中至少 1 个 thscode", pipeline=steps))
    else:
        assumptions.append("未从目标中识别出具体标的，仅执行市场层面的任务。")

    dep = ["t1"] if have_targets else []
    if have_targets and intents["prices"]:
        tasks.append(PlanTask(id="t2", title="行情与近 90 日走势", objective="获取标的最新行情快照与近 90 个自然日前复权日 K 线，并计算区间收益、最大回撤、年化波动等历史统计量。", tools=["get_quote_snapshot", "get_kline", "compute_price_stats"], depends_on=dep, acceptance="快照证据 + K 线证据 + 统计量证据", pipeline=[{"tool": "get_quote_snapshot", "args": {"thscodes": "$targets_csv"}}, {"tool": "get_kline", "args": {"thscode": "$target0", "start_date": "$d90", "end_date": "$today", "adjust": "forward"}, "save_as": "kline_label"}, {"tool": "compute_price_stats", "args": {"evidence_label": "$kline_label"}}, {"tool": "get_kline", "args": {"thscode": "$target1", "start_date": "$d90", "end_date": "$today", "adjust": "forward"}, "save_as": "kline_label_1", "optional": True}, {"tool": "compute_price_stats", "args": {"evidence_label": "$kline_label_1"}, "optional": True}]))
    if have_targets and intents["valuation"]:
        tasks.append(PlanTask(id="t3", title="估值快照", objective="获取标的最新 PE(TTM/MRQ)、PB、PS、PCF 估值倍数。", tools=["get_valuation_snapshot"], depends_on=dep, acceptance="估值证据，字段含 pe_ttm/pb_mrq", pipeline=[{"tool": "get_valuation_snapshot", "args": {"thscodes": "$targets_csv"}}]))
    if have_targets and intents["financials"]:
        tasks.append(PlanTask(id="t4", title="财务指标与利润表", objective="获取最新可得报告期的五类财务指标与最近 4 期年报利润表。", tools=["get_financial_indicators", "get_income_statements"], depends_on=dep, acceptance="财务指标证据 + 利润表证据（缺失需标注）", pipeline=[{"tool": "get_financial_indicators", "args": {"thscode": "$target0", "report": "$report0"}, "fallback_args": [{"report": "$report1"}, {"report": "$report2"}]}, {"tool": "get_income_statements", "args": {"thscode": "$target0", "period": "annual", "limit": 4}}, {"tool": "get_financial_indicators", "args": {"thscode": "$target1", "report": "$report0"}, "fallback_args": [{"report": "$report1"}], "optional": True}]))
    if intents["sector"] and has("find_ths_index"):
        kw = sector_kw or (names[0] if names else None)
        if kw:
            tasks.append(PlanTask(id="t5", title=f"板块对比：{kw}", objective=f"查找“{kw}”相关同花顺板块，取成分股并批量获取估值快照，生成对比表。", tools=["find_ths_index", "get_index_constituents", "get_valuation_snapshot", "build_comparison", "get_index_snapshot"], depends_on=[], acceptance="板块代码 + 成分股 + 估值对比表", pipeline=[{"tool": "find_ths_index", "args": {"keyword": kw, "tag": "cn_concept"}, "save_as": "sector_index", "fallback_args": [{"tag": "industry"}]}, {"tool": "get_index_snapshot", "args": {"thscodes": "$sector_index"}, "optional": True}, {"tool": "get_index_constituents", "args": {"thscode": "$sector_index"}, "save_as": "constituents"}, {"tool": "get_valuation_snapshot", "args": {"thscodes": "$constituents_csv"}, "save_as": "sector_val_label"}, {"tool": "build_comparison", "args": {"evidence_labels": "$sector_val_label"}}]))
        else:
            assumptions.append("目标提到板块/对比，但未识别出板块关键词，已跳过板块任务。")
    if intents["sentiment"] or intents["index"]:
        tasks.append(PlanTask(id="t6", title="市场情绪与主要指数", objective="获取主要指数快照、涨停市场统计、涨停池与热股榜，刻画当日市场情绪（只描述，不预测）。", tools=["get_index_snapshot", "get_limit_up_statistics", "get_limit_up_pool", "get_hot_stock_list"], depends_on=[], acceptance="指数快照 + 涨停统计 + 热榜证据", pipeline=[{"tool": "get_index_snapshot", "args": {"thscodes": "000001.SH,399001.SZ,399006.SZ,000300.SH"}}, {"tool": "get_limit_up_statistics", "args": {}}, {"tool": "get_limit_up_pool", "args": {"size": 30}}, {"tool": "get_hot_stock_list", "args": {"period": "day", "limit": 20}}]))
    if not tasks:
        tasks.append(PlanTask(id="t1", title="交易日历与市场概览", objective="获取交易日历与主要指数快照。", tools=["get_trading_days", "get_index_snapshot"], acceptance="交易日历 + 指数快照", pipeline=[{"tool": "get_trading_days", "args": {}}, {"tool": "get_index_snapshot", "args": {"thscodes": "000001.SH,399001.SZ,399006.SZ,000300.SH"}}]))
    title = goal.strip()[:40] or "研究任务"
    return Plan(title=title, tasks=tasks, assumptions=assumptions, mode="template")


class Planner:
    def __init__(self, llm: Optional[LLMRouter], registry: ToolRegistry):
        self.llm = llm
        self.registry = registry

    async def plan(self, goal: str, *, memories: list[dict], history: list[dict], on_event: Optional[Callable[[str, dict], None]] = None, force_template: bool = False) -> tuple[Plan, Optional[str]]:
        """返回 (计划, 降级原因|None)。"""
        if force_template:
            return template_plan(goal, self.registry), None
        if self.llm is None or not self.llm.available:
            return template_plan(goal, self.registry), "模型未配置，使用模板规划"
        user = self._user_prompt(goal, memories, history)
        last_err = ""
        for attempt in range(2):
            try:
                resp = await self.llm.chat([{"role": "system", "content": PLANNER_SYSTEM}, {"role": "user", "content": user if attempt == 0 else user + f"\n\n上一次输出无法解析（{last_err}），请只输出合法 JSON。"}], purpose="plan", on_event=on_event, temperature=0.1, max_tokens=1500, json_mode=True)
            except LLMUnavailable as e:
                return template_plan(goal, self.registry), f"模型不可用（{e}），降级为模板规划"
            obj = extract_json_object(resp.content or "")
            plan, err = self._validate(obj, goal)
            if plan:
                return plan, None
            last_err = err
            log.warning("plan parse failed: %s", err)
        return template_plan(goal, self.registry), f"模型输出无法解析为计划（{last_err}），降级为模板规划"

    def _user_prompt(self, goal: str, memories: list[dict], history: list[dict]) -> str:
        parts = [f"今天：{today_str()}（Asia/Shanghai）", f"研究目标：{goal}", "", "可用工具（名称 [风险/来源]: 说明）：", self.registry.catalog_text()]
        if memories:
            parts += ["", "相关长期记忆（匿名构造数据，可参考用户偏好）："] + [f"- [{m['kind']}] {m['content']}" for m in memories[:5]]
        if history:
            parts += ["", "本线程历史研究："] + [f"- 目标：{h['goal']}；摘要：{h['summary'][:200]}" for h in history]
        parts += ["", "请输出 JSON 计划。"]
        return "\n".join(parts)

    def _validate(self, obj: Optional[dict], goal: str) -> tuple[Optional[Plan], str]:
        if not obj or not isinstance(obj.get("tasks"), list) or not obj["tasks"]:
            return None, "缺少 tasks"
        tasks: list[PlanTask] = []
        ids: set[str] = set()
        for i, t in enumerate(obj["tasks"][:6]):
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or f"t{i + 1}")
            if tid in ids:
                tid = f"{tid}_{i}"
            ids.add(tid)
            tools = [n for n in (t.get("tools") or []) if isinstance(n, str) and self.registry.get(n)]
            tasks.append(PlanTask(id=tid, title=str(t.get("title") or f"任务 {i + 1}")[:60], objective=str(t.get("objective") or t.get("title") or "")[:600], depends_on=[str(d) for d in (t.get("depends_on") or []) if str(d) in ids or True], tools=tools, acceptance=str(t.get("acceptance") or "")[:300]))
        if not tasks:
            return None, "tasks 为空"
        valid_ids = {t.id for t in tasks}
        for t in tasks:
            t.depends_on = [d for d in t.depends_on if d in valid_ids and d != t.id]
        return Plan(title=str(obj.get("title") or goal[:40]), tasks=tasks, assumptions=[str(a) for a in (obj.get("assumptions") or [])][:5], mode="llm"), ""
