"""``tools/semantic_split_audit.py`` 的回归：split 溯源的口径必须自己站得住。

真实 18 份 baseline 的数字由这个工具产出，所以"谁是第一提案窗口""final role 从哪来"
"applied/not_applied 怎么判"都必须有离线样本钉住。零模型调用。
"""

from __future__ import annotations

import json
import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOL_DIR / "tools"))

import semantic_split_audit as split_audit  # noqa: E402


def _prediction(window_id: str, roles: dict[str, str], splits: list[tuple[str, str]]) -> dict:
    refs = list(roles)
    return {
        "schema": "md-prework/semantic-prediction/v1",
        "window_id": window_id,
        "roles": [{"block_ref": ref, "role": role} for ref, role in roles.items()],
        "boundaries": [
            {
                "left_ref": refs[i],
                "right_ref": refs[i + 1],
                "relation": "SAME_PROBLEM",
            }
            for i in range(len(refs) - 1)
        ],
        "splits": [{"block_ref": ref, "anchor": anchor} for ref, anchor in splits],
        "uncertain": [],
    }


B0 = {"block_ref": "p0000:b0000", "label": "number", "raw_text": "1. 甲题"}
B1 = {"block_ref": "p0000:b0001", "label": "text", "raw_text": "题干续 2. 乙题"}
B2 = {"block_ref": "p0000:b0002", "label": "number", "raw_text": "3. 丙题"}
B3 = {"block_ref": "p0000:b0003", "label": "text", "raw_text": "作答说明 附题"}
B4 = {"block_ref": "p0000:b0004", "label": "text", "raw_text": "4. 丁题"}


def _payload() -> dict:
    """两个重叠窗口：w0000=[b0000..b0003]、w0001=[b0002..b0004]，b0002/b0003 落在重叠区。"""

    w0 = [B0, B1, B2, B3]
    w1 = [B2, B3, B4]
    roles_w0 = {
        "p0000:b0000": "PROBLEM_START",
        "p0000:b0001": "PROBLEM_CONTINUATION",
        "p0000:b0002": "PROBLEM_START",
        "p0000:b0003": "SHARED_CONTEXT",
    }
    roles_w1 = {
        "p0000:b0002": "PROBLEM_CONTINUATION",
        "p0000:b0003": "PROBLEM_CONTINUATION",
        "p0000:b0004": "PROBLEM_START",
    }
    # 归档 reconciled：重叠块 b0002/b0003 两个窗口结论不同 → 当时降级为 UNCERTAIN
    archived_roles = {
        "p0000:b0000": "PROBLEM_START",
        "p0000:b0001": "PROBLEM_CONTINUATION",
        "p0000:b0002": "PROBLEM_START",
        "p0000:b0003": "UNCERTAIN",
        "p0000:b0004": "PROBLEM_START",
    }
    return {
        "document_id": "doc",
        "windows": [
            {"window_id": "w0000", "blocks": w0},
            {"window_id": "w0001", "blocks": w1},
        ],
        "predictions": {
            "w0000": _prediction(
                "w0000",
                roles_w0,
                [
                    ("p0000:b0001", "2. 乙题"),  # 块中间 → 可切
                    ("p0000:b0002", "3. 丙题"),  # 块首 → 切不出块内第二题
                    # 同一窗口对同一块再提一次同样的 anchor：提案数 ≠ 提案窗口数
                    ("p0000:b0001", "2. 乙题"),
                ],
            ),
            "w0001": _prediction(
                "w0001",
                roles_w1,
                [
                    ("p0000:b0002", "丙题"),  # 同一块，另一个 anchor（first-one-wins 取 w0000）
                    ("p0000:b0003", "附题"),  # 归档 final role 是 UNCERTAIN
                    ("p0000:b0004", "不存在的锚"),  # not_found，不进审计口径
                ],
            ),
        },
        "reconciled": {
            "roles": archived_roles,
            "boundaries": {},
            "role_conflicts": {
                "p0000:b0003": ["PROBLEM_CONTINUATION", "SHARED_CONTEXT"]
            },
            "boundary_conflicts": {},
            "has_conflict": True,
        },
        # chain 的 first-one-wins 结果（按 predictions 顺序）
        "splits": {
            "p0000:b0001": {"status": "resolved", "start": 4, "end": 9},
            "p0000:b0002": {"status": "resolved", "start": 0, "end": 5},
            "p0000:b0003": {"status": "resolved", "start": 5, "end": 7},
            "p0000:b0004": {"status": "not_found"},
        },
    }


def _write(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "doc.semantic-run.json"
    path.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    return path


def test_entry_carries_every_provenance_field(tmp_path: pathlib.Path) -> None:
    run = split_audit.audit_run(_write(tmp_path))
    by_ref = {entry["block_ref"]: entry for entry in run["splits"]}
    assert sorted(by_ref) == ["p0000:b0001", "p0000:b0002", "p0000:b0003"], "只审计 resolved"
    assert run["archived_split_statuses"]["p0000:b0004"] == "not_found"

    mid = by_ref["p0000:b0001"]
    assert mid["outcome"] == "applied"
    assert mid["placement"] == "statement"
    assert mid["first_proposer_window"] == "w0000"
    assert mid["source_role"] == "PROBLEM_CONTINUATION"
    assert mid["final_role"] == "PROBLEM_CONTINUATION"
    assert mid["proposer_count"] == 2 and mid["distinct_anchors"] == 1
    assert mid["proposer_window_count"] == 1, "两条提案都来自 w0000"
    assert mid["anchor"] == "2. 乙题"
    assert mid["covering_windows"] == [
        {"window_id": "w0000", "role": "PROBLEM_CONTINUATION"}
    ]
    assert mid["role_conflict"] is False
    assert mid["anchor_reproduces_archived_range"] is True

    head = by_ref["p0000:b0002"]
    assert head["outcome"] == "not_applied", "anchor 在块首，切不出块内第二题"
    assert head["placement"] == "statement", "块本身仍进了题面，只是没被切开"
    assert head["proposer_count"] == 2 and head["distinct_anchors"] == 2
    assert head["proposer_window_count"] == 2, "w0000 与 w0001 各提了一个 anchor"
    assert head["first_proposer_window"] == "w0000", "first-one-wins：归档顺序里的第一个提案"
    assert head["anchor"] == "3. 丙题"
    assert head["source_role"] == "PROBLEM_START"
    assert head["role_conflict"] is False, "归档 role_conflicts 没记它，就按归档说"
    assert head["covering_windows"] == [
        {"window_id": "w0000", "role": "PROBLEM_START"},
        {"window_id": "w0001", "role": "PROBLEM_CONTINUATION"},
    ]
    assert [p["role"] for p in head["proposers"]] == ["PROBLEM_START", "PROBLEM_CONTINUATION"]

    uncertain = by_ref["p0000:b0003"]
    assert uncertain["outcome"] == "not_applied"
    assert uncertain["source_role"] == "PROBLEM_CONTINUATION", "第一提案窗口是 w0001"
    assert uncertain["final_role"] == "UNCERTAIN", "final role 读归档 reconciled，不重算"
    assert uncertain["role_conflict"] is True
    assert uncertain["placement"] == "excluded"


def test_summaries_count_from_the_entries(tmp_path: pathlib.Path) -> None:
    totals = split_audit.summarize([split_audit.audit_run(_write(tmp_path))])
    assert totals["resolved"] == 3
    assert totals["applied"] == 1
    assert totals["not_applied"] == 2
    assert totals["not_applied_source_role"] == {
        "PROBLEM_START": 1,
        "PROBLEM_CONTINUATION": 1,
    }
    assert totals["not_applied_source_problem_final_uncertain"] == 1
    assert totals["not_applied_source_problem_final_solution"] == 0
    assert totals["not_applied_source_solution"] == 0
    assert totals["not_applied_source_uncertain"] == 0
    assert totals["multi_proposal_blocks"] == 2
    assert totals["multi_proposal_conflicting_anchor"] == 1
    assert totals["multi_proposal_same_anchor"] == 1
    assert totals["multi_window_proposal_blocks"] == 1
    assert totals["single_window_multi_proposal_blocks"] == 1
    assert totals["role_conflict_splits"] == 1  # 归档 role_conflicts 只记了 b0003
    assert totals["no_role_conflict_but_not_applied"] == 1
    assert totals["anchor_reproduces_archived_range"] == 3
    assert totals["anchor_reconstruction_mismatch"] == 0
    assert totals["placement_of_not_applied"] == {"statement": 1, "excluded": 1}


def test_final_role_default_stays_frozen(tmp_path: pathlib.Path) -> None:
    """默认读归档；显式重算时 b0003 会被当前 reconcile 降级、b0002 结论也随之改变。"""

    path = _write(tmp_path)
    frozen = split_audit.audit_run(path)
    recomputed = split_audit.audit_run(path, recompute_reconcile=True)

    def entries(run: dict) -> dict:
        return {entry["block_ref"]: entry for entry in run["splits"]}

    assert entries(frozen)["p0000:b0002"]["final_role"] == "PROBLEM_START"
    assert entries(recomputed)["p0000:b0002"]["final_role"] == "UNCERTAIN"
    assert entries(frozen)["p0000:b0003"]["outcome"] == "not_applied"
    assert entries(recomputed)["p0000:b0003"]["outcome"] == "not_applied"


def test_cli_writes_json_and_markdown_reports(tmp_path: pathlib.Path) -> None:
    _write(tmp_path)
    json_out = tmp_path / "out" / "split_audit.json"
    md_out = tmp_path / "out" / "split_audit.md"
    assert split_audit.main(
        ["--run-dir", str(tmp_path), "--output", str(json_out), "--markdown", str(md_out)]
    ) == 0
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["totals"]["resolved"] == 3
    assert len(report["runs"]) == 1
    markdown = md_out.read_text(encoding="utf-8")
    assert "## 分类抽样" in markdown
    assert "p0000:b0003" in markdown
    assert "first-one-wins" not in markdown  # 抽样只贴事实，不贴实现细节
