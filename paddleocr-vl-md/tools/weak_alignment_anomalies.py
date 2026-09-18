"""弱对齐异常审查（Phase 4.1）——离线，机器先给第一轮 reason。

对 ``weak_question_alignment`` 报出的两类异常逐个定性：

* **对齐失败**（题干在 block 序列里找不到起点）；
* **超长 span**（相邻两题起点之间跨了很多 block）。

每例给一个 ``machine_reason`` + 证据片段，供人工/GPT 复核；这不是真值判定。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import weak_question_alignment as wa  # noqa: E402


LONG_SPAN_THRESHOLD = 20
SOLUTION_MARKERS = ("参考答案", "答案", "解析", "解答", "解：", "证明")


def _join_contents(blocks: Sequence[Mapping[str, Any]], start: int, end: int) -> str:
    return "\n".join(str(block.get("content") or "") for block in blocks[start:end])


def classify_long_span(
    blocks: Sequence[Mapping[str, Any]], start: int, next_start: int
) -> tuple[str, str]:
    """给超长 span 一个机器判断，返回 (reason, evidence)。"""

    between = _join_contents(blocks, start, next_start)
    between_blocks = blocks[start:next_start]
    first_page = blocks[start].get("page_seq")
    last_page = blocks[next_start - 1].get("page_seq") if next_start - 1 < len(blocks) else first_page

    has_number = any(str(block.get("label") or "") == "number" for block in between_blocks)
    if any(marker in between for marker in SOLUTION_MARKERS) and has_number:
        return "SOLUTION_BLEED", between[:80]
    if has_number:
        return "ALIGNMENT_DRIFT", f"区间内有 {sum(1 for b in between_blocks if str(b.get('label')) == 'number')} 个 number 块（疑似漏对齐的题号）"
    if next_start == start:
        return "MULTI_PROBLEM_BLOCK", between[:80]
    if first_page != last_page:
        return "CROSS_PAGE", f"p{first_page}→p{last_page}"
    if len(between_blocks) > LONG_SPAN_THRESHOLD:
        return "REAL_LONG_PROBLEM", between[:80]
    return "ALIGNMENT_DRIFT", between[:80]


def classify_failure(statement: str, blocks: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """对齐失败：题干文本与新块文本到底差在哪。"""

    normalized = wa.normalize(statement)
    if not normalized:
        return "SOLUTION_BLEED", "题干引用为空"
    # 用题干中段/尾段再试一次：能命中说明只是开头被 OCR 改写
    for window in (60, 40, 24):
        for offset in range(0, max(1, len(normalized) - window), max(1, window // 2)):
            probe = normalized[offset : offset + window]
            if len(probe) < 12:
                continue
            for block in blocks:
                if probe in wa.normalize(str(block.get("content") or "")):
                    return "OCR_CONTENT_VARIATION", f"中段命中（offset={offset}, window={window}）"
    return "ALIGNMENT_ALGORITHM_LIMIT", normalized[:60]


def review_document(plan_path: pathlib.Path, blocks_evidence_path: pathlib.Path) -> list[dict[str, Any]]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    units = wa.load_old_units(plan_path.parent / "evidence.json")
    evidence = json.loads(blocks_evidence_path.read_text(encoding="utf-8"))
    blocks = wa.flatten_blocks(evidence)
    document_id = str(evidence.get("document_id") or blocks_evidence_path.stem)

    findings: list[dict[str, Any]] = []
    starts: list[tuple[str, int]] = []
    for question in plan.get("questions") or []:
        label = str(question.get("source_item_id") or question.get("local_key") or "?")
        text = wa.statement_text(question, units)
        index = wa.find_start_block(text, blocks)
        if index is None:
            reason, evidence_text = classify_failure(text, blocks)
            findings.append(
                {
                    "document_id": document_id,
                    "source_item_id": label,
                    "kind": "alignment_failure",
                    "machine_reason": reason,
                    "evidence": evidence_text,
                }
            )
            continue
        starts.append((label, index))

    for position, (label, start) in enumerate(starts):
        next_start = starts[position + 1][1] if position + 1 < len(starts) else len(blocks)
        span = max(1, next_start - start)
        if span <= LONG_SPAN_THRESHOLD:
            continue
        reason, evidence_text = classify_long_span(blocks, start, next_start)
        first_page = blocks[start].get("page_seq")
        last_page = blocks[min(len(blocks) - 1, next_start - 1)].get("page_seq")
        findings.append(
            {
                "document_id": document_id,
                "source_item_id": label,
                "kind": "long_span",
                "span": span,
                "pages": f"p{first_page}→p{last_page}",
                "machine_reason": reason,
                "evidence": evidence_text,
            }
        )
    return findings


def build_report(plan_dir: pathlib.Path, evidence_dir: pathlib.Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for plan_path in sorted(plan_dir.glob("*/rule_plan.json")):
        blocks_path = evidence_dir / f"{plan_path.parent.name}.evidence.json"
        if not blocks_path.is_file():
            continue
        findings.extend(review_document(plan_path, blocks_path))

    counts: dict[str, int] = {}
    for finding in findings:
        key = f"{finding['kind']}:{finding['machine_reason']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "schema_version": "md-prework/weak-alignment-anomalies/v1",
        "note": "机器初判，需人工/GPT 复核；reason 不是真值",
        "totals": {
            "anomalies": len(findings),
            "alignment_failures": sum(1 for f in findings if f["kind"] == "alignment_failure"),
            "long_spans": sum(1 for f in findings if f["kind"] == "long_span"),
        },
        "reason_counts": dict(sorted(counts.items(), key=lambda item: -item[1])),
        "findings": findings,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 弱对齐异常审查（机器初判）",
        "",
        f"- 异常 {report['totals']['anomalies']} 例：对齐失败 {report['totals']['alignment_failures']}，"
        f"超长 span {report['totals']['long_spans']}",
        "",
        "## 原因分布",
        "",
        "| kind:reason | 数量 |",
        "| --- | --- |",
    ]
    for key, count in report["reason_counts"].items():
        lines.append(f"| {key} | {count} |")
    lines += [
        "",
        "## 明细",
        "",
        "| 文档 | 题目 | 类型 | span | 页 | 机器初判 | 证据 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for finding in report["findings"]:
        snippet = str(finding["evidence"]).replace("|", "/").replace("\n", " ")[:60]
        lines.append(
            f"| {finding['document_id']} | {finding['source_item_id']} | {finding['kind']} | "
            f"{finding.get('span', '-')} | {finding.get('pages', '-')} | "
            f"{finding['machine_reason']} | {snippet} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="弱对齐异常审查（Phase 4.1）")
    parser.add_argument("--plan-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown", default="")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    report = build_report(pathlib.Path(args.plan_dir), pathlib.Path(args.evidence_dir))
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        path = pathlib.Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"totals": report["totals"], "reasons": report["reason_counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
