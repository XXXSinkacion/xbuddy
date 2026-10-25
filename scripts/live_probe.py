"""扶摇 live 冒烟：用真实 FUYAO_API_KEY 逐个调用已注册的扶摇工具，打印状态 / 时点 / 单位口径 / 摘要。

用法（.env 已填 FUYAO_API_KEY）：
    python scripts/live_probe.py            # 摘要
    python scripts/live_probe.py --raw      # 同时打印原始 JSON 前 500 字

不依赖模型密钥；使用临时数据目录，不污染 data/。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT)

from app.config import Settings  # noqa: E402
from app.factory import build_app_state  # noqa: E402
from app.harness.planner import date_days_ago, latest_report_period, today_str  # noqa: E402
from app.tools.registry import ToolContext  # noqa: E402


async def main(show_raw: bool) -> int:
    tmp = tempfile.mkdtemp(prefix="xbuddy-probe-")
    st = build_app_state(Settings(fuyao_mode="live", data_dir=tmp))
    if st.settings.fuyao_effective_mode != "live":
        print("FUYAO_API_KEY 未配置，无法做 live 冒烟")
        return 2
    h = st.harness
    thr = h.create_thread("live-probe")
    ctx = ToolContext(run_id=f"run_probe_{int(time.time())}", task_id=None, settings=st.settings, evidence=h.evidence, emit=lambda t, p: None, working_memory={}, cache=h.cache, breaker=h.breaker, faults=h.faults, registry=h.registry, memory=h.memory, thread_id=thr["id"])
    report = latest_report_period()
    probes = [
        ("search_ticker", {"q": "贵州茅台"}),
        ("get_trading_days", {}),
        ("get_quote_snapshot", {"thscodes": "600519.SH,000858.SZ"}),
        ("get_index_snapshot", {"thscodes": "000001.SH,000300.SH"}),
        ("get_kline", {"thscode": "600519.SH", "start_date": date_days_ago(90), "end_date": today_str()}),
        ("get_index_kline", {"thscode": "000300.SH", "start_date": date_days_ago(30), "end_date": today_str()}),
        ("get_valuation_snapshot", {"thscodes": "600519.SH"}),
        ("get_financial_indicators", {"thscode": "600519.SH", "report": report[0]}),
        ("get_financial_indicators", {"thscode": "600519.SH", "report": report[1]}),
        ("get_income_statements", {"thscode": "600519.SH", "period": "annual", "limit": 4}),
        ("get_balance_sheets", {"thscode": "600519.SH", "limit": 2}),
        ("get_cash_flow_statements", {"thscode": "600519.SH", "limit": 2}),
        ("find_ths_index", {"keyword": "白酒"}),
        ("get_index_constituents", {"thscode": "885525.TI"}),
        ("get_limit_up_pool", {"size": 10}),
        ("get_limit_down_pool", {}),
        ("get_limit_up_statistics", {}),
        ("get_hot_stock_list", {"limit": 5}),
        ("get_skyrocket_list", {"limit": 5}),
        ("search_ticker", {"q": "不存在公司甲乙丙"}),
    ]
    fuyao_tools = {n for n in h.registry.names() if (h.registry.get(n).source == "fuyao")}
    missing = fuyao_tools - {p[0] for p in probes}
    if missing:
        print("未覆盖的扶摇工具：", sorted(missing))
    bad = 0
    for name, args in probes:
        t0 = time.time()
        res, ev = await h.runner.invoke(name, args, ctx)
        lat = int((time.time() - t0) * 1000)
        flag = "OK " if res.status in ("ok", "partial") else ("MISS" if res.status == "missing" else "ERR ")
        if flag == "ERR ":
            bad += 1
        print(f"[{flag}] {name} {json.dumps(args, ensure_ascii=False)}")
        print(f"       status={res.status} source={res.source} ts={res.data_timestamp} req={res.request_id} lat={lat}ms label={ev and ev.get('label')}")
        print(f"       口径: {res.unit_notes}")
        print(f"       摘要: {(res.summary or '')[:200]}")
        if res.error:
            print(f"       错误: {res.error}")
        if show_raw and res.data is not None:
            print("       原始:", json.dumps(res.data, ensure_ascii=False)[:500])
    print(f"\n熔断器状态: {h.breaker.state()}")
    await st.close()
    print(f"\n完成：{len(probes)} 次调用，error {bad} 次（missing 为预期的数据缺失，不计入）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--raw" in sys.argv)))
