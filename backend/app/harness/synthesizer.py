"""报告合成：LLM 撰写（含一次合规重写）→ Linter 强制修正；模型不可用时生成确定性模板报告。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..compliance.linter import LintResult, lint, scan_forbidden
from ..llm.base import LLMUnavailable
from ..llm.router import LLMRouter
from ..models import Plan
from ..tools.fuyao import fmt_ts

log = logging.getLogger("xbuddy.synth")
TZ = timezone(timedelta(hours=8))

SYNTH_SYSTEM = """你是「投资 X Buddy」的研究报告撰写器。根据任务小结与证据目录撰写 Markdown 研究报告。
硬性规则：
1. 固定章节（二级标题）：## 摘要、## 核心结论、## 数据口径与时点、## 数据缺失与限制。（证据清单与免责声明由系统自动追加，不要写。）
2. 「核心结论」每条要点必须以【事实】【推断】或【不确定】开头，并以 [E#] 引用证据编号（可多个）。没有证据支撑的内容只能标【不确定】。
3. 绝对禁止：涨跌预测（会涨/会跌/有望上涨…）、目标价、收益承诺、买入/卖出/加减仓/持有建议、评级。只做描述与可验证的比较。
4. 数值必须与证据一致，写明单位与时点；缺失/失败/过期缓存的数据要在「数据缺失与限制」如实列出，不得默认正常。
5. 语言：中文，简洁，面向投资研究者。"""


def _evidence_catalog(evidence: list[dict]) -> str:
    lines = []
    for e in evidence:
        ts = fmt_ts(e.get("data_timestamp")) if e.get("data_timestamp") else "未提供"
        lines.append(f"- [{e['label']}] {e['tool']} | 状态 {e.get('status')} | 来源 {e.get('source')} | 时点 {ts} | 单位/口径：{(e.get('unit_notes') or '')[:120]} | 摘要：{(e.get('summary') or e.get('error') or '')[:300]}")
    return "\n".join(lines) or "（无证据）"


class Synthesizer:
    def __init__(self, llm: Optional[LLMRouter]):
        self.llm = llm

    async def write(
        self,
        *,
        goal: str,
        plan: Plan,
        task_summaries: list[dict],
        evidence: list[dict],
        run_meta: dict,
        on_event: Optional[Callable[[str, dict], None]] = None,
        force_template: bool = False,
    ) -> tuple[str, LintResult, str]:
        """返回 (报告 md, lint 结果, 生成模式 llm|template)。"""
        mode = "template"
        md: Optional[str] = None
        if not force_template and self.llm is not None and self.llm.available:
            try:
                md = await self._write_llm(goal, plan, task_summaries, evidence, run_meta, on_event)
                mode = "llm"
            except LLMUnavailable as e:
                if on_event:
                    on_event("run.degraded", {"reason": f"报告阶段模型不可用：{e}，使用模板报告"})
        if md is None:
            md = template_report(goal, plan, task_summaries, evidence, run_meta)
        result = lint(md, evidence)
        return result.md, result, mode

    async def _write_llm(self, goal, plan, task_summaries, evidence, run_meta, on_event) -> str:
        assert self.llm is not None
        user = (
            f"研究目标：{goal}\n计划标题：{plan.title}\n运行状态：{run_meta.get('status_note', '完成')}\n\n"
            "任务小结：\n" + "\n\n".join(f"### {s['title']}（{s['status']}）\n{s['summary']}" for s in task_summaries)
            + "\n\n证据目录：\n" + _evidence_catalog(evidence)
            + "\n\n请撰写报告（Markdown）。"
        )
        messages = [{"role": "system", "content": SYNTH_SYSTEM}, {"role": "user", "content": user}]
        resp = await self.llm.chat(messages, purpose="synthesize", on_event=on_event, temperature=0.2, max_tokens=3000)
        md = (resp.content or "").strip()
        hits = scan_forbidden(md)
        if hits:
            if on_event:
                on_event("compliance.rewrite", {"hits": hits[:10]})
            messages += [{"role": "assistant", "content": md}, {"role": "user", "content": "以下句子违反合规规则（确定性预测 / 交易建议），请重写整份报告，删除或改写这些表述为中性描述，其余保持不变：\n" + "\n".join(f"- 「{h['match']}」：{h['text']}" for h in hits[:10])}]
            resp2 = await self.llm.chat(messages, purpose="synthesize_rewrite", on_event=on_event, temperature=0.1, max_tokens=3000)
            md = (resp2.content or md).strip()
        return md

    @staticmethod
    def usage_for(resp_list):  # 占位：成本累加在 runtime 完成
        return None


def template_report(goal: str, plan: Plan, task_summaries: list[dict], evidence: list[dict], run_meta: dict) -> str:
    ok_ev = [e for e in evidence if e.get("status") in ("ok", "partial", "stale")]
    bad_ev = [e for e in evidence if e.get("status") not in ("ok", "partial", "stale")]
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    lines = [f"# {plan.title}", "", "## 摘要", f"研究目标：{goal}。本报告由模板模式生成（{run_meta.get('mode_note', '模型不可用或未配置')}），共执行 {len(task_summaries)} 个任务，获得 {len(evidence)} 条证据（成功 {len(ok_ev)}，失败/缺失 {len(bad_ev)}）。生成时间 {now}。{run_meta.get('status_note', '')}", "", "## 核心结论"]
    for e in ok_ev:
        tag = "【事实】" if e.get("status") == "ok" else "【不确定】"
        lines.append(f"- {tag} {e.get('summary', '')[:300]} [{e['label']}]")
    # 简单推断：若有 compute_price_stats 与估值证据，做描述性比较
    stats = [e for e in ok_ev if e["tool"] == "compute_price_stats"]
    vals = [e for e in ok_ev if e["tool"] == "get_valuation_snapshot"]
    if stats and vals:
        lines.append(f"- 【推断】价格统计（{', '.join(s['label'] for s in stats)}）与估值快照（{', '.join(v['label'] for v in vals)}）取自不同时点与口径，解读时需结合报告期与复权方式，不构成任何方向性判断。 [{stats[0]['label']}] [{vals[0]['label']}]")
    for e in bad_ev:
        lines.append(f"- 【不确定】{e['tool']} 未获得数据：{(e.get('error') or e.get('summary') or '')[:200]} [{e['label']}]")
    if not ok_ev:
        lines.append("- 【不确定】本次运行未获得任何有效证据，无法给出结论。")
    lines += ["", "## 任务小结"]
    for s in task_summaries:
        lines += [f"### {s['title']}（{s['status']}）", s["summary"], ""]
    lines += ["## 数据缺失与限制", "- 模板模式仅罗列证据摘要，不做文字归纳；以下由系统按证据状态自动检测。"]
    return "\n".join(lines)


def propose_memories(goal: str, md: str, evidence: list[dict], run_id: str) -> list[dict]:
    """从报告中提炼可写入长期记忆的条目（需审批）。"""
    out = []
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    # 结论摘要
    i = md.find("## 核心结论")
    if i != -1:
        seg = md[i + 7 :]
        nxt = seg.find("\n## ")
        seg = seg if nxt == -1 else seg[:nxt]
        bullets = [l.strip() for l in seg.splitlines() if l.strip().startswith("-") and "【事实】" in l][:3]
        if bullets:
            out.append({"kind": "conclusion", "content": f"{day} 研究「{goal[:40]}」事实要点：" + " ".join(b.lstrip('- ') for b in bullets)[:400], "tags": f"conclusion,{day}"})
    codes = sorted({e.get("args", {}).get("thscode") for e in evidence if isinstance(e.get("args"), dict) and e.get("args", {}).get("thscode")})
    if codes:
        out.append({"kind": "note", "content": f"{day} 研究过的标的：{', '.join(codes[:6])}（目标：{goal[:40]}）", "tags": "history," + ",".join(codes[:6])})
    return out
