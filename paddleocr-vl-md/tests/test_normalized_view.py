"""Normalized View V1 的不变量测试（离线，不依赖真实 OCR）。"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import normalized_view as nv  # noqa: E402


def _evidence() -> dict:
    return {
        "schema_version": "md-prework/ocr-evidence/v1",
        "document_id": "doc-abc12345",
        "content_hash": "deadbeef",
        "pages": [
            {
                "page_seq": 0,
                "blocks": [
                    {"block_ref": "p0000:b0000", "label": "text", "content": "设 a+b=10 。"},
                    {"block_ref": "p0000:b0001", "label": "display_formula", "content": "  x^2+y^2=1  "},
                    {"block_ref": "p0000:b0002", "label": "header", "content": "2020 年全国高中数学联赛"},
                    {"block_ref": "p0000:b0003", "label": "number", "content": "3."},
                    {"block_ref": "p0000:b0004", "label": "text", "content": "第二段。"},
                ],
            }
        ],
    }


def test_profile_dispatch() -> None:
    assert nv.profile_for_label("text") == nv.PROFILE_TEXT
    assert nv.profile_for_label("display_formula") == nv.PROFILE_FORMULA
    assert nv.profile_for_label("inline_formula") == nv.PROFILE_FORMULA
    assert nv.profile_for_label("header") == nv.PROFILE_PRESERVE
    assert nv.profile_for_label("image") == nv.PROFILE_PRESERVE
    assert nv.profile_for_label("table") == nv.PROFILE_PRESERVE


def test_preserve_labels_never_touched() -> None:
    view = nv.build_normalized_view(_evidence(), text_normalizer=lambda text: text.upper())
    preserved = [b for b in view["blocks"] if b["profile"] == nv.PROFILE_PRESERVE]
    assert preserved
    for block in preserved:
        assert block["status"] == nv.STATUS_UNCHANGED
        assert "normalized_content" not in block


def test_block_refs_and_count_are_stable() -> None:
    evidence = _evidence()
    view = nv.build_normalized_view(evidence, text_normalizer=lambda text: text)
    source_refs = [
        block["block_ref"] for page in evidence["pages"] for block in page["blocks"]
    ]
    assert [b["block_ref"] for b in view["blocks"]] == source_refs
    assert view["block_count"] == len(source_refs)
    assert view["evidence_hash"] == evidence["content_hash"]


def test_fatal_keeps_block_and_raw_text() -> None:
    def boom(text: str) -> str:
        raise ValueError("$ 不配对")

    view = nv.build_normalized_view(_evidence(), text_normalizer=boom)
    text_blocks = [b for b in view["blocks"] if b["profile"] == nv.PROFILE_TEXT]
    assert text_blocks and all(b["status"] == nv.STATUS_FATAL for b in text_blocks)
    assert all(b["normalized_content"] is None for b in text_blocks)
    assert all(b["diagnostics"] for b in text_blocks)
    assert len(view["blocks"]) == 5, "fatal 不得让块消失"


def test_missing_normalizer_is_fatal_not_silent() -> None:
    view = nv.build_normalized_view(_evidence(), text_normalizer=None)
    text_blocks = [b for b in view["blocks"] if b["profile"] == nv.PROFILE_TEXT]
    assert text_blocks
    assert all(b["status"] == nv.STATUS_FATAL for b in text_blocks)
    assert all(
        diag["code"] == "NORMALIZER_UNAVAILABLE"
        for block in text_blocks
        for diag in block["diagnostics"]
    )


def test_formula_profile_only_trims() -> None:
    view = nv.build_normalized_view(_evidence(), text_normalizer=lambda text: text)
    formula = [b for b in view["blocks"] if b["profile"] == nv.PROFILE_FORMULA][0]
    assert formula["status"] == nv.STATUS_CHANGED
    assert formula["normalized_content"] == "x^2+y^2=1"
    assert formula["actions"] == [{"kind": "TRIM_OUTER_WHITESPACE", "impact": "cosmetic"}]
    assert nv.change_impact(formula) == "cosmetic_only"


def test_non_idempotent_normalizer_is_reported() -> None:
    calls = {"n": 0}

    def growing(text: str) -> str:
        calls["n"] += 1
        return text + "!"

    view = nv.build_normalized_view(_evidence(), text_normalizer=growing)
    text_blocks = [b for b in view["blocks"] if b["profile"] == nv.PROFILE_TEXT]
    assert all(
        any(diag["code"] == "NOT_IDEMPOTENT" for diag in block["diagnostics"])
        for block in text_blocks
    )


def test_mixed_math_layout_is_preserved_not_fatal() -> None:
    """块内 display+inline 混排：现有 normalizer 不支持，但块本身可理解 → preserve + warning。"""

    def nested(text: str) -> str:
        raise ValueError(
            "ValidationError: ERROR_NESTED_MATH_ENVIRONMENT: nested math in display math at line 3:1"
        )

    view = nv.build_normalized_view(_evidence(), text_normalizer=nested)
    text_blocks = [b for b in view["blocks"] if b["profile"] == nv.PROFILE_TEXT]
    assert all(b["status"] == nv.STATUS_PRESERVED for b in text_blocks)
    raw_by_ref = {
        block["block_ref"]: block["content"]
        for page in _evidence()["pages"]
        for block in page["blocks"]
    }
    assert all(
        b["normalized_content"] == raw_by_ref[b["block_ref"]] for b in text_blocks
    ), "preserve 必须原样保留 raw 文本"
    assert all(
        diag["code"] == nv.WARN_MIXED_MATH for b in text_blocks for diag in b["diagnostics"]
    )
    assert nv.change_impact(text_blocks[0]) == nv.STATUS_PRESERVED


def test_unbalanced_math_still_fatal() -> None:
    """真正不闭合的数学环境仍然是 fatal（不能靠猜测修复）。"""

    def unbalanced(text: str) -> str:
        raise ValueError("ValidationError: ERROR_UNBALANCED_INLINE_MATH: unclosed inline math")

    view = nv.build_normalized_view(_evidence(), text_normalizer=unbalanced)
    text_blocks = [b for b in view["blocks"] if b["profile"] == nv.PROFILE_TEXT]
    assert all(b["status"] == nv.STATUS_FATAL for b in text_blocks)
    assert all(b["normalized_content"] is None for b in text_blocks)


def test_process_file_writes_sidecar_and_keeps_evidence(tmp_path: pathlib.Path) -> None:
    evidence_path = tmp_path / "doc.evidence.json"
    payload = json.dumps(_evidence(), ensure_ascii=False)
    evidence_path.write_text(payload, encoding="utf-8")
    before = evidence_path.read_bytes()

    target, view = nv.process_file(
        evidence_path, output_dir=tmp_path / "views", text_normalizer=lambda text: text
    )
    assert target.name == "doc.normalized.json"
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == nv.NORMALIZED_VIEW_SCHEMA
    assert evidence_path.read_bytes() == before, "Evidence 必须原样不动"
    assert view["block_count"] == 5


def test_summary_counts() -> None:
    view = nv.build_normalized_view(_evidence(), text_normalizer=lambda text: text + "x")
    summary = nv.summarize(view)
    assert summary["blocks"] == 5
    assert summary["totals"][nv.STATUS_CHANGED] == 3  # 2 个 text + 1 个 formula
    assert summary["totals"][nv.STATUS_UNCHANGED] == 2
    assert summary["totals"][nv.STATUS_FATAL] == 0
    assert summary["metrics"]["byte_changed"] == 3
    assert summary["metrics"]["substantive_changed"] == 2, "text 块是实质变化"
    assert summary["metrics"]["cosmetic_only"] == 1, "formula 块只有去空白"
