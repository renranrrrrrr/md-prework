"""离线重放审计器自身的回归：它是我们对冻结 baseline 报数的唯一口径，不能算错。

最关键的一条是"默认路径必须读归档的 ``reconciled``"——否则以后 reconcile 一改，
同一份冻结 baseline 的 replay 结论就会跟着变，重放就不再是重放。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR / "tools"))

import semantic_replay_audit as audit  # noqa: E402


BLOCKS = [
    {"block_ref": "p0001:b0001", "raw_text": "1. 求 x"},
    {"block_ref": "p0001:b0002", "raw_text": "其中 x 为正整数"},
    {"block_ref": "p0001:b0003", "raw_text": "解：x=2。2. 第二题"},
]

#: 当时模型逐窗口给出的角色（重算 reconcile 会得到这套结论）
PREDICTED_ROLES = {
    "p0001:b0001": "PROBLEM_START",
    "p0001:b0002": "PROBLEM_CONTINUATION",
    "p0001:b0003": "SOLUTION_START",
}

SPLITS = {
    "p0001:b0003": {"status": "resolved", "start": 5, "end": 9},
    "p0001:b0002": {"status": "not_found"},
    "p0009:b9999": {"status": "resolved", "start": 0, "end": 3},
}


def _payload(*, archived_roles: dict[str, str] | None = None) -> dict:
    """一份最小可信的 ``semantic-run.json``；``archived_roles=None`` 表示根本不写 reconciled。"""

    prediction = {
        "schema": "md-prework/semantic-prediction/v1",
        "window_id": "w0000",
        "roles": [
            {"block_ref": ref, "role": PREDICTED_ROLES[ref]} for ref in PREDICTED_ROLES
        ],
        "boundaries": [
            {
                "left_ref": BLOCKS[i]["block_ref"],
                "right_ref": BLOCKS[i + 1]["block_ref"],
                "relation": "SAME_PROBLEM",
            }
            for i in range(len(BLOCKS) - 1)
        ],
        "splits": [],
        "uncertain": [],
    }
    payload: dict = {
        "document_id": "doc",
        "windows": [{"window_id": "w0000", "blocks": BLOCKS}],
        "predictions": {"w0000": prediction},
        "splits": SPLITS,
    }
    if archived_roles is not None:
        payload["reconciled"] = {
            "roles": dict(archived_roles),
            "boundaries": {},
            "role_conflicts": {},
            "boundary_conflicts": {},
            "has_conflict": False,
        }
    return payload


def _write(tmp_path: pathlib.Path, payload: dict) -> pathlib.Path:
    run_dir = tmp_path / "doc_prework" / "semantic"
    run_dir.mkdir(parents=True)
    path = run_dir / "semantic-run.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_replay_counts_silent_loss_and_unapplied_splits(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, _payload(archived_roles=PREDICTED_ROLES))
    files = audit.iter_run_files(tmp_path)
    assert [p.name for p in files] == ["semantic-run.json"]

    row = audit.audit_run(files[0])
    assert row["reconcile_source"] == audit.RECONCILE_ARCHIVED
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


def test_default_uses_archived_reconcile_not_current_code(tmp_path: pathlib.Path) -> None:
    """归档结论与当前 reconcile 结论**故意不同**，两条路径必须给出不同结果。"""

    # 归档里首题被降级成 UNCERTAIN（当时确有 role conflict），模型逐窗口答案却是 PROBLEM_START
    archived = dict(PREDICTED_ROLES, **{"p0001:b0001": "UNCERTAIN"})
    path = _write(tmp_path, _payload(archived_roles=archived))

    default = audit.audit_run(path)
    recomputed = audit.audit_run(path, recompute_reconcile=True)

    assert default["reconcile_source"] == audit.RECONCILE_ARCHIVED
    assert recomputed["reconcile_source"] == audit.RECONCILE_RECOMPUTED
    assert default["candidates"] == 0, "默认路径必须沿用归档的 UNCERTAIN"
    assert recomputed["candidates"] == 1, "显式重算才用当前 reconcile"
    assert default["silent_loss"] == recomputed["silent_loss"] == 0


def test_missing_archived_reconcile_fails_loudly(tmp_path: pathlib.Path) -> None:
    """缺 reconciled 时不许悄悄退回当前 reconcile。"""

    path = _write(tmp_path, _payload(archived_roles=None))
    with pytest.raises(audit.FrozenReconcileMissing):
        audit.audit_run(path)
    # 显式要求重算时才允许没有归档结论
    assert audit.audit_run(path, recompute_reconcile=True)["candidates"] == 1


def test_cli_reports_missing_reconcile_as_error(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "blocks"
    root.mkdir()
    (root / "doc.semantic-run.json").write_text(
        json.dumps(_payload(archived_roles=None), ensure_ascii=False), encoding="utf-8"
    )
    assert audit.main(["--run-dir", str(root)]) == 2


def test_unapplied_split_is_observability_not_a_gate(tmp_path: pathlib.Path) -> None:
    """有 split_not_applied 但无静默丢失时仍退出 0：它不是门，只是可见性。"""

    root = tmp_path / "blocks"
    root.mkdir()
    (root / "doc.semantic-run.json").write_text(
        json.dumps(_payload(archived_roles=PREDICTED_ROLES), ensure_ascii=False), encoding="utf-8"
    )
    report = tmp_path / "report.json"
    assert audit.main(["--run-dir", str(root), "--output", str(report), "--list-refs"]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    totals = payload["totals"]
    assert totals["reconcile_source"] == audit.RECONCILE_ARCHIVED
    assert totals["split_not_applied"] == 2
    assert totals["split_not_applied_by_role"] == {"NO_ROLE": 1, "SOLUTION_START": 1}
    assert totals["silent_loss"] == 0
    assert totals["runs"] == 1
