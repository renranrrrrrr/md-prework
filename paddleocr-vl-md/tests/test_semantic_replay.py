"""历史产物的只读离线重组装与审计。"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from tools import replay_semantic_assembly as replay  # noqa: E402


def _fixture(tmp_path):
    evidence = tmp_path / "doc.evidence.json"
    run = tmp_path / "doc.semantic-run.json"
    evidence.write_text(
        json.dumps(
            {
                "document_id": "doc",
                "content_hash": "test-hash",
                "pages": [
                    {
                        "page_seq": 0,
                        "blocks": [
                            {"block_ref": "b0", "sequence_index": 0, "content": "1. 甲题"},
                            {"block_ref": "b1", "sequence_index": 1, "content": "共享材料"},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    run.write_text(
        json.dumps(
            {
                "document_id": "doc",
                "evidence_hash": "test-hash",
                "reconciled": {"roles": {"b0": "PROBLEM_START", "b1": "SHARED_CONTEXT"}},
                "splits": {},
                "candidates": {
                    "candidates": [
                        {
                            "statement_refs": [{"block_ref": "b0", "start": 0, "end": 5}],
                            "solution_refs": [],
                            "shared_refs": [],
                        }
                    ],
                    "excluded": [],
                    "diagnostics": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return evidence, run


def test_replay_reports_actual_before_after_refs_and_keeps_inputs_unchanged(tmp_path) -> None:
    evidence, run = _fixture(tmp_path)
    before = (evidence.read_bytes(), run.read_bytes())
    result = replay.replay_document(evidence, run)
    assert result["before"]["silent_loss_refs"] == ["b1"]
    assert result["with_recorded_splits"]["silent_loss_refs"] == []
    assert result["after"]["silent_loss_refs"] == []
    assert result["historical_loss_roles"] == {"b1": "SHARED_CONTEXT"}
    assert result["candidates"]["diagnostic_records"] == [
        {"code": "orphan_shared_context", "block_ref": "b1"}
    ]
    assert (evidence.read_bytes(), run.read_bytes()) == before


@pytest.mark.parametrize(
    "key,value,reason",
    [("evidence_hash", "wrong", "evidence_hash_mismatch"), ("document_id", "wrong", "document_id_mismatch")],
)
def test_replay_rejects_mismatched_cached_run(tmp_path, key, value, reason) -> None:
    evidence, run = _fixture(tmp_path)
    payload = json.loads(run.read_text(encoding="utf-8"))
    payload[key] = value
    run.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=reason):
        replay.replay_document(evidence, run)


def test_replay_cli_outputs_audit_without_model_calls(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path)

    def no_network(*_args, **_kwargs):
        raise AssertionError("replay must be offline")

    import socket

    monkeypatch.setattr(socket.socket, "connect", no_network)
    output = tmp_path / "report.json"
    assert replay.main(["--evidence-dir", str(tmp_path), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["original_silent_loss"] == 1
    assert report["replayed_silent_loss"] == 0
    assert report["manual_review_records"] == 1


def test_replay_cli_cannot_overwrite_evidence(tmp_path) -> None:
    evidence, _run = _fixture(tmp_path)
    original = evidence.read_bytes()
    with pytest.raises(SystemExit) as exc:
        replay.main(["--evidence-dir", str(tmp_path), "--output", str(evidence)])
    assert exc.value.code == 2
    assert evidence.read_bytes() == original


def test_replay_cli_missing_run_is_not_a_success(tmp_path) -> None:
    _evidence, run = _fixture(tmp_path)
    run.unlink()
    output = tmp_path / "report.json"
    assert replay.main(["--evidence-dir", str(tmp_path), "--output", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["complete"] is False and report["hard_gates_passed"] is False
    assert report["documents_detail"][0]["status"] == "error"


def test_replay_cli_empty_input_is_not_a_success(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        replay.main(["--evidence-dir", str(tmp_path), "--output", str(tmp_path / "report.json")])
    assert exc.value.code == 2


def test_replay_rechecks_conflicts_in_cached_predictions(tmp_path) -> None:
    evidence, run = _fixture(tmp_path)
    source = json.loads(evidence.read_text(encoding="utf-8"))
    source["pages"][0]["blocks"][0]["content"] = "1. 甲 2. 乙 3. 丙"
    evidence.write_text(json.dumps(source), encoding="utf-8")
    cached = json.loads(run.read_text(encoding="utf-8"))
    cached["splits"] = {"b0": {"status": "resolved", "start": 5, "end": 9}}
    cached["predictions"] = {
        str(i): {"splits": [{"block_ref": "b0", "anchor": anchor}]}
        for i, anchor in enumerate(["2. 乙", "3. 丙"])
    }
    run.write_text(json.dumps(cached), encoding="utf-8")
    result = replay.replay_document(evidence, run)
    assert result["split_source"] == "cached_predictions"
    assert result["splits"]["b0"]["status"] == "conflict"
    assert "unresolved_split:b0:conflict" in result["candidates"]["diagnostics"]
    assert result["after"]["silent_loss_refs"] == []
