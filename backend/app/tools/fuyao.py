"""扶摇金融数据 API 工具集。

- live 模式：真实调用 https://fuyao.aicubes.cn（X-api-key），按响应信封 code 分类错误。
- fixture 模式：无 Key 时使用本地演示数据（source 显式标注“演示数据”）。
每个工具都填写 unit_notes（单位 / 口径）与 data_timestamp（数据时点），供证据与报告引用。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from ..models import ToolResult
from . import fixtures
from .registry import ToolContext, ToolError, ToolRegistry, ToolSpec

log = logging.getLogger("xbuddy.fuyao")
TZ = timezone(timedelta(hours=8))
SOURCE_LIVE = "fuyao"
SOURCE_FIXTURE = "fuyao-fixture(演示数据)"

RETRYABLE_CODES = {4001, 5001, 5002, 5003}
MISSING_CODES = {3001, 3002, 3004}


def parse_date_ms(s: str, end_of_day: bool = False) -> int:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, fmt).replace(tzinfo=TZ)
            if end_of_day:
                d = d + timedelta(hours=23, minutes=59)
            return int(d.timestamp() * 1000)
        except ValueError:
            continue
    raise ToolError(f"日期格式错误：{s}（应为 YYYY-MM-DD）", retryable=False, code=1002)


def fmt_ts(ms: Optional[int]) -> str:
    if not ms:
        return "未知"
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def split_codes(s: str) -> list[str]:
    out = []
    for part in str(s).replace("，", ",").split(","):
        p = part.strip().upper()
        if p and p not in out:
            out.append(p)
    return out


class FuyaoBackend:
    """live / fixture 两种后端，统一返回响应信封 dict。"""

    def __init__(self, *, mode: str, base_url: str, api_key: Optional[str], timeout: float = 20.0):
        self.mode = mode
        self.source = SOURCE_LIVE if mode == "live" else SOURCE_FIXTURE
        self._http: Optional[httpx.AsyncClient] = None
        if mode == "live":
            self._http = httpx.AsyncClient(base_url=base_url, headers={"X-api-key": api_key or "", "User-Agent": "xbuddy/0.1"}, timeout=timeout)

    async def aclose(self):
        if self._http:
            await self._http.aclose()

    async def get(self, path: str, params: dict) -> dict:
        if self.mode != "live":
            return self._fixture(path, params)
        assert self._http is not None
        try:
            r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.TimeoutException as e:
            raise ToolError(f"扶摇请求超时：{e}", retryable=True, transport=True) from e
        except httpx.HTTPError as e:
            raise ToolError(f"扶摇网络错误：{e}", retryable=True, transport=True) from e
        if r.status_code == 429:
            raise ToolError("扶摇频率超限（HTTP 429）", retryable=True, code=4001, transport=True)
        if r.status_code >= 500:
            raise ToolError(f"扶摇服务端错误 HTTP {r.status_code}", retryable=True, code=5001, transport=True)
        try:
            body = r.json()
        except ValueError as e:
            raise ToolError(f"扶摇响应非 JSON（HTTP {r.status_code}）", retryable=True, transport=True) from e
        code = body.get("code", -1)
        rid = body.get("request_id")
        if code == 0:
            return body
        msg = body.get("message", "")
        if code in MISSING_CODES:
            raise ToolError(f"扶摇 code={code}：{msg}", retryable=False, code=code, request_id=rid, status="missing")
        if code in RETRYABLE_CODES:
            # 4001 限流 / 5001 服务错误是数据源整体状况；5003“数据源不可用”实测常见于单个端点或单个参数（如未披露的报告期），不连坐其它端点
            raise ToolError(f"扶摇 code={code}：{msg}", retryable=True, code=code, request_id=rid, transport=code in (4001, 5001))
        if code in (2001, 2003):
            raise ToolError(f"扶摇鉴权/权限错误 code={code}：{msg}（请检查 FUYAO_API_KEY）", retryable=False, code=code, request_id=rid)
        raise ToolError(f"扶摇参数/业务错误 code={code}：{msg}", retryable=False, code=code, request_id=rid)

    # ---------- fixture 分派 ----------
    def _fixture(self, path: str, params: dict) -> dict:
        p = path
        if p == "/api/meta/tickers/search":
            return fixtures.search(params.get("q", ""), params.get("asset_type"), int(params.get("limit") or 10))
        if p == "/api/a-share/calendar/trading-days":
            return fixtures.calendar()
        if p in ("/api/a-share/prices/snapshot", "/api/a-share-index/prices/snapshot"):
            return fixtures.snapshot(split_codes(params.get("thscodes", "")))
        if p in ("/api/a-share/prices/historical", "/api/a-share-index/prices/historical"):
            return fixtures.kline(params["thscode"], start_ms=int(params["start"]), end_ms=int(params["end"]))
        if p == "/api/a-share/valuations/snapshot":
            return fixtures.valuations(split_codes(params.get("thscodes", "")))
        if p == "/api/a-share/financials/indicators":
            return fixtures.financial_indicators(params["thscode"], params["report"])
        if p in ("/api/a-share/financials/income-statements", "/api/a-share/financials/balance-sheets", "/api/a-share/financials/cash-flow-statements"):
            return fixtures.income_statements(params["thscode"], params.get("period", "annual"), int(params.get("limit") or 4))
        if p == "/api/a-share-index/catalog/ths-index-list":
            return fixtures.index_list(params.get("tag", "cn_concept"))
        if p == "/api/a-share-index/constituents/ths-stock-list":
            return fixtures.constituents(params["thscode"])
        if p == "/api/a-share/special-data/limit-up-pool":
            return fixtures.limit_up_pool(int(params.get("size") or 50))
        if p == "/api/a-share/special-data/limit-down-pool":
            e = fixtures.limit_up_pool(3)
            for it in e["data"]["item"]:
                it["price_change_ratio_pct"] = -10.0
                it["continue_day_text"] = "跌停"
            return e
        if p == "/api/a-share/special-data/limit-up-statistics":
            return fixtures.limit_up_statistics(params.get("date"))
        if p in ("/api/a-share/special-data/hot-stock-list", "/api/a-share/special-data/skyrocket-list"):
            return fixtures.hot_list(int(params.get("limit") or 30))
        return {"code": 1003, "message": f"fixture 未实现端点 {path}", "request_id": "fixture-1003", "data": None}


def _check(body: dict, source: str) -> dict:
    """fixture 路径也走统一错误分类。"""
    code = body.get("code", -1)
    if code == 0:
        return body
    msg = body.get("message", "")
    if code in MISSING_CODES:
        raise ToolError(f"code={code}：{msg}", retryable=False, code=code, request_id=body.get("request_id"), status="missing")
    raise ToolError(f"code={code}：{msg}", retryable=code in RETRYABLE_CODES, code=code, request_id=body.get("request_id"))


def register_fuyao_tools(registry: ToolRegistry, backend: FuyaoBackend) -> list[str]:
    src = backend.source
    names: list[str] = []

    def reg(spec: ToolSpec):
        registry.register(spec)
        names.append(spec.name)

    async def call(path: str, params: dict) -> dict:
        body = await backend.get(path, params)
        return _check(body, src)

    # ---- 标的检索 ----
    async def search_ticker(args: dict, ctx: ToolContext) -> ToolResult:
        body = await call("/api/meta/tickers/search", {"q": args["q"], "asset_type": args.get("asset_type"), "limit": min(int(args.get("limit") or 10), 50)})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing(f"未检索到与“{args['q']}”匹配的标的", source=src, request_id=body.get("request_id"))
        names_ = "、".join(f"{i['name']}({i['thscode']})" for i in items[:8])
        return ToolResult(ok=True, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="标的代码为完整 thscode（含交易所后缀）", summary=f"检索“{args['q']}”得到 {len(items)} 个标的：{names_}")

    reg(ToolSpec(name="search_ticker", description="按名称 / 代码 / thscode 检索 A 股股票、指数、同花顺板块等标的，返回完整 thscode（如 600519.SH）。研究任何标的前应先用它消歧。", parameters={"type": "object", "properties": {"q": {"type": "string", "description": "关键词：中文名、6 位代码或 thscode"}, "asset_type": {"type": "string", "description": "可选过滤：a-share | a-share-index | fund-etf 等，可逗号分隔"}, "limit": {"type": "integer", "description": "返回上限，最大 50", "default": 10}}, "required": ["q"]}, handler=search_ticker, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=3600, tags=("meta",)))

    # ---- 交易日历 ----
    async def get_trading_days(args: dict, ctx: ToolContext) -> ToolResult:
        body = await call("/api/a-share/calendar/trading-days", {})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing("交易日历为空", source=src)
        return ToolResult(ok=True, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="近一年 A 股交易日，Asia/Shanghai，date 为 yyyyMMdd", summary=f"近一年交易日 {len(items)} 个，最近交易日 {items[-1]['date']}，最早 {items[0]['date']}")

    reg(ToolSpec(name="get_trading_days", description="获取 A 股近一年交易日序列（用于确定最近交易日、计算日期区间）。", parameters={"type": "object", "properties": {}}, handler=get_trading_days, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=6 * 3600, tags=("calendar",)))

    # ---- 行情快照 ----
    def _snapshot_summary(items: list[dict], unit: str = "元") -> str:
        parts = []
        for it in items[:6]:
            parts.append(f"{it['thscode']} 最新 {it.get('last_price')} {unit}，涨跌幅 {it.get('price_change_ratio_pct')}%，成交额 {round((it.get('turnover') or 0) / 1e8, 2)} 亿元")
        return "；".join(parts)

    async def get_quote_snapshot(args: dict, ctx: ToolContext) -> ToolResult:
        codes = split_codes(args["thscodes"])
        body = await call("/api/a-share/prices/snapshot", {"thscodes": ",".join(codes)})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing(f"未返回 {codes} 的行情快照（标的可能不存在或停牌）", source=src, request_id=body.get("request_id"))
        missing = [c for c in codes if c not in {i["thscode"] for i in items}]
        status = "partial" if missing else "ok"
        return ToolResult(ok=True, status=status, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="价格单位元(CNY)；成交量单位股；成交额单位元；涨跌幅为百分比数值；相对前收盘价", summary=_snapshot_summary(items) + (f"；缺失：{missing}" if missing else "") + f"；数据时点 {fmt_ts(body['data'].get('timestamp'))}")

    reg(ToolSpec(name="get_quote_snapshot", description="批量获取 A 股股票最新行情快照（最新价、涨跌幅、开高低、前收、成交量/额）。thscodes 逗号分隔。", parameters={"type": "object", "properties": {"thscodes": {"type": "string", "description": "逗号分隔的股票 thscode，如 600519.SH,000858.SZ"}}, "required": ["thscodes"]}, handler=get_quote_snapshot, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=60, tags=("prices",)))

    async def get_index_snapshot(args: dict, ctx: ToolContext) -> ToolResult:
        codes = split_codes(args["thscodes"])
        body = await call("/api/a-share-index/prices/snapshot", {"thscodes": ",".join(codes)})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing(f"未返回 {codes} 的指数快照", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="指数点位（点）；涨跌幅为百分比数值；成交额单位元", summary=_snapshot_summary(items, "点") + f"；数据时点 {fmt_ts(body['data'].get('timestamp'))}")

    reg(ToolSpec(name="get_index_snapshot", description="批量获取指数 / 同花顺板块最新行情快照（如 000001.SH 上证指数、000300.SH 沪深300、399006.SZ 创业板指、8xxxxx.TI 同花顺板块）。", parameters={"type": "object", "properties": {"thscodes": {"type": "string", "description": "逗号分隔的指数 thscode"}}, "required": ["thscodes"]}, handler=get_index_snapshot, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=60, tags=("prices", "index")))

    # ---- 历史 K 线 ----
    def _kline_summary(code: str, bars: list[dict], adjust: Optional[str]) -> str:
        if not bars:
            return "无 K 线"
        first, last = bars[0], bars[-1]
        chg = (last["close_price"] / first["close_price"] - 1) * 100 if first["close_price"] else 0
        hi = max(b["high_price"] for b in bars)
        lo = min(b["low_price"] for b in bars)
        return f"{code} {fmt_ts(first['date_ms'])[:10]}~{fmt_ts(last['date_ms'])[:10]} 共 {len(bars)} 根日K" + (f"（{adjust} 复权）" if adjust else "") + f"；首收 {first['close_price']} → 末收 {last['close_price']}，区间涨跌 {chg:.2f}%；区间最高 {hi} 最低 {lo}"

    async def get_kline(args: dict, ctx: ToolContext) -> ToolResult:
        code = args["thscode"].strip().upper()
        if "," in code:
            raise ToolError("thscode 不接受逗号，每次仅一个标的", retryable=False, code=1002)
        start, end = parse_date_ms(args["start_date"]), parse_date_ms(args["end_date"], end_of_day=True)
        adjust = args.get("adjust") or "forward"
        body = await call("/api/a-share/prices/historical", {"thscode": code, "interval": "1d", "start": start, "end": end, "adjust": adjust})
        bars = body["data"].get("item", [])
        if not bars:
            return ToolResult.missing(f"{code} 在 {args['start_date']}~{args['end_date']} 无 K 线数据", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data=body["data"] | {"thscode": code, "adjust": adjust}, source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes=f"日线；价格单位元；成交量股；成交额元；复权方式 {adjust}", summary=_kline_summary(code, bars, adjust))

    reg(ToolSpec(name="get_kline", description="获取单只 A 股股票的历史日 K 线（开高低收、量、额），支持前复权/后复权/不复权。一次一个 thscode，区间 ≤10 年。", parameters={"type": "object", "properties": {"thscode": {"type": "string"}, "start_date": {"type": "string", "description": "YYYY-MM-DD"}, "end_date": {"type": "string", "description": "YYYY-MM-DD"}, "adjust": {"type": "string", "enum": ["forward", "backward", "none"], "default": "forward"}}, "required": ["thscode", "start_date", "end_date"]}, handler=get_kline, risk="L1", source="fuyao", cacheable=True, cache_ttl_s=3600, est_cost=2, tags=("prices",)))

    async def get_index_kline(args: dict, ctx: ToolContext) -> ToolResult:
        code = args["thscode"].strip().upper()
        start, end = parse_date_ms(args["start_date"]), parse_date_ms(args["end_date"], end_of_day=True)
        body = await call("/api/a-share-index/prices/historical", {"thscode": code, "interval": "1d", "start": start, "end": end})
        bars = body["data"].get("item", [])
        if not bars:
            return ToolResult.missing(f"指数 {code} 在区间内无 K 线数据", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data=body["data"] | {"thscode": code}, source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="指数日线；点位；成交额元；无复权", summary=_kline_summary(code, bars, None))

    reg(ToolSpec(name="get_index_kline", description="获取单只指数 / 同花顺板块的历史日 K 线（近 5 年内）。", parameters={"type": "object", "properties": {"thscode": {"type": "string"}, "start_date": {"type": "string", "description": "YYYY-MM-DD"}, "end_date": {"type": "string", "description": "YYYY-MM-DD"}}, "required": ["thscode", "start_date", "end_date"]}, handler=get_index_kline, risk="L1", source="fuyao", cacheable=True, cache_ttl_s=3600, est_cost=2, tags=("prices", "index")))

    # ---- 估值 ----
    async def get_valuation_snapshot(args: dict, ctx: ToolContext) -> ToolResult:
        codes = split_codes(args["thscodes"])
        body = await call("/api/a-share/valuations/snapshot", {"thscodes": ",".join(codes[:100])})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing(f"未返回 {codes} 的估值数据", source=src, request_id=body.get("request_id"))
        missing = [c for c in codes if c not in {i["thscode"] for i in items}]
        parts = [f"{i.get('name', i['thscode'])} PE(TTM) {i.get('pe_ttm')} / PB(MRQ) {i.get('pb_mrq')} / PS(TTM) {i.get('ps_ttm')}" for i in items[:8]]
        return ToolResult(ok=True, status="partial" if missing else "ok", data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="估值倍数（倍）；TTM=滚动 12 个月，MRQ=最近季报；负值/null 原样保留，null 表示上游未提供（如亏损时 PE 不适用）", summary="；".join(parts) + (f"；缺失 {missing}" if missing else "") + f"；时点 {fmt_ts(body['data'].get('timestamp'))}")

    reg(ToolSpec(name="get_valuation_snapshot", description="批量获取 A 股最新估值快照：PE(TTM/MRQ)、PB(MRQ)、PS(TTM)、PCF(TTM)。仅最新值，无历史分位。", parameters={"type": "object", "properties": {"thscodes": {"type": "string", "description": "逗号分隔股票 thscode，最多 100 个"}}, "required": ["thscodes"]}, handler=get_valuation_snapshot, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=600, tags=("valuation",)))

    # ---- 财务指标 ----
    async def get_financial_indicators(args: dict, ctx: ToolContext) -> ToolResult:
        code = args["thscode"].strip().upper()
        report = args["report"].strip()
        body = await call("/api/a-share/financials/indicators", {"thscode": code, "report": report})
        data = body["data"] or {}
        flat: dict[str, Any] = {}
        nulls = []
        for ab in data.get("abilities", []):
            for ind in ab.get("indicators", []):
                flat[ind["index_id"]] = ind.get("value")
                if ind.get("value") is None:
                    nulls.append(ind["index_id"])
        if not flat:
            return ToolResult.missing(f"{code} {report} 无财务指标", source=src, request_id=body.get("request_id"))
        key = {"sale_gross_margin": "毛利率", "sale_net_interest_ratio": "净利率", "index_weighted_avg_roe": "ROE", "assets_debt_ratio": "资产负债率", "operating_income_yoy_growth_ratio": "营收同比", "net_profit_yoy_growth_ratio": "净利润同比", "net_profit_cash_content": "净利润现金含量"}
        parts = [f"{v} {_fmt_num(flat.get(k))}" for k, v in key.items() if flat.get(k) is not None]
        return ToolResult(ok=True, status="partial" if nulls else "ok", data=data | {"flat": flat}, source=src, data_timestamp=None, request_id=body.get("request_id"), unit_notes="报告期 " + report + "；百分比类指标为百分数值（如 89.12 表示 89.12%），周转率为次，倍数为倍；value 为数据源原始字符串", summary=f"{code} {report} 财务指标：" + "，".join(parts) + (f"；缺失指标 {len(nulls)} 个" if nulls else ""))

    reg(ToolSpec(name="get_financial_indicators", description="获取单只 A 股指定报告期的五类财务指标（成长、盈利、偿债、营运、现金流）。report 格式 yyyy-1/2/3/4（1 一季报、2 中报、3 三季报、4 年报）。", parameters={"type": "object", "properties": {"thscode": {"type": "string"}, "report": {"type": "string", "description": "如 2025-2"}}, "required": ["thscode", "report"]}, handler=get_financial_indicators, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=86400, tags=("financials",)))

    # ---- 三大报表 ----
    def make_statement_tool(name: str, path: str, zh: str, key_fields: dict[str, str]):
        async def handler(args: dict, ctx: ToolContext) -> ToolResult:
            code = args["thscode"].strip().upper()
            period = args.get("period") or "annual"
            limit = max(1, min(int(args.get("limit") or 4), 20))
            body = await call(path, {"thscode": code, "period": period, "limit": limit})
            items = body["data"].get("item", [])
            if not items:
                return ToolResult.missing(f"{code} 无{zh}数据", source=src, request_id=body.get("request_id"))
            parts = []
            for it in items[:4]:
                kv = "，".join(f"{v} {_fmt_money(it.get(k))}" for k, v in key_fields.items() if it.get(k) is not None)
                parts.append(f"{it.get('fiscal_year')}{it.get('fiscal_period')}：{kv}")
            return ToolResult(ok=True, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes=f"合并{zh}；金额单位元(CNY)，basic_eps 单位元/股；null 表示该期未披露；period={period}", summary=f"{code} 最近 {len(items)} 期{zh}：" + "；".join(parts))

        reg(ToolSpec(name=name, description=f"获取单只 A 股合并{zh}多期序列（最近 N 期，annual 年报 / quarterly 季报）。", parameters={"type": "object", "properties": {"thscode": {"type": "string"}, "period": {"type": "string", "enum": ["annual", "quarterly"], "default": "annual"}, "limit": {"type": "integer", "default": 4, "description": "1-20"}}, "required": ["thscode"]}, handler=handler, risk="L1", source="fuyao", cacheable=True, cache_ttl_s=86400, tags=("financials",)))

    make_statement_tool("get_income_statements", "/api/a-share/financials/income-statements", "利润表", {"operating_income": "营收", "net_profit": "净利润", "parent_holder_net_profit": "归母净利润", "basic_eps": "EPS"})
    # 字段名以扶摇 live 实际返回为准（2026-10-25 实测）：资产负债表 assets_total / total_debt / holder_equity_total / cash，
    # 现金流量表 act_cash_flow_net / invest_cash_flow_net / financing_cash_flow_net
    make_statement_tool("get_balance_sheets", "/api/a-share/financials/balance-sheets", "资产负债表", {"assets_total": "总资产", "total_debt": "总负债", "holder_equity_total": "所有者权益", "cash": "货币资金"})
    make_statement_tool("get_cash_flow_statements", "/api/a-share/financials/cash-flow-statements", "现金流量表", {"act_cash_flow_net": "经营现金流净额", "invest_cash_flow_net": "投资现金流净额", "financing_cash_flow_net": "筹资现金流净额"})

    # ---- 指数 / 板块 ----
    async def find_ths_index(args: dict, ctx: ToolContext) -> ToolResult:
        tag = args.get("tag") or "cn_concept"
        kw = (args.get("keyword") or "").strip()
        body = await call("/api/a-share-index/catalog/ths-index-list", {"tag": tag})
        items = body["data"].get("item", [])
        hits = [i for i in items if kw.lower() in i.get("name", "").lower()] if kw else items[:30]
        if not hits:
            return ToolResult.missing(f"在 {tag} 标签下未找到名称含“{kw}”的同花顺指数（共 {len(items)} 个）", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data={"timestamp": body["data"].get("timestamp"), "tag": tag, "total": len(items), "item": hits[:30]}, source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="同花顺指数目录；tag ∈ cn_concept 概念 / industry 行业 / region 区域 / tszs 特色", summary=f"{tag} 下匹配“{kw}”的指数 {len(hits)} 个：" + "、".join(f"{i['name']}({i['thscode']})" for i in hits[:8]))

    reg(ToolSpec(name="find_ths_index", description="在同花顺指数目录中按名称关键词查找概念 / 行业 / 区域 / 特色指数，得到板块 thscode（如 8xxxxx.TI），用于板块对比。", parameters={"type": "object", "properties": {"keyword": {"type": "string", "description": "名称关键词，如 白酒、光伏"}, "tag": {"type": "string", "enum": ["cn_concept", "industry", "region", "tszs"], "default": "cn_concept"}}, "required": ["keyword"]}, handler=find_ths_index, risk="L1", source="fuyao", cacheable=True, cache_ttl_s=86400, est_cost=2, tags=("index",)))

    async def get_index_constituents(args: dict, ctx: ToolContext) -> ToolResult:
        code = args["thscode"].strip().upper()
        body = await call("/api/a-share-index/constituents/ths-stock-list", {"thscode": code})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing(f"指数 {code} 无成分股数据", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data=body["data"] | {"thscode": code}, source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="当前成分股清单（无权重）", summary=f"指数 {code} 成分股 {len(items)} 只：" + "、".join(f"{i['name']}({i['thscode']})" for i in items[:10]) + ("…" if len(items) > 10 else ""))

    reg(ToolSpec(name="get_index_constituents", description="获取指数 / 同花顺板块的当前成分股清单（支持 000300.SH 沪深300 等标准指数与 8xxxxx.TI 板块）。", parameters={"type": "object", "properties": {"thscode": {"type": "string"}}, "required": ["thscode"]}, handler=get_index_constituents, risk="L1", source="fuyao", cacheable=True, cache_ttl_s=86400, est_cost=2, tags=("index",)))

    # ---- 涨跌停 / 情绪 ----
    async def get_limit_up_pool(args: dict, ctx: ToolContext) -> ToolResult:
        params = {"size": min(int(args.get("size") or 50), 200), "page": 1, "sort_field": "continue_day_cnt", "sort_dir": "desc"}
        if args.get("date"):
            params["date_ms"] = parse_date_ms(args["date"])
        body = await call("/api/a-share/special-data/limit-up-pool", params)
        items = body["data"].get("item", [])
        used_date = args.get("date") or "当日"
        if not items and not args.get("date"):
            # 非交易日 / 盘前当日池为空：回退到最近一个交易日，并把所用日期显式写进口径，不冒充当日
            cal = await call("/api/a-share/calendar/trading-days", {})
            days = cal["data"].get("item", [])
            if days:
                d = days[-1]["date"]
                used_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                params["date_ms"] = parse_date_ms(used_date)
                body = await call("/api/a-share/special-data/limit-up-pool", params)
                items = body["data"].get("item", [])
        total = (body["data"].get("pagination") or {}).get("total", len(items))
        if not items:
            return ToolResult.missing(f"涨停池为空（{used_date}，非交易日或数据未就绪）", source=src, request_id=body.get("request_id"))
        top = "、".join(f"{i['name']}({i.get('continue_day_text')})" for i in items[:8])
        return ToolResult(ok=True, data=body["data"] | {"trade_date": used_date}, source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes=f"交易日 {used_date}；封单额单位元；涨跌幅百分比；连板计数为交易日", summary=f"{used_date} 涨停池共 {total} 只，连板居前：{top}；时点 {fmt_ts(body['data'].get('timestamp'))}")

    reg(ToolSpec(name="get_limit_up_pool", description="获取某交易日 A 股涨停 / 连板股票池（涨停时间、原因、连板数、封单额）。", parameters={"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，省略为当日"}, "size": {"type": "integer", "default": 50}}}, handler=get_limit_up_pool, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=300, tags=("sentiment",)))

    async def get_limit_down_pool(args: dict, ctx: ToolContext) -> ToolResult:
        params = {"size": min(int(args.get("size") or 50), 200), "page": 1}
        if args.get("date"):
            params["date_ms"] = parse_date_ms(args["date"])
        body = await call("/api/a-share/special-data/limit-down-pool", params)
        items = body["data"].get("item", [])
        if not items:
            return ToolResult(ok=True, status="ok", data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="跌停池", summary="跌停池为空（0 只）")
        return ToolResult(ok=True, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="跌停池；涨跌幅百分比", summary=f"跌停池 {len(items)} 只：" + "、".join(i["name"] for i in items[:8]))

    reg(ToolSpec(name="get_limit_down_pool", description="获取某交易日 A 股跌停股票池。", parameters={"type": "object", "properties": {"date": {"type": "string"}, "size": {"type": "integer", "default": 50}}}, handler=get_limit_down_pool, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=300, tags=("sentiment",)))

    async def get_limit_up_statistics(args: dict, ctx: ToolContext) -> ToolResult:
        body = await call("/api/a-share/special-data/limit-up-statistics", {"date": args.get("date")})
        data = body["data"] or {}
        items = data.get("item") or ([data] if data and "item" not in data else [])
        if not items:
            return ToolResult.missing("涨停统计无数据", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data=data, source=src, data_timestamp=data.get("timestamp"), request_id=body.get("request_id"), unit_notes="封板率等比例为小数；家数为只", summary=f"涨停市场统计：{_short_json(items[0])}")

    reg(ToolSpec(name="get_limit_up_statistics", description="获取涨停市场汇总：封板率、炸板家数、连板档位家数（市场情绪指标）。", parameters={"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，省略为最近交易日"}}}, handler=get_limit_up_statistics, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=300, tags=("sentiment",)))

    async def get_hot_stock_list(args: dict, ctx: ToolContext) -> ToolResult:
        body = await call("/api/a-share/special-data/hot-stock-list", {"period": args.get("period") or "day", "limit": min(int(args.get("limit") or 30), 100)})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing("热股榜为空", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="同花顺热度排名；heat 为上游原始字符串；rank_change 正数为上升", summary="热股榜前列：" + "、".join(f"{i['rank']}.{i['name']}" for i in items[:10]) + f"；时点 {fmt_ts(body['data'].get('timestamp'))}")

    reg(ToolSpec(name="get_hot_stock_list", description="获取同花顺 A 股热股榜（day=24 小时榜 / hour=小时榜），反映关注度而非基本面。", parameters={"type": "object", "properties": {"period": {"type": "string", "enum": ["day", "hour"], "default": "day"}, "limit": {"type": "integer", "default": 30}}}, handler=get_hot_stock_list, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=300, tags=("sentiment",)))

    async def get_skyrocket_list(args: dict, ctx: ToolContext) -> ToolResult:
        body = await call("/api/a-share/special-data/skyrocket-list", {"period": args.get("period") or "day", "limit": min(int(args.get("limit") or 30), 100)})
        items = body["data"].get("item", [])
        if not items:
            return ToolResult.missing("飙升榜为空", source=src, request_id=body.get("request_id"))
        return ToolResult(ok=True, data=body["data"], source=src, data_timestamp=body["data"].get("timestamp"), request_id=body.get("request_id"), unit_notes="热度排名飙升榜", summary="飙升榜前列：" + "、".join(f"{i['rank']}.{i['name']}" for i in items[:10]))

    reg(ToolSpec(name="get_skyrocket_list", description="获取同花顺 A 股热度飙升榜。", parameters={"type": "object", "properties": {"period": {"type": "string", "enum": ["day", "hour"], "default": "day"}, "limit": {"type": "integer", "default": 30}}}, handler=get_skyrocket_list, risk="L0", source="fuyao", cacheable=True, cache_ttl_s=300, tags=("sentiment",)))

    return names


def _fmt_num(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "--"


def _fmt_money(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "--"
    if abs(f) >= 1e8:
        return f"{f / 1e8:.2f} 亿元"
    if abs(f) >= 1e4:
        return f"{f / 1e4:.2f} 万元"
    return f"{f:.2f}"


def _short_json(d: dict, n: int = 10) -> str:
    return "，".join(f"{k}={v}" for k, v in list(d.items())[:n])
