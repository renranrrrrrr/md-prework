"""Weak question ↔ Paddle block alignment —— 离线弱对齐，不联网、不当真值。

目的：用**已有的** ProblemBank 重建结果（268 题）去回答一个量级问题：
一道题平均跨几个 Paddle block？从而检验 ``window=5 / stride=3`` 是否站得住。

做法（明确是"弱"对齐）：

1. 读旧重建计划 ``rule_plan.json`` 的 ``statement`` 引用，从旧 ``evidence.json``
   取出每道题的题干文本；
2. 按文档找到新的 block 级证据，把题干开头若干字符在 block 文本里做归一化匹配，
   得到该题的**起始块**；
3. 相邻两题起始块之间的块数即该题的 block span（最后一题取到文档末尾）；
4. 统计 p50/p90/p95/max、1 块题占比、跨页题占比、同块两题的数量，以及对齐失败数。

这只是结构量级的证据，不是 Ground Truth；对齐失败必须如实计数。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import sys
from typing import Any, Iterable, Mapping, Sequence


PROBE_LENGTHS = (40, 28, 20, 14)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """归一化：去掉所有空白，统一常见全角/半角标点差异。"""

    text = _WS_RE.sub("", text or "")
    table = str.maketrans({"，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")"})
    return text.translate(table)


def load_old_units(evidence_path: pathlib.Path) -> dict[str, str]:
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    return {str(unit["unit_id"]): str(unit.get("raw_text") or "") for unit in payload.get("units") or []}


def statement_text(question: Mapping[str, Any], units: Mapping[str, str]) -> str:
    parts: list[str] = []
    for ref in question.get("statement") or []:
        unit_id = str(ref.get("unit_id") or "")
        text = units.get(unit_id, "")
        start = int(ref.get("start") or 0)
        end = int(ref.get("end") or 0)
        if not text or end <= start:
            continue
        separator = {"linebreak": "\n", "paragraph": "\n\n", "space": " "}.get(
            str(ref.get("join_before") or "none"), ""
        )
        if parts and separator:
            parts.append(separator)
        parts.append(text[start:end])
    return "".join(parts)


def flatten_blocks(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for page in evidence.get("pages") or []:
        for block in page.get("blocks") or []:
            blocks.append({"page_seq": page.get("page_seq"), **block})
    return blocks


def find_start_block(statement: str, blocks: Sequence[Mapping[str, Any]]) -> int | None:
    """在块序列里定位题干的起始块；找不到返回 None（如实计入失败）。"""

    normalized_statement = normalize(statement)
    if not normalized_statement:
        return None
    for length in PROBE_LENGTHS:
        probe = normalized_statement[:length]
        if len(probe) < 6:
            continue
        for index, block in enumerate(blocks):
            content = normalize(str(block.get("content") or ""))
            if not content:
                continue
            if probe in content:
                return index
    return None


def percentile(values: Sequence[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return int(ordered[position])


def align_document(plan_path: pathlib.Path, blocks_evidence_path: pathlib.Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    old_evidence_path = plan_path.parent / "evidence.json"
    units = load_old_units(old_evidence_path)
    new_evidence = json.loads(blocks_evidence_path.read_text(encoding="utf-8"))
    blocks = flatten_blocks(new_evidence)

    questions = list(plan.get("questions") or [])
    starts: list[tuple[str, int]] = []
    failures: list[str] = []
    for question in questions:
        text = statement_text(question, units)
        index = find_start_block(text, blocks)
        if index is None:
            failures.append(str(question.get("source_item_id") or question.get("local_key") or "?"))
            continue
        starts.append((str(question.get("source_item_id") or "?"), index))

    spans: list[int] = []
    spans_with_page: list[tuple[int, int]] = []
    same_block_pairs = 0
    for position, (_, start) in enumerate(starts):
        next_start = starts[position + 1][1] if position + 1 < len(starts) else len(blocks)
        span = max(1, next_start - start)
        spans.append(span)
        first_page = blocks[start].get("page_seq") if start < len(blocks) else None
        last_index = min(len(blocks) - 1, next_start - 1)
        last_page = blocks[last_index].get("page_seq") if last_index >= start else first_page
        spans_with_page.append((int(first_page or 0), int(last_page or 0)))
        if next_start == start:
            same_block_pairs += 1

    cross_page = sum(1 for first, last in spans_with_page if last > first)
    return {
        "document_id": new_evidence.get("document_id") or blocks_evidence_path.stem,
        "questions": len(questions),
        "aligned": len(starts),
        "alignment_failures": failures,
        "block_count": len(blocks),
        "span_p50": percentile(spans, 0.5),
        "span_p90": percentile(spans, 0.9),
        "span_p95": percentile(spans, 0.95),
        "span_max": max(spans) if spans else 0,
        "spans": spans,
        "single_block_questions": sum(1 for span in spans if span == 1),
        "within_3_blocks": sum(1 for span in spans if span <= 3),
        "within_5_blocks": sum(1 for span in spans if span <= 5),
        "within_7_blocks": sum(1 for span in spans if span <= 7),
        "cross_page_questions": cross_page,
        "same_block_consecutive_pairs": same_block_pairs,
    }


def build_report(plan_dir: pathlib.Path, evidence_dir: pathlib.Path) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    missing: list[str] = []
    for plan_path in sorted(plan_dir.glob("*/rule_plan.json")):
        stem = plan_path.parent.name
        blocks_path = evidence_dir / f"{stem}.evidence.json"
        if not blocks_path.is_file():
            missing.append(stem)
            continue
        documents.append(align_document(plan_path, blocks_path))

    all_spans = [span for doc in documents for span in doc["spans"]]
    questions = sum(doc["questions"] for doc in documents)
    aligned = sum(doc["aligned"] for doc in documents)
    return {
        "schema_version": "md-prework/weak-question-alignment/v1",
        "note": "弱对齐：用已有重建计划的题干开头匹配 Paddle block 文本，不是 Ground Truth",
        "totals": {
            "documents": len(documents),
            "documents_missing_evidence": missing,
            "questions": questions,
            "aligned": aligned,
            "alignment_failures": questions - aligned,
            "questions_with_span": len(all_spans),
            "single_block_questions": sum(doc["single_block_questions"] for doc in documents),
            "cross_page_questions": sum(doc["cross_page_questions"] for doc in documents),
            "same_block_consecutive_pairs": sum(
                doc["same_block_consecutive_pairs"] for doc in documents
            ),
            "within_3_blocks_ratio": round(sum(doc["within_3_blocks"] for doc in documents) / len(all_spans), 4)
            if all_spans
            else None,
            "within_5_blocks_ratio": round(sum(doc["within_5_blocks"] for doc in documents) / len(all_spans), 4)
            if all_spans
            else None,
            "within_7_blocks_ratio": round(sum(doc["within_7_blocks"] for doc in documents) / len(all_spans), 4)
            if all_spans
            else None,
        },
        "span_distribution": {
            "p50": percentile(all_spans, 0.5),
            "p90": percentile(all_spans, 0.9),
            "p95": percentile(all_spans, 0.95),
            "max": max(all_spans) if all_spans else 0,
            "mean": round(statistics.fmean(all_spans), 2) if all_spans else None,
        },
        "documents": documents,
    }


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    distribution = report["span_distribution"]
    lines = [
        "# 弱对齐：一道题跨多少个 Paddle block",
        "",
        f"- 文档 {totals['documents']} 份，题 {totals['questions']}，成功对齐 {totals['aligned']}"
        f"（失败 {totals['alignment_failures']}）",
        f"- span 分布：p50={distribution['p50']}，p90={distribution['p90']}，"
        f"p95={distribution['p95']}，max={distribution['max']}，mean={distribution['mean']}",
        f"- ≤3 块 {totals['within_3_blocks_ratio']}，≤5 块 {totals['within_5_blocks_ratio']}，"
        f"≤7 块 {totals['within_7_blocks_ratio']}",
        f"- 单块题 {totals['single_block_questions']}，跨页题 {totals['cross_page_questions']}，"
        f"相邻两题起始块相同 {totals['same_block_consecutive_pairs']}",
        "",
        "| 文档 | 题 | 对齐 | p50 | p95 | max | ≤5 块 | 跨页 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for doc in report["documents"]:
        lines.append(
            f"| {doc['document_id']} | {doc['questions']} | {doc['aligned']} | {doc['span_p50']} | "
            f"{doc['span_p95']} | {doc['span_max']} | {doc['within_5_blocks']} | {doc['cross_page_questions']} |"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="弱对齐：题目 ↔ Paddle block span 统计")
    parser.add_argument("--plan-dir", required=True, help="含 <doc>/rule_plan.json 的目录")
    parser.add_argument("--evidence-dir", required=True, help="含 <doc>.evidence.json 的目录")
    parser.add_argument("--output", required=True, help="报告 JSON 输出路径")
    parser.add_argument("--markdown", default="", help="可选 Markdown 摘要")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    print(json.dumps({"totals": report["totals"], "span": report["span_distribution"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
