"""演示数据（fixture）：无扶摇 API Key 时用于演示与测试。

重要：所有返回都标注 source = "fuyao-fixture(演示数据)"，message 含 "fixture"，
数值由标的代码种子的伪随机过程生成，**不是真实行情**，报告中会据此标注。
"""
from __future__ import annotations

import hashlib
import math
import random
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

TICKERS: list[dict] = [
    {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台", "exchange": "SH", "asset_type": "a-share", "base": 1480.0},
    {"thscode": "000858.SZ", "ticker": "000858", "name": "五粮液", "exchange": "SZ", "asset_type": "a-share", "base": 128.0},
    {"thscode": "000568.SZ", "ticker": "000568", "name": "泸州老窖", "exchange": "SZ", "asset_type": "a-share", "base": 126.0},
    {"thscode": "600809.SH", "ticker": "600809", "name": "山西汾酒", "exchange": "SH", "asset_type": "a-share", "base": 190.0},
    {"thscode": "002304.SZ", "ticker": "002304", "name": "洋河股份", "exchange": "SZ", "asset_type": "a-share", "base": 72.0},
    {"thscode": "300750.SZ", "ticker": "300750", "name": "宁德时代", "exchange": "SZ", "asset_type": "a-share", "base": 262.0},
    {"thscode": "002594.SZ", "ticker": "002594", "name": "比亚迪", "exchange": "SZ", "asset_type": "a-share", "base": 318.0},
    {"thscode": "601318.SH", "ticker": "601318", "name": "中国平安", "exchange": "SH", "asset_type": "a-share", "base": 54.0},
    {"thscode": "600036.SH", "ticker": "600036", "name": "招商银行", "exchange": "SH", "asset_type": "a-share", "base": 41.0},
    {"thscode": "300033.SZ", "ticker": "300033", "name": "同花顺", "exchange": "SZ", "asset_type": "a-share", "base": 290.0},
    {"thscode": "601012.SH", "ticker": "601012", "name": "隆基绿能", "exchange": "SH", "asset_type": "a-share", "base": 16.5},
    {"thscode": "300274.SZ", "ticker": "300274", "name": "阳光电源", "exchange": "SZ", "asset_type": "a-share", "base": 76.0},
    {"thscode": "000001.SH", "ticker": "000001", "name": "上证指数", "exchange": "SH", "asset_type": "a-share-index", "base": 3300.0},
    {"thscode": "399001.SZ", "ticker": "399001", "name": "深证成指", "exchange": "SZ", "asset_type": "a-share-index", "base": 10500.0},
    {"thscode": "000300.SH", "ticker": "000300", "name": "沪深300", "exchange": "SH", "asset_type": "a-share-index", "base": 3900.0},
    {"thscode": "399006.SZ", "ticker": "399006", "name": "创业板指", "exchange": "SZ", "asset_type": "a-share-index", "base": 2100.0},
    {"thscode": "885613.TI", "ticker": "885613", "name": "白酒概念(演示)", "exchange": None, "asset_type": "a-share-index", "base": 2800.0},
    {"thscode": "881138.TI", "ticker": "881138", "name": "新能源电池(演示)", "exchange": None, "asset_type": "a-share-index", "base": 1900.0},
]
_BY_CODE = {t["thscode"]: t for t in TICKERS}

INDEX_CATALOG = {
    "cn_concept": [
        {"thscode": "885613.TI", "name": "白酒概念(演示)"},
        {"thscode": "881138.TI", "name": "新能源电池(演示)"},
        {"thscode": "886042.TI", "name": "光伏概念(演示)"},
    ],
    "industry": [
        {"thscode": "881273.TI", "name": "白酒Ⅱ(演示)"},
        {"thscode": "881101.TI", "name": "银行(演示)"},
    ],
    "region": [{"thscode": "885900.TI", "name": "长三角(演示)"}],
    "tszs": [{"thscode": "885999.TI", "name": "核心资产(演示)"}],
}
CONSTITUENTS = {
    "885613.TI": ["600519.SH", "000858.SZ", "000568.SZ", "600809.SH", "002304.SZ"],
    "881273.TI": ["600519.SH", "000858.SZ", "000568.SZ", "600809.SH", "002304.SZ"],
    "881138.TI": ["300750.SZ", "002594.SZ"],
    "886042.TI": ["601012.SH", "300274.SZ"],
    "000300.SH": ["600519.SH", "000858.SZ", "300750.SZ", "002594.SZ", "601318.SH", "600036.SH"],
    "881101.TI": ["600036.SH", "601318.SH"],
}


def _seed(*parts) -> random.Random:
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:12], 16))


def _day_ms(d: datetime) -> int:
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ).timestamp() * 1000)


def envelope(data: dict) -> dict:
    return {"code": 0, "message": "success (fixture 演示数据)", "request_id": "fixture-" + hashlib.md5(str(data)[:200].encode()).hexdigest()[:12], "data": data}


def trading_days() -> list[datetime]:
    today = datetime.now(TZ)
    days = []
    d = today - timedelta(days=366)
    while d <= today:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def search(q: str, asset_type: str | None, limit: int) -> dict:
    ql = q.strip().lower()
    out = []
    for t in TICKERS:
        if ql in t["thscode"].lower() or ql in t["ticker"] or ql in t["name"].lower():
            if asset_type and t["asset_type"] not in asset_type.split(","):
                continue
            out.append({k: t[k] for k in ("thscode", "ticker", "name", "exchange", "asset_type")} | {"currency": "CNY", "list_date": None, "end_date": None, "last_trade_date": None, "last_delivery_date": None})
    return envelope({"timestamp": int(time.time() * 1000), "item": out[:limit]})


def _last_close(code: str) -> float:
    bars = kline(code, 40)["data"]["item"]
    return bars[-1]["close_price"] if bars else _BY_CODE.get(code, {}).get("base", 10.0)


def snapshot(thscodes: list[str]) -> dict:
    items = []
    for code in thscodes:
        meta = _BY_CODE.get(code)
        if not meta:
            continue  # 未知代码：不生成占位项（与真实接口一致）
        r = _seed(code, "snap", datetime.now(TZ).strftime("%Y%m%d%H"))
        prev = _last_close(code)
        chg = r.uniform(-0.03, 0.03)
        last = round(prev * (1 + chg), 2)
        items.append(
            {
                "thscode": code,
                "ticker": meta["ticker"],
                "last_price": last,
                "price_change": round(last - prev, 2),
                "price_change_ratio_pct": round(chg * 100, 4),
                "open_price": round(prev * (1 + r.uniform(-0.01, 0.01)), 2),
                "high_price": round(max(last, prev) * (1 + r.uniform(0, 0.012)), 2),
                "low_price": round(min(last, prev) * (1 - r.uniform(0, 0.012)), 2),
                "prev_price": prev,
                "volume": int(r.uniform(1e6, 5e7)),
                "turnover": int(r.uniform(1e8, 8e9)),
            }
        )
    return envelope({"timestamp": int(time.time() * 1000), "total": len(items), "item": items})


def kline(code: str, n_days: int = 60, start_ms: int | None = None, end_ms: int | None = None) -> dict:
    meta = _BY_CODE.get(code)
    if not meta:
        return {"code": 3001, "message": "标的不存在 (fixture)", "request_id": "fixture-3001", "data": None}
    days = trading_days()
    r = _seed(code, "kline")
    price = meta["base"] * 0.9
    bars = []
    vol_scale = 3e6 if meta["asset_type"] == "a-share" else 2e8
    for d in days:
        drift = 0.0003
        ret = r.gauss(drift, 0.018)
        o = price
        c = max(0.5, price * (1 + ret))
        h = max(o, c) * (1 + abs(r.gauss(0, 0.006)))
        l = min(o, c) * (1 - abs(r.gauss(0, 0.006)))
        v = int(abs(r.gauss(vol_scale, vol_scale * 0.35)))
        bars.append(
            {
                "date_ms": _day_ms(d),
                "open_price": round(o, 2),
                "high_price": round(h, 2),
                "low_price": round(l, 2),
                "close_price": round(c, 2),
                "volume": v,
                "turnover": int(v * (o + c) / 2),
            }
        )
        price = c
    if start_ms is not None and end_ms is not None:
        bars = [b for b in bars if start_ms <= b["date_ms"] <= end_ms]
    else:
        bars = bars[-n_days:]
    return envelope({"timestamp": bars[-1]["date_ms"] if bars else None, "item": bars})


def valuations(thscodes: list[str]) -> dict:
    items = []
    for code in thscodes:
        meta = _BY_CODE.get(code)
        if not meta or meta["asset_type"] != "a-share":
            continue
        r = _seed(code, "val")
        pe = round(r.uniform(8, 45), 4)
        items.append(
            {
                "thscode": code,
                "ticker": meta["ticker"],
                "name": meta["name"],
                "pe_ttm": pe,
                "pe_mrq": round(pe * r.uniform(0.9, 1.1), 4),
                "pb_mrq": round(r.uniform(0.8, 9.5), 4),
                "ps_ttm": round(r.uniform(0.6, 12), 4),
                "pcf_ttm": round(r.uniform(5, 40), 4) if r.random() > 0.15 else None,
            }
        )
    return envelope({"timestamp": int(time.time() * 1000), "total": len(items), "item": items})


_INDICATORS = {
    "growth": ["total_assets_growth_ratio", "net_profit_yoy_growth_ratio", "operating_income_yoy_growth_ratio", "operating_profit_yoy_growth_ratio"],
    "profitability": ["sale_gross_margin", "sale_net_interest_ratio", "total_assets_net_ratio", "index_deduct_weighted_avg_roe", "index_weighted_avg_roe"],
    "solvency": ["current_ratio", "quick_ratio", "assets_debt_ratio", "cash_ratio", "earned_interest_multiple"],
    "operation": ["long_term_debt_equity_ratio", "total_assets_turnover_ratio", "inventory_turnover_ratio", "current_assets_turnover_ratio", "receive_account_turnover_ratio"],
    "cash-flow": ["cash_operating_index", "operating_cash_flow_net_divide_income", "net_profit_cash_content", "operating_cash_net_yoy_growth_ratio", "cash_meet_invest_ratio"],
}


def financial_indicators(code: str, report: str) -> dict:
    meta = _BY_CODE.get(code)
    if not meta or meta["asset_type"] != "a-share":
        return {"code": 3001, "message": "标的不存在 (fixture)", "request_id": "fixture-3001", "data": None}
    r = _seed(code, "fin", report)
    abilities = []
    for ab, ids in _INDICATORS.items():
        inds = []
        for i in ids:
            if "ratio" in i and "turnover" not in i and "growth" not in i:
                val = r.uniform(5, 95)
            elif "growth" in i:
                val = r.uniform(-30, 60)
            elif "margin" in i or "roe" in i or "net_ratio" in i:
                val = r.uniform(3, 60)
            elif "turnover" in i:
                val = r.uniform(0.2, 8)
            else:
                val = r.uniform(0.5, 30)
            inds.append({"index_id": i, "value": None if r.random() < 0.06 else f"{val:.8f}"})
        abilities.append({"ability": ab, "indicators": inds})
    return {"code": 0, "message": "success (fixture 演示数据)", "request_id": "fixture-fin", "data": {"thscode": code, "report": report, "abilities": abilities}}


def income_statements(code: str, period: str, limit: int) -> dict:
    meta = _BY_CODE.get(code)
    if not meta or meta["asset_type"] != "a-share":
        return {"code": 3001, "message": "标的不存在 (fixture)", "request_id": "fixture-3001", "data": None}
    r = _seed(code, "inc", period)
    base_rev = meta["base"] * 1e8 * r.uniform(0.5, 3)
    items = []
    year = datetime.now(TZ).year - 1
    for k in range(limit):
        fy = year - (k if period == "annual" else k // 4)
        q = 4 if period == "annual" else 4 - (k % 4)
        rev = base_rev * (1 + r.uniform(-0.1, 0.2)) ** (-k)
        np_ = rev * r.uniform(0.08, 0.5)
        pe_end = datetime(fy, 3 * q, 28, tzinfo=TZ)
        items.append(
            {
                "thscode": code,
                "ticker": meta["ticker"],
                "period": period,
                "fiscal_year": fy,
                "fiscal_period": "FY" if period == "annual" else f"Q{q}",
                "report_date_ms": _day_ms(pe_end + timedelta(days=60)),
                "period_end_ms": _day_ms(pe_end),
                "currency": "CNY",
                "operating_income": round(rev, 2),
                "operating_costs": round(rev * r.uniform(0.3, 0.7), 2),
                "operating_expenses": round(rev * r.uniform(0.5, 0.85), 2),
                "sales_fee": round(rev * r.uniform(0.02, 0.15), 2),
                "manage_fee": round(rev * r.uniform(0.02, 0.1), 2),
                "research_and_development_expenses": round(rev * r.uniform(0.0, 0.08), 2),
                "operating_profit": round(np_ * 1.25, 2),
                "interest_expenses": round(rev * r.uniform(0, 0.01), 2),
                "profit_total": round(np_ * 1.24, 2),
                "income_tax_expense": round(np_ * 0.24, 2),
                "net_profit": round(np_, 2),
                "parent_holder_net_profit": round(np_ * r.uniform(0.9, 1.0), 2),
                "basic_eps": round(np_ / (meta["base"] * 1e7), 4),
            }
        )
    return envelope({"timestamp": items[0]["period_end_ms"] if items else None, "item": items})


def limit_up_pool(size: int) -> dict:
    r = _seed("limitup", datetime.now(TZ).strftime("%Y%m%d"))
    pool = [t for t in TICKERS if t["asset_type"] == "a-share"]
    items = []
    for i, t in enumerate(r.sample(pool, min(size, len(pool), 6))):
        cnt = r.choice([1, 1, 1, 2, 3])
        items.append(
            {
                "thscode": t["thscode"],
                "ticker": t["ticker"],
                "name": t["name"],
                "is_st": False,
                "is_new": False,
                "last_price": round(t["base"] * 1.1, 2),
                "price_change_ratio_pct": 10.0 if not t["ticker"].startswith("30") else 20.0,
                "limit_up_time": f"{r.randint(9, 14):02d}:{r.randint(0, 59):02d}",
                "limit_up_reason": r.choice(["业绩预增(演示)", "行业政策催化(演示)", None]),
                "continue_day_text": "首板" if cnt == 1 else f"{cnt}连板",
                "continue_day_cnt": cnt,
                "seal_money": int(r.uniform(2e7, 9e8)),
                "max_seal_money": int(r.uniform(9e8, 2e9)),
            }
        )
    return envelope({"timestamp": int(time.time() * 1000), "pagination": {"total": len(items), "pages": 1, "size": size, "page": 1}, "item": items})


def limit_up_statistics(date: str | None) -> dict:
    r = _seed("stat", date or datetime.now(TZ).strftime("%Y%m%d"))
    return envelope(
        {
            "timestamp": int(time.time() * 1000),
            "item": [
                {
                    "date": date or datetime.now(TZ).strftime("%Y-%m-%d"),
                    "limit_up_count": r.randint(30, 120),
                    "limit_down_count": r.randint(0, 25),
                    "seal_rate": round(r.uniform(0.55, 0.85), 4),
                    "break_count": r.randint(5, 40),
                    "continuous_2": r.randint(5, 30),
                    "continuous_3": r.randint(1, 12),
                    "continuous_4": r.randint(0, 6),
                    "continuous_5_plus": r.randint(0, 4),
                }
            ],
        }
    )


def hot_list(limit: int) -> dict:
    r = _seed("hot", datetime.now(TZ).strftime("%Y%m%d%H"))
    pool = [t for t in TICKERS if t["asset_type"] == "a-share"]
    r.shuffle(pool)
    items = []
    for i, t in enumerate(pool[:limit]):
        ch = r.randint(-20, 20)
        items.append({"thscode": t["thscode"], "ticker": t["ticker"], "name": t["name"], "rank": i + 1, "heat": str(r.randint(100000, 999999)), "rank_change": ch, "rank_trend": "up" if ch > 0 else "down" if ch < 0 else "flat"})
    return envelope({"timestamp": int(time.time() * 1000), "item": items})


def index_list(tag: str) -> dict:
    return envelope({"timestamp": int(time.time() * 1000), "item": INDEX_CATALOG.get(tag, [])})


def constituents(code: str) -> dict:
    codes = CONSTITUENTS.get(code)
    if codes is None:
        return {"code": 3002, "message": "数据未就绪 (fixture：该指数无成分股演示数据)", "request_id": "fixture-3002", "data": None}
    items = [{"thscode": c, "ticker": _BY_CODE[c]["ticker"], "name": _BY_CODE[c]["name"]} for c in codes if c in _BY_CODE]
    return envelope({"timestamp": int(time.time() * 1000), "item": items})


def calendar() -> dict:
    days = trading_days()
    return envelope({"timestamp": _day_ms(days[-1]), "item": [{"date_ms": _day_ms(d), "date": d.strftime("%Y%m%d")} for d in days]})
