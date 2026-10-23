"""用假 provider 检查真实质量门与落盘结果，不访问 OCR / LLM API。"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_baseline as baseline  # noqa: E402
import semantic_prediction as prediction  # noqa: E402


def _files(tmp_path: pathlib.Path, refs: list[str] | None = None):
    refs = refs or ["p0000:b0000", "p0000:b0001"]
    blocks = [
        {"block_ref": ref, "sequence_index": i, "label": "text", "content": f"原文{i}"}
        for i, ref in enumerate(refs)
    ]
    evidence = tmp_path / "doc.evidence.json"
    view = tmp_path / "doc.normalized.json"
    evidence.write_text(
        json.dumps(
            {"document_id": "doc", "content_hash": "test-hash", "pages": [{"page_seq": 0, "blocks": blocks}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    view.write_text(
        json.dumps(
            {"blocks": [{"block_ref": ref, "status": "fatal", "normalized_content": None} for ref in refs]}
        ),
        encoding="utf-8",
    )
    return evidence, view


def _provider(roles):
    def decide(window):
        refs = [b["block_ref"] for b in window["blocks"]]
        return {
            "roles": [{"block_ref": ref, "role": roles[ref]} for ref in refs],
            "boundaries": [
                {"left_ref": a, "right_ref": b, "relation": "SAME_PROBLEM"} for a, b in zip(refs, refs[1:])
            ],
            "splits": [],
            "uncertain": [],
        }

    return prediction.FakeSemanticProvider(decide)


def test_baseline_eof_shared_context_passes_gate_with_reviewable_diagnostic(tmp_path) -> None:
    evidence, view = _files(tmp_path)
    before = (evidence.read_bytes(), view.read_bytes())
    row = baseline.run_document(
        evidence,
        view,
        lambda: _provider({"p0000:b0000": "PROBLEM_START", "p0000:b0001": "SHARED_CONTEXT"}),
        dump_dir=tmp_path / "dump",
    )
    assert row["silent_loss"] == 0
    assert all(row["gates"].values())
    assert row["assembly_coverage"]["diagnosed_refs"] == ["p0000:b0001"]
    saved = json.loads((tmp_path / "dump" / "doc.semantic-run.json").read_text(encoding="utf-8"))
    assert saved["candidates"]["diagnostic_records"] == [
        {"code": "orphan_shared_context", "block_ref": "p0000:b0001"}
    ]
    assert saved["assembly_coverage"] == row["assembly_coverage"]
    assert (evidence.read_bytes(), view.read_bytes()) == before


@pytest.mark.parametrize(
    "assembly,missing,valid_ranges",
    [
        ({"candidates": [], "diagnostics": ["orphan_solution:p0000:b00010"]}, ["p0000:b0001"], True),
        (
            {
                "candidates": [{"statement_refs": [{"block_ref": "p0000:b0001", "start": 0, "end": 1}]}],
                "excluded": [{"block_ref": "p0000:b00010", "reason": "test"}],
            },
            ["p0000:b0001"],
            True,
        ),
        (
            {
                "candidates": [{"statement_refs": [{"block_ref": "p0000:b0001", "start": -1, "end": 3}]}],
                "excluded": [{"block_ref": "p0000:b00010", "reason": "test"}],
            },
            ["p0000:b0001"],
            False,
        ),
    ],
)
def test_baseline_quality_gate_detects_missing_blocks_and_partial_ranges(
    tmp_path, monkeypatch, assembly, missing, valid_ranges
) -> None:
    evidence, view = _files(tmp_path, ["p0000:b0001", "p0000:b00010"])

    def stub(document, *_args, **_kwargs):
        return {
            "windows": [{"blocks": document["blocks"]}],
            "predictions": {},
            "reconciled": {"roles": {}, "has_conflict": False},
            "expansion": {},
            "splits": {},
            "candidates": assembly,
        }

    monkeypatch.setattr(baseline.chain, "run_chain", stub)
    row = baseline.run_document(evidence, view, lambda: None, dump_dir=tmp_path / "dump")
    assert row["gates"]["assembler_no_silent_loss"] is False
    assert row["gates"]["assembler_reference_integrity"] is valid_ranges
    assert row["assembly_coverage"]["silent_loss_refs"] == missing


def test_aggregate_keeps_actual_loss_counts(tmp_path) -> None:
    evidence, view = _files(tmp_path)
    row = baseline.run_document(
        evidence,
        view,
        lambda: _provider({"p0000:b0000": "SOLUTION_START", "p0000:b0001": "SHARED_CONTEXT"}),
        dump_dir=tmp_path / "dump",
    )
    report = baseline.aggregate([row])
    assert report["silent_loss"] == 0
    assert report["hard_gates_passed"] is True
    assert report["candidate_diagnostics"] == 2


def test_explicit_dump_directory_keeps_new_layout_documents_separate(tmp_path) -> None:
    for name in ["first", "second"]:
        prework = tmp_path / f"{name}_prework"
        prework.mkdir()
        evidence, view = _files(prework)
        evidence = evidence.rename(prework / "evidence.json")
        baseline.run_document(
            evidence,
            view,
            lambda: _provider({"p0000:b0000": "PROBLEM_START", "p0000:b0001": "SHARED_CONTEXT"}),
            dump_dir=tmp_path / "semantic",
        )
    assert sorted(p.name for p in (tmp_path / "semantic").iterdir()) == [
        "first.semantic-run.json",
        "second.semantic-run.json",
    ]
