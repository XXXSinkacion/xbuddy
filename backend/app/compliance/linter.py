"""合规闸门：禁止确定性预测 / 收益承诺 / 买卖建议；结论必须引用证据；补齐固定章节。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..tools.fuyao import fmt_ts

FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"必涨|必跌|稳赚|稳赢|保本|无风险|躺赢", "确定性预测/收益承诺"),
    (r"保证(收益|盈利|赚钱|回报)", "收益承诺"),
    (r"(强烈)?(建议|推荐)(大家)?(买入|卖出|加仓|减仓|持有|清仓|抄底|追高|入场|离场|配置|买进|卖出)", "交易建议"),
    (r"(买入|卖出|增持|减持|中性|回避)评级", "评级式建议"),
    (r"目标价|止损位|止盈位|建仓价|加仓点", "交易指令"),
    (r"(一定|肯定|必然|绝对|毫无疑问)(会|能|将)?(上涨|下跌|涨|跌|翻倍|赚|反弹|创新高|突破)", "确定性预测"),
    (r"(将|会|即将|有望|预计)(大幅|继续|持续|显著)?(上涨|下跌|反弹|回调|翻倍|创新高|突破|暴涨|暴跌)", "涨跌预测"),
    (r"(可以|值得|建议|应该|不妨)(现在|趁机|逢低|逢高)?(买入|卖出|抄底|入手|建仓|清仓|减仓|加仓)", "交易建议"),
    (r"翻倍|十倍股|牛股|妖股", "夸张表述"),
]
REMOVED_MARK = "[已按合规规则移除：疑似确定性预测或交易建议]"
REQUIRED_SECTIONS = ["核心结论", "数据口径与时点", "数据缺失与限制", "证据清单", "免责声明"]
CITATION_RE = re.compile(r"\[(E\d+)\]")
LABEL_RE = re.compile(r"【(事实|推断|不确定)】")
DISCLAIMER = (
    "本报告由「投资 X Buddy」AI 工作台基于公开数据源（扶摇金融数据 API / iFinD MCP）自动生成，仅为研究过程记录与数据整理，"
    "不构成任何投资建议、买卖推荐或收益承诺，不对任何证券的未来价格作出预测。所有结论均可回溯至证据清单中的原始字段，"
    "请以原始数据源与官方披露为准，并注意数据时点、单位与统计口径差异。市场有风险，决策需独立判断。"
)


@dataclass
class LintResult:
    ok: bool
    md: str
    violations: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"ok": self.ok, "violations": self.violations, "stats": self.stats}


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；\n])", text)
    return [p for p in parts if p]


def scan_forbidden(md: str) -> list[dict]:
    hits = []
    for line_no, line in enumerate(md.splitlines(), 1):
        for pat, kind in FORBIDDEN_PATTERNS:
            for m in re.finditer(pat, line):
                hits.append({"type": "forbidden", "kind": kind, "match": m.group(0), "line": line_no, "text": line.strip()[:160]})
    return hits


def remove_forbidden(md: str) -> tuple[str, int]:
    out_lines = []
    removed = 0
    for line in md.splitlines():
        if any(re.search(p, line) for p, _ in FORBIDDEN_PATTERNS):
            sentences = _split_sentences(line)
            new = []
            for s in sentences:
                if any(re.search(p, s) for p, _ in FORBIDDEN_PATTERNS):
                    removed += 1
                    # 保留列表符号与引用
                    prefix = re.match(r"^\s*(?:[-*]|\d+\.)\s*", s)
                    cites = " ".join(CITATION_RE.findall(s))
                    new.append((prefix.group(0) if prefix else "") + REMOVED_MARK + (f" [{cites}]" if cites else ""))
                else:
                    new.append(s)
            out_lines.append("".join(new))
        else:
            out_lines.append(line)
    return "\n".join(out_lines), removed


def _section(md: str, title: str) -> Optional[tuple[int, int]]:
    m = re.search(rf"^##\s*[\d一二三四五六七八九十、.\s]*{re.escape(title)}.*$", md, re.M)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^##\s", md[start:], re.M)
    end = start + nxt.start() if nxt else len(md)
    return start, end


def check_citations(md: str, labels: set[str]) -> tuple[str, list[dict]]:
    viol: list[dict] = []
    sec = _section(md, "核心结论")
    if not sec:
        return md, [{"type": "missing_section", "section": "核心结论"}]
    start, end = sec
    body = md[start:end]
    new_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        is_bullet = bool(re.match(r"^(?:[-*]|\d+\.)\s+", stripped))
        if not is_bullet or len(stripped) < 8:
            new_lines.append(line)
            continue
        cites = CITATION_RE.findall(stripped)
        bad = [c for c in cites if c not in labels]
        if bad:
            viol.append({"type": "bad_citation", "labels": bad, "text": stripped[:160]})
            for b in bad:
                line = line.replace(f"[{b}]", "[引用无效]")
        if not cites or all(c in bad for c in cites):
            viol.append({"type": "uncited", "text": stripped[:160]})
            if "【不确定】" not in line:
                line = re.sub(r"^(\s*(?:[-*]|\d+\.)\s+)", r"\1【不确定·无证据引用】", line, count=1)
        new_lines.append(line)
    new_body = "\n".join(new_lines)
    if not LABEL_RE.search(new_body):
        viol.append({"type": "no_labels", "text": "核心结论未使用【事实】【推断】【不确定】分层标签"})
    return md[:start] + new_body + md[end:], viol


def build_missing_section(evidence: list[dict]) -> str:
    bad = [e for e in evidence if e.get("status") not in ("ok",)]
    lines = []
    for e in bad:
        st = e.get("status")
        desc = {"missing": "数据缺失", "error": "调用失败", "stale": "使用过期缓存", "partial": "部分缺失"}.get(st, st)
        lines.append(f"- [{e['label']}] {e['tool']} — {desc}：{(e.get('error') or e.get('summary') or '')[:140]}")
    if not lines:
        lines.append("- 本次研究所用证据均成功获取；仍需注意数据时点与口径（见上节）。")
    fixture = [e for e in evidence if "演示数据" in (e.get("source") or "")]
    if fixture:
        lines.append(f"- ⚠️ 共 {len(fixture)} 条证据来自**演示数据（fixture）**，并非真实行情，仅用于功能演示。")
    return "\n".join(lines)


def build_evidence_section(evidence: list[dict]) -> str:
    rows = ["| 编号 | 工具 | 来源 | 状态 | 数据时点 | 单位/口径 | 摘要 |", "|---|---|---|---|---|---|---|"]
    for e in evidence:
        ts = fmt_ts(e.get("data_timestamp")) if e.get("data_timestamp") else "—"
        rows.append(
            f"| {e['label']} | {e['tool']} | {e.get('source','')} | {e.get('status','')} | {ts} | {(e.get('unit_notes') or '').replace('|','/')[:60]} | {(e.get('summary') or e.get('error') or '').replace('|','/')[:80]} |"
        )
    return "\n".join(rows)


def build_scope_section(evidence: list[dict]) -> str:
    lines = []
    seen = set()
    for e in evidence:
        if e.get("status") not in ("ok", "partial", "stale"):
            continue
        key = (e["tool"], e.get("unit_notes"))
        if key in seen:
            continue
        seen.add(key)
        ts = fmt_ts(e.get("data_timestamp")) if e.get("data_timestamp") else "接口未提供（以抓取时间为准：" + fmt_ts(e.get("fetched_at")) + "）"
        lines.append(f"- [{e['label']}] {e['tool']}：时点 {ts}；{e.get('unit_notes') or '口径见原始字段'}")
    return "\n".join(lines) if lines else "- 无成功获取的数据。"


def ensure_sections(md: str, evidence: list[dict]) -> tuple[str, list[str]]:
    added = []
    if not _section(md, "数据口径与时点"):
        md += "\n\n## 数据口径与时点\n" + build_scope_section(evidence)
        added.append("数据口径与时点")
    if not _section(md, "数据缺失与限制"):
        md += "\n\n## 数据缺失与限制\n" + build_missing_section(evidence)
        added.append("数据缺失与限制")
    else:
        # 已有章节也追加自动检测到的缺失（去重由读者判断）
        auto = build_missing_section(evidence)
        if "自动检测" not in md:
            s, e = _section(md, "数据缺失与限制")  # type: ignore[misc]
            md = md[:e].rstrip() + "\n\n**自动检测（来自证据状态）：**\n" + auto + md[e:]
    if not _section(md, "证据清单"):
        md += "\n\n## 证据清单\n" + build_evidence_section(evidence)
        added.append("证据清单")
    if not _section(md, "免责声明"):
        md += "\n\n## 免责声明\n" + DISCLAIMER
        added.append("免责声明")
    return md, added


def lint(md: str, evidence: list[dict]) -> LintResult:
    labels = {e["label"] for e in evidence}
    violations: list[dict] = []
    forb = scan_forbidden(md)
    violations += forb
    md2, removed = remove_forbidden(md) if forb else (md, 0)
    md3, cit = check_citations(md2, labels)
    violations += cit
    md4, added = ensure_sections(md3, evidence)
    if added:
        violations.append({"type": "sections_added", "sections": added})
    ok = not any(v["type"] in ("forbidden", "bad_citation") for v in violations)
    return LintResult(ok=ok, md=md4, violations=violations, stats={"forbidden_removed": removed, "uncited": sum(1 for v in cit if v["type"] == "uncited"), "bad_citations": sum(1 for v in cit if v["type"] == "bad_citation"), "sections_added": added})
