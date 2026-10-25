"""数值核验：核心结论里的数字能否在其引用证据的原始字段中找到。

题目要求“核心结论必须能回到原始字段或原文”。引用编号只能证明“指向了某条证据”，
这里进一步核对数值本身：把【事实】要点中的数字（按 亿 / 万 / % 等单位换算、按显示精度容差）
与所引证据（及其 based_on 上游证据）原始 JSON 中的全部数值比对，找不到的标注“待核”。

刻意保守：只核对“像度量值”的数字（带小数、带单位或千分位），跳过日期、代码、报告期、
计数（59 根 / 29 日）等，避免误报；核不到只做标注，不改写事实内容。
"""
from __future__ import annotations

import bisect
import re
from typing import Any, Iterable, Optional

# 先剔除不是度量值的数字片段
_STRIP_PATTERNS = [
    r"\[(?:E\d+|引用无效)\]",
    r"\b\d{6}\.(?:SH|SZ|BJ|TI)\b",
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?",
    r"\d{4}-[1-4](?!\d)",  # 报告期 2026-2
    r"\b\d{8}\b",  # yyyyMMdd
    r"\d{1,2}:\d{2}(?::\d{2})?",
    r"\d{4}\s*年(?:度|报|中报|一季报|三季报)?",
    r"\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?",
    r"(?:MA|ma|E|Q|T|t)\d+",
    r"√\s*\d+",
    r"\d+\s*(?:个月|个交易日|天|日|周|根|只|家|个|条|期|项|季度|年期|档|连板|板)",
    r"(?:近|前|后|最近)\s*\d+",
    r"[（(]\s*\d+\s*[)）]",
]
_STRIP_RE = re.compile("|".join(f"(?:{p})" for p in _STRIP_PATTERNS))
NUM_RE = re.compile(r"([+\-−]?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(%|％|亿|万|元|倍|点|股|手|次)?")
UNIT_SCALES = {"亿": (1e8, 1.0), "万": (1e4, 1.0), "%": (1.0, 0.01), "％": (1.0, 0.01)}


def extract_claim_numbers(text: str) -> list[dict]:
    """返回需要核验的数字：{raw, value, decimals, unit}。"""
    cleaned = _STRIP_RE.sub(" ", text)
    out = []
    for m in NUM_RE.finditer(cleaned):
        sign, digits, unit = m.group(1), m.group(2), m.group(3) or ""
        has_dec = "." in digits
        has_comma = "," in digits
        val = float(digits.replace(",", ""))
        if not (has_dec or has_comma or unit):
            # 无小数、无单位的整数：只核验“大数”，跳过年份与小计数
            if val < 1000 or 1900 <= val <= 2100:
                continue
        if unit in ("次",) and not has_dec:
            continue
        if sign in ("-", "−"):
            val = -val
        decimals = len(digits.split(".")[1]) if has_dec else 0
        out.append({"raw": (sign + digits + unit).strip(), "value": val, "decimals": decimals, "unit": unit})
    return out


def _to_float(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").rstrip("%％")
        if not s or len(s) > 32:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def collect_numbers(obj: Any, out: list[float], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            collect_numbers(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            collect_numbers(v, out, depth + 1)
    else:
        f = _to_float(obj)
        if f is not None:
            out.append(f)


class NumberBag:
    """一组证据的全部数值（原始 JSON + 摘要文本中的数字），支持带容差的查找。"""

    def __init__(self, evidences: Iterable[dict]):
        vals: list[float] = []
        for e in evidences:
            collect_numbers(e.get("raw"), vals)
            for txt in (e.get("summary"), e.get("unit_notes")):
                if txt:
                    for m in NUM_RE.finditer(str(txt)):
                        try:
                            vals.append(float(m.group(2).replace(",", "")))
                        except ValueError:
                            pass
        self._vals = sorted(set(vals) | {abs(v) for v in vals})

    def __len__(self) -> int:
        return len(self._vals)

    def contains(self, target: float, tol: float) -> bool:
        lo = bisect.bisect_left(self._vals, target - tol)
        return lo < len(self._vals) and self._vals[lo] <= target + tol

    def verify(self, claim: dict) -> bool:
        x = claim["value"]
        unit = claim["unit"]
        step = 10 ** (-claim["decimals"])  # 显示精度：允许四舍五入或截断造成的差异
        scales = {1.0}
        if unit in UNIT_SCALES:
            scales |= set(UNIT_SCALES[unit])
        for k in scales:
            t = x * k
            tol = step * k + 1e-9 * abs(t)
            # 正负号常由文字表达（“下降 2.16%”“回撤 9.27%”），因此按绝对值也接受
            if self.contains(t, tol) or self.contains(abs(t), tol):
                return True
        return False


def evidence_closure(labels: list[str], by_label: dict[str, dict]) -> list[dict]:
    """所引证据 + 其 based_on 上游证据（如 compute_price_stats 基于的 K 线）。"""
    seen: set[str] = set()
    out: list[dict] = []
    stack = list(labels)
    while stack:
        lb = stack.pop()
        if lb in seen or lb not in by_label:
            continue
        seen.add(lb)
        e = by_label[lb]
        out.append(e)
        raw = e.get("raw")
        if isinstance(raw, dict):
            b = raw.get("based_on")
            for x in (b if isinstance(b, list) else [b]):
                if isinstance(x, str):
                    stack.append(x.upper())
    return out
