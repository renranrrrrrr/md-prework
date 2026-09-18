"""Normalizer diff 分桶与分层抽样（Phase 4.2a 第 2–3 步）。

只做确定性分桶，不改任何算法：

* ``HIGH_RISK``：没有数学证据却发生了数学化（新增 ``$``、英文词被包成数学、年份/纯数字散文被数学化）；
* ``WRAP_MATH_ONLY``：除"加数学环境"之外没有其它改动；
* ``PUNCTUATION_ONLY``：只有标点/空白规范化；
* ``MIXED``：上面两类都有；
* ``OTHER``：其它结构改动。

输出分桶计数 + 按配额抽样的明细（供人工/GPT 复核）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any, Mapping, Sequence


#: 高风险只看 GPT 列出的具体模式，不做"没有数学符号就算高风险"这类粗判：
#: 连续英文词被包成数学、年份被数学化、纯数字被数学化。
ASCII_WORD_IN_MATH_RE = re.compile(r"(?<!\\)\b[A-Za-z]{3,}\b")
_MATH_ENV_RE = re.compile(r"\$[^$\n]*\$")
NUMBER_IN_MATH_RE = re.compile(r"\$[^$]*\d[^$]*\$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
PUNCT_MAP = str.maketrans({"，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")"})


def _strip_math(text: str) -> str:
    return text.replace("$$", "").replace("$", "")


def _norm_punct(text: str) -> str:
    return re.sub(r"\s+", "", _strip_math(text).translate(PUNCT_MAP))


def classify_diff(raw: str, normalized: str) -> str:
    added_dollars = normalized.count("$") - raw.count("$")
    if added_dollars <= 0:
        return "OTHER"
    risky = (
        _ascii_word_in_math(normalized)
        or bool(YEAR_RE.search(_math_span(normalized)))
        or count_standalone_numeric_wraps(raw, normalized) > 0
    )
    if risky:
        return "HIGH_RISK"
    wrap_only = _strip_math(normalized) == _strip_math(raw)
    punct_only = _norm_punct(normalized) == _norm_punct(raw)
    if wrap_only:
        return "WRAP_MATH_ONLY"
    if punct_only:
        return "PUNCTUATION_ONLY"
    return "MIXED"


def _math_span(text: str) -> str:
    """取出所有数学环境内容，供年份等模式判断。"""

    parts = re.findall(r"\$([^$]*)\$", text, flags=re.DOTALL)
    return " ".join(parts)


def _ascii_word_in_math(text: str) -> bool:
    """数学环境里出现 3 个以上字母的英文单词（排除 ``\\sin`` 这类 LaTeX 命令）。"""

    for env in _MATH_ENV_RE.finditer(text):
        if ASCII_WORD_IN_MATH_RE.search(env.group(0)):
            return True
    return False


def collect(evidence_dir: pathlib.Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "HIGH_RISK": [],
        "WRAP_MATH_ONLY": [],
        "PUNCTUATION_ONLY": [],
        "MIXED": [],
        "OTHER": [],
    }
    fatals: list[dict[str, Any]] = []
    numeric_wraps = 0
    for evidence_path in sorted(evidence_dir.glob("*.evidence.json")):
        view_path = evidence_path.with_name(
            evidence_path.name.replace(".evidence.json", ".normalized.json")
        )
        if not view_path.is_file():
            continue
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        view = json.loads(view_path.read_text(encoding="utf-8"))
        raw = {
            block["block_ref"]: str(block.get("content") or "")
            for page in evidence.get("pages") or []
            for block in page.get("blocks") or []
        }
        document_id = str(evidence.get("document_id") or evidence_path.stem)
        for block in view.get("blocks") or []:
            ref = str(block.get("block_ref"))
            if block.get("status") == "fatal":
                fatals.append(
                    {
                        "document_id": document_id,
                        "block_ref": ref,
                        "diagnostic": (block.get("diagnostics") or [{}])[0].get("detail", ""),
                        "raw": raw.get(ref, "")[:300],
                    }
                )
                continue
            if block.get("profile") != "text_math_v1" or block.get("status") != "changed":
                continue
            normalized = str(block.get("normalized_content") or "")
            original = raw.get(ref, "")
            numeric_wraps += count_standalone_numeric_wraps(original, normalized)
            buckets[classify_diff(original, normalized)].append(
                {
                    "document_id": document_id,
                    "block_ref": ref,
                    "raw": original[:300],
                    "normalized": normalized[:300],
                    "distance": _distance(original, normalized),
                }
            )
    return buckets, fatals, numeric_wraps


def _distance(a: str, b: str) -> int:
    """轻量差异度量：长度差 + 共同前缀外的长度（足够用来排序抽样）。"""

    common = 0
    for left, right in zip(a, b):
        if left != right:
            break
        common += 1
    return (len(a) - common) + (len(b) - common)


def count_standalone_numeric_wraps(raw: str, normalized: str) -> int:
    """统计"孤立数字被新包进数学环境"的次数（GPT 冻结门的核心指标）。

    只数数学环境里**只有数字**（可带千分位/小数点/百分号）的情形，且是相对原文的增量，
    不会把原文已有的 ``$10$`` 算进去。
    """

    def spans(text: str) -> list[str]:
        found: list[str] = []
        for match in re.finditer(r"\$[^$\n]*\$", text):
            inner = match.group(0).strip("$").strip()
            if inner and re.fullmatch(r"[\d,]+(?:\.\d+)?%?", inner):
                found.append(inner)
        return found

    extra = spans(normalized)
    for item in spans(raw):
        if item in extra:
            extra.remove(item)
    return len(extra)


QUOTA = {
    "HIGH_RISK": 15,
    "WRAP_MATH_ONLY": 10,
    "MIXED": 10,
    "PUNCTUATION_ONLY": 10,
    "OTHER": 0,
}


def sample(buckets: Mapping[str, Sequence[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    chosen: dict[str, list[dict[str, Any]]] = {}
    defecit = 0
    for name, quota in QUOTA.items():
        items = sorted(buckets.get(name, []), key=lambda item: -item["distance"])
        chosen[name] = list(items[:quota])
        if len(items) < quota:
            defecit += quota - len(items)
    if defecit:
        extra = sorted(buckets.get("HIGH_RISK", []), key=lambda item: -item["distance"])[
            len(chosen["HIGH_RISK"]) : len(chosen["HIGH_RISK"]) + defecit
        ]
        chosen["HIGH_RISK"].extend(extra)
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalizer diff 分桶与抽样")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown", default="")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    buckets, fatals, numeric_wraps = collect(pathlib.Path(args.evidence_dir))
    chosen = sample(buckets)
    report = {
        "schema_version": "md-prework/normalizer-diff-audit/v1",
        "buckets": {name: len(items) for name, items in buckets.items()},
        "changed_text_total": sum(len(items) for items in buckets.values()),
        "high_risk_ratio": round(len(buckets["HIGH_RISK"]) / max(1, sum(len(v) for v in buckets.values())), 4),
        "sample": chosen,
        "fatal": fatals,
        "standalone_numeric_wraps": numeric_wraps,
    }
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        lines = ["# Normalizer diff 分桶（Phase 4.2a）", "", f"- changed text 总数 {report['changed_text_total']}", f"- 高风险比例 {report['high_risk_ratio']}", ""]
        for name, count in report["buckets"].items():
            lines.append(f"- {name}: {count}")
        lines += ["", "## 抽样明细", ""]
        for name, items in chosen.items():
            for item in items:
                lines.append(f"### {name} · {item['document_id']} · {item['block_ref']}")
                lines.append(f"- 原：{item['raw'][:160]}")
                lines.append(f"- 新：{item['normalized'][:160]}")
                lines.append("")
        markdown_path = pathlib.Path(args.markdown)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"buckets": report["buckets"], "changed_text_total": report["changed_text_total"], "high_risk_ratio": report["high_risk_ratio"], "fatal": len(fatals), "standalone_numeric_wraps": numeric_wraps}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
