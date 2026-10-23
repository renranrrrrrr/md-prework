"""从已有 Evidence 与 semantic-run 离线复算组装；没有 OCR / LLM provider 入口。"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import prework_paths  # noqa: E402
import semantic_assembler as assembler  # noqa: E402
import semantic_chain as chain  # noqa: E402


def replay_document(evidence_path: pathlib.Path, run_path: pathlib.Path) -> dict[str, Any]:
    """原候选、固定旧切分的重组装、当前切分汇总的重组装分别核查。输入只读。"""

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if not evidence.get("content_hash") or run.get("evidence_hash") != evidence["content_hash"]:
        raise ValueError("evidence_hash_mismatch")
    if not evidence.get("document_id") or run.get("document_id") != evidence["document_id"]:
        raise ValueError("document_id_mismatch")
    if "reconciled" not in run or "candidates" not in run:
        raise ValueError("semantic_run_missing_assembly_inputs")

    # 只取不可变原文与原始身份，排序与 semantic_window.load_document 相同；不需要重跑格式层。
    blocks = [
        {"block_ref": block.get("block_ref"), "raw_text": block.get("content") or ""}
        for page in sorted(evidence.get("pages") or [], key=lambda p: p.get("page_seq") or 0)
        for block in sorted(page.get("blocks") or [], key=lambda b: b.get("sequence_index") or 0)
    ]
    before = assembler.audit_block_coverage(blocks, run["candidates"])
    recorded_splits = run.get("splits") or {}
    fixed_splits = assembler.assemble(blocks, run["reconciled"], splits=recorded_splits)
    predictions = run.get("predictions")
    splits = (
        chain.resolve_prediction_splits(blocks, list(predictions.values()))
        if predictions
        else recorded_splits
    )
    after = assembler.assemble(blocks, run["reconciled"], splits=splits)
    coverage = assembler.audit_block_coverage(blocks, after)
    return {
        "status": "ok",
        "document_id": evidence["document_id"],
        "evidence_path": str(evidence_path),
        "run_path": str(run_path),
        "evidence_hash": evidence["content_hash"],
        "blocks": len(blocks),
        "before": before,
        "with_recorded_splits": assembler.audit_block_coverage(blocks, fixed_splits),
        "after": coverage,
        "split_source": "cached_predictions" if predictions else "recorded_splits",
        "splits": splits,
        "candidates": after,
        "historical_loss_roles": {
            ref: (run["reconciled"].get("roles") or {}).get(ref, "UNCERTAIN")
            for ref in before["silent_loss_refs"]
        },
    }


def _run_path(evidence: pathlib.Path, run_dir: pathlib.Path | None) -> pathlib.Path:
    stem = (
        evidence.parent.name.removesuffix("_prework")
        if evidence.name == "evidence.json"
        else evidence.name.removesuffix(".evidence.json")
    )
    root = run_dir if run_dir is not None else prework_paths.semantic_dir_for(evidence)
    named = root / f"{stem}.semantic-run.json"
    return named if named.is_file() else root / "semantic-run.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--run-dir", help="可选：显式 semantic-run 汇集目录；默认兼容新旧产物布局")
    parser.add_argument("--output", required=True, help="新建审计报告；拒绝覆盖任何已有文件")
    args = parser.parse_args(argv)
    evidence_paths = prework_paths.iter_evidence(pathlib.Path(args.evidence_dir))
    if not evidence_paths:
        parser.error("未找到 Evidence，不能把空样本报告为通过")
    output = pathlib.Path(args.output)
    if output.exists():
        parser.error("输出已存在；请指定新的报告文件，输入产物不会被覆盖")
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else None
    rows = []
    for evidence_path in evidence_paths:
        run_path = _run_path(evidence_path, run_dir)
        try:
            rows.append(replay_document(evidence_path, run_path))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            rows.append(
                {
                    "status": "error",
                    "evidence_path": str(evidence_path),
                    "run_path": str(run_path),
                    "error": str(exc),
                }
            )

    good = [row for row in rows if row["status"] == "ok"]
    complete = len(good) == len(rows)
    report = {
        "schema_version": "md-prework/assembly-replay/v1",
        "documents_found": len(rows),
        "documents_replayed": len(good),
        "complete": complete,
        "original_silent_loss": sum(len(row["before"]["silent_loss_refs"]) for row in good),
        "replayed_silent_loss": sum(len(row["after"]["silent_loss_refs"]) for row in good),
        "manual_review_records": sum(len(row["candidates"]["diagnostic_records"]) for row in good),
        "documents_detail": rows,
    }
    report["hard_gates_passed"] = complete and all(
        not row["after"]["silent_loss_refs"] and not row["after"]["invalid_references"] for row in good
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "documents_detail"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["hard_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
