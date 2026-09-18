"""离线重放审计器自身的回归：它是我们对冻结 baseline 报数的唯一口径，不能算错。"""

from __future__ import annotations

import json
import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR / "tools"))

import semantic_replay_audit as audit  # noqa: E402


def _payload() -> dict:
    """一份最小可信的 ``semantic-run.json``：2 块题面 + 1 块解析，外加一个无处落地的 split。"""

    blocks = [
        {"block_ref": "p0001:b0001", "raw_text": "1. 求 x"},
        {"block_ref": "p0001:b0002", "raw_text": "其中 x 为正整数"},
        {"block_ref": "p0001:b0003", "raw_text": "解：x=2。2. 第二题"},
    ]
    roles = {
        "p0001:b0001": "PROBLEM_START",
        "p0001:b0002": "PROBLEM_CONTINUATION",
        "p0001:b0003": "SOLUTION_START",
    }
    prediction = {
        "schema": "md-prework/semantic-prediction/v1",
        "window_id": "w0000",
        "roles": [{"block_ref": ref, "role": role} for ref, role in roles.items()],
        "boundaries": [
            {
                "left_ref": blocks[i]["block_ref"],
                "right_ref": blocks[i + 1]["block_ref"],
                "relation": "SAME_PROBLEM",
            }
            for i in range(len(blocks) - 1)
        ],
        "splits": [],
        "uncertain": [],
    }
    return {
        "document_id": "doc",
        "windows": [{"window_id": "w0000", "blocks": blocks}],
        "predictions": {"w0000": prediction},
        "splits": {
            "p0001:b0003": {"status": "resolved", "start": 5, "end": 9},
            "p0001:b0002": {"status": "not_found"},
            "p0009:b9999": {"status": "resolved", "start": 0, "end": 3},
        },
    }


def test_replay_counts_silent_loss_and_unapplied_splits(tmp_path: pathlib.Path) -> None:
    run_file = tmp_path / "doc_prework" / "semantic"
    run_file.mkdir(parents=True)
    (run_file / "semantic-run.json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
    )

    files = audit.iter_run_files(tmp_path)
    assert [path.name for path in files] == ["semantic-run.json"]
    row = audit.audit_run(files[0])
    assert row["blocks"] == 3
    assert row["candidates"] == 1
    assert row["silent_loss"] == 0, row["silent_loss_refs"]
    assert row["resolved_splits"] == 2
    assert row["split_not_applied"] == 2
    assert row["split_not_applied_refs"] == ["p0001:b0003", "p0009:b9999"]
    assert row["split_not_applied_roles"] == {
        "p0001:b0003": "SOLUTION_START",
        "p0009:b9999": "NO_ROLE",
    }


def test_unapplied_split_is_observability_not_a_gate(tmp_path: pathlib.Path) -> None:
    """有 split_not_applied 但无静默丢失时仍退出 0：它不是门，只是可见性。"""

    root = tmp_path / "blocks"
    root.mkdir()
    (root / "doc.semantic-run.json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
    )
    report = tmp_path / "report.json"
    assert audit.main(["--run-dir", str(root), "--output", str(report), "--list-refs"]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    totals = payload["totals"]
    assert totals["split_not_applied"] == 2
    assert totals["split_not_applied_by_role"] == {"NO_ROLE": 1, "SOLUTION_START": 1}
    assert totals["silent_loss"] == 0
    assert totals["runs"] == 1
