"""Normalized View V1 —— Evidence 的**派生 sidecar**（Phase 4.2 基础设施）。

红线（本层不变量）：

* 原文只存在于 OCR Evidence；本层只产出 ``block_ref → normalized_content``；
* 块数量、``block_ref``、label、bbox/polygon/order 一律不变；
* 规范化失败（fatal）**不得**让块消失：``normalized_content=null`` + 诊断，原文仍可用；
* 只按 label 分发：``text → text_math_v1``、``display_formula/inline_formula → formula_v1``、
  其余 label 一律 ``preserve``；
* 每个块记录 profile / version / status / diagnostics / actions，供日后训练小模型复盘。

profile：

* ``text_math_v1``：受控适配现有 ``md-math-normalizer``（若可用），只在 text 块上跑；
* ``formula_v1``：Paddle 已确认这是公式，因此**不做裸数学候选识别**，V1 只做保守 canonicalize；
* ``preserve``：不动。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any, Callable, Mapping, Sequence


NORMALIZED_VIEW_SCHEMA = "md-prework/normalized-view/v1"
NORMALIZER_VERSION = "normalized-view/v1"
PROFILE_TEXT = "text_math_v1"
PROFILE_FORMULA = "formula_v1"
PROFILE_PRESERVE = "preserve"

STATUS_CHANGED = "changed"
STATUS_UNCHANGED = "unchanged"
STATUS_FATAL = "fatal"

FORMULA_LABELS = {"display_formula", "inline_formula"}


def profile_for_label(label: str) -> str:
    if label == "text":
        return PROFILE_TEXT
    if label in FORMULA_LABELS:
        return PROFILE_FORMULA
    return PROFILE_PRESERVE


def resolve_text_normalizer() -> tuple[Callable[[str], str] | None, str]:
    """定位并加载现有 md-math-normalizer；拿不到就返回 (None, 说明)。"""

    candidates: list[pathlib.Path] = []
    override = os.environ.get("MD_MATH_NORMALIZER")
    if override:
        candidates.append(pathlib.Path(override))
    here = pathlib.Path(__file__).resolve().parent
    candidates.append(here.parent / "md-math-normalizer")
    for root in candidates:
        source = root / "src"
        if not (source / "md_math_normalizer").is_dir():
            continue
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        try:
            from md_math_normalizer import normalize_markdown_text  # type: ignore

            return normalize_markdown_text, f"md-math-normalizer @ {source}"
        except Exception as exc:  # noqa: BLE001 - 加载失败不应中断整批
            return None, f"加载失败：{type(exc).__name__}: {exc}"
    return None, "未找到 md-math-normalizer（可设置 MD_MATH_NORMALIZER）"


def canonicalize_formula(content: str) -> tuple[str, list[dict[str, Any]]]:
    """formula_v1：只做保守处理（去首尾空白），并如实记录动作。"""

    trimmed = content.strip()
    if trimmed == content:
        return content, []
    return trimmed, [{"kind": "TRIM_WHITESPACE"}]


def normalize_block(
    block: Mapping[str, Any],
    *,
    text_normalizer: Callable[[str], str] | None,
) -> dict[str, Any]:
    """规范化单个块；返回 sidecar 里的块条目。"""

    block_ref = str(block.get("block_ref") or "")
    label = str(block.get("label") or "")
    content = str(block.get("content") or "")
    profile = profile_for_label(label)
    entry: dict[str, Any] = {"block_ref": block_ref, "label": label, "profile": profile}

    if profile == PROFILE_PRESERVE:
        entry.update({"status": STATUS_UNCHANGED, "actions": [], "diagnostics": []})
        return entry

    if profile == PROFILE_FORMULA:
        normalized, actions = canonicalize_formula(content)
        entry.update(
            {
                "status": STATUS_CHANGED if normalized != content else STATUS_UNCHANGED,
                "normalized_content": normalized,
                "actions": actions,
                "diagnostics": [],
            }
        )
        return entry

    # text_math_v1
    if text_normalizer is None:
        entry.update(
            {
                "status": STATUS_FATAL,
                "normalized_content": None,
                "actions": [],
                "diagnostics": [{"code": "NORMALIZER_UNAVAILABLE"}],
            }
        )
        return entry
    try:
        normalized = text_normalizer(content)
    except Exception as exc:  # noqa: BLE001 - fatal 不丢块
        entry.update(
            {
                "status": STATUS_FATAL,
                "normalized_content": None,
                "actions": [],
                "diagnostics": [
                    {"code": "NORMALIZE_FATAL", "detail": f"{type(exc).__name__}: {exc}"}
                ],
            }
        )
        return entry

    diagnostics: list[dict[str, Any]] = []
    try:  # 幂等自检：再跑一遍不应继续变化
        if text_normalizer(normalized) != normalized:
            diagnostics.append({"code": "NOT_IDEMPOTENT"})
    except Exception as exc:  # noqa: BLE001
        diagnostics.append({"code": "SECOND_PASS_FATAL", "detail": type(exc).__name__})

    entry.update(
        {
            "status": STATUS_CHANGED if normalized != content else STATUS_UNCHANGED,
            "normalized_content": normalized,
            "actions": [{"kind": "NORMALIZE_TEXT"}] if normalized != content else [],
            "diagnostics": diagnostics,
        }
    )
    return entry


def build_normalized_view(
    evidence: Mapping[str, Any],
    *,
    text_normalizer: Callable[[str], str] | None,
    normalizer_source: str = "",
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for page in evidence.get("pages") or []:
        for block in page.get("blocks") or []:
            blocks.append(normalize_block(block, text_normalizer=text_normalizer))
    return {
        "schema_version": NORMALIZED_VIEW_SCHEMA,
        "normalizer_version": NORMALIZER_VERSION,
        "normalizer_source": normalizer_source,
        "evidence_schema_version": evidence.get("schema_version"),
        "evidence_hash": evidence.get("content_hash"),
        "document_id": evidence.get("document_id"),
        "block_count": len(blocks),
        "blocks": blocks,
    }


def summarize(view: Mapping[str, Any]) -> dict[str, Any]:
    per_label: dict[str, dict[str, int]] = {}
    idempotence_failures = 0
    for block in view.get("blocks") or []:
        label = str(block.get("label") or "")
        bucket = per_label.setdefault(
            label, {STATUS_CHANGED: 0, STATUS_UNCHANGED: 0, STATUS_FATAL: 0}
        )
        bucket[str(block.get("status"))] = bucket.get(str(block.get("status")), 0) + 1
        if any(diag.get("code") == "NOT_IDEMPOTENT" for diag in block.get("diagnostics") or []):
            idempotence_failures += 1
    totals = {
        STATUS_CHANGED: sum(bucket[STATUS_CHANGED] for bucket in per_label.values()),
        STATUS_UNCHANGED: sum(bucket[STATUS_UNCHANGED] for bucket in per_label.values()),
        STATUS_FATAL: sum(bucket[STATUS_FATAL] for bucket in per_label.values()),
    }
    return {
        "blocks": sum(totals.values()),
        "totals": totals,
        "idempotence_failures": idempotence_failures,
        "per_label": per_label,
    }


def process_file(
    evidence_path: pathlib.Path,
    *,
    output_dir: pathlib.Path | None = None,
    text_normalizer: Callable[[str], str] | None,
    normalizer_source: str = "",
) -> tuple[pathlib.Path, dict[str, Any]]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    view = build_normalized_view(
        evidence, text_normalizer=text_normalizer, normalizer_source=normalizer_source
    )
    stem = evidence_path.name.removesuffix(".evidence.json")
    target = (output_dir or evidence_path.parent) / f"{stem}.normalized.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(view, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target, view


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalized View V1（Evidence 派生 sidecar）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--evidence", help="单个 *.evidence.json")
    group.add_argument("--evidence-dir", help="包含 *.evidence.json 的目录")
    parser.add_argument("--output-dir", default="", help="sidecar 输出目录（默认与证据同目录）")
    parser.add_argument("--report", default="", help="benchmark 统计 JSON 输出路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    normalizer, source = resolve_text_normalizer()
    output_dir = pathlib.Path(args.output_dir) if args.output_dir else None
    if args.evidence:
        paths = [pathlib.Path(args.evidence)]
    else:
        paths = sorted(pathlib.Path(args.evidence_dir).glob("*.evidence.json"))
    if not paths:
        print("没有找到证据文件。", file=sys.stderr)
        return 2

    report: dict[str, Any] = {}
    for path in paths:
        _, view = process_file(
            path, output_dir=output_dir, text_normalizer=normalizer, normalizer_source=source
        )
        report[str(view.get("document_id") or path.stem)] = summarize(view)

    summary = {
        "schema_version": "md-prework/normalized-view-benchmark/v1",
        "normalizer_source": source,
        "documents": len(report),
        "totals": {
            key: sum(doc["totals"][key] for doc in report.values())
            for key in (STATUS_CHANGED, STATUS_UNCHANGED, STATUS_FATAL)
        },
        "idempotence_failures": sum(doc["idempotence_failures"] for doc in report.values()),
        "per_label": _merge_per_label(report.values()),
        "documents_detail": report,
    }
    if args.report:
        target = pathlib.Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("documents", "totals", "idempotence_failures")}, ensure_ascii=False, indent=2))
    return 0 if summary["totals"][STATUS_FATAL] == 0 else 1


def _merge_per_label(documents: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for document in documents:
        for label, bucket in (document.get("per_label") or {}).items():
            target = merged.setdefault(
                label, {STATUS_CHANGED: 0, STATUS_UNCHANGED: 0, STATUS_FATAL: 0}
            )
            for key, value in bucket.items():
                target[key] = target.get(key, 0) + int(value)
    return merged


if __name__ == "__main__":
    raise SystemExit(main())
