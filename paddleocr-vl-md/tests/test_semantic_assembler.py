"""SPLIT anchor 解析与确定性 assembler 的离线测试。"""

from __future__ import annotations

import pathlib
import sys
import subprocess
from copy import deepcopy
from itertools import product

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_assembler as sa  # noqa: E402


def _blocks() -> list[dict]:
    return [
        {"block_ref": "b0", "label": "number", "raw_text": "1. 第一题"},
        {"block_ref": "b1", "label": "text", "raw_text": "续写"},
        {"block_ref": "b2", "label": "text", "raw_text": "2. 第二题 3. 第三题"},
        {"block_ref": "b3", "label": "paragraph_title", "raw_text": "参考答案"},
        {"block_ref": "b4", "label": "text", "raw_text": "解析内容"},
    ]


def test_resolve_split_unique_and_ambiguous() -> None:
    assert sa.resolve_split("2. 第二题 3. 第三题", "3. 第三题")["status"] == "resolved"
    assert sa.resolve_split("3. 三 3. 三", "3. 三")["status"] == "ambiguous"
    assert sa.resolve_split("abc", "zzz")["status"] == "not_found"


def test_assemble_builds_candidates_with_refs_only() -> None:
    reconciled = {
        "roles": {
            "b0": "PROBLEM_START",
            "b1": "PROBLEM_CONTINUATION",
            "b2": "PROBLEM_START",
            "b3": "SOLUTION_START",
            "b4": "SOLUTION_CONTINUATION",
        }
    }
    result = sa.assemble(_blocks(), reconciled)
    assert result["candidate_count"] == 2
    first, second = result["candidates"]
    assert [item["block_ref"] for item in first["statement_refs"]] == ["b0", "b1"]
    assert [item["block_ref"] for item in second["statement_refs"]] == ["b2"]
    assert [item["block_ref"] for item in second["solution_refs"]] == ["b3", "b4"]
    assert "raw_text" not in str(result), "候选只保存引用，不复制正文"


def test_split_inside_block_creates_second_candidate() -> None:
    split = sa.resolve_split("1. 一 2. 二", "2. 二")
    result = sa.assemble(
        [{"block_ref": "b0", "raw_text": "1. 一 2. 二"}],
        {"roles": {"b0": "PROBLEM_START"}},
        splits={"b0": split},
    )
    assert result["candidate_count"] == 2
    assert result["candidates"][1]["statement_refs"][0]["start"] == split["start"]


def test_ambiguous_split_is_reported_not_guessed() -> None:
    split = sa.resolve_split("3. 三 3. 三", "3. 三")
    result = sa.assemble(
        [{"block_ref": "b0", "raw_text": "3. 三 3. 三"}],
        {"roles": {"b0": "PROBLEM_START"}},
        splits={"b0": split},
    )
    assert result["candidate_count"] == 1, "ambiguous 不得默认切分"
    assert any(item.startswith("unresolved_split:b0") for item in result["diagnostics"])


def test_orphans_and_shared_context() -> None:
    reconciled = {
        "roles": {
            "b0": "SOLUTION_START",
            "b1": "SHARED_CONTEXT",
            "b2": "PROBLEM_START",
            "b3": "UNCERTAIN",
        }
    }
    result = sa.assemble(_blocks(), reconciled)
    assert any(item.startswith("orphan_solution:b0") for item in result["diagnostics"])
    assert any(item.startswith("uncertain_role:b3") for item in result["diagnostics"])
    assert result["candidates"][0]["shared_refs"] == ["b1"], "共享材料归属到其后第一道题"


def test_assembly_is_deterministic() -> None:
    reconciled = {"roles": {"b0": "PROBLEM_START", "b1": "PROBLEM_CONTINUATION"}}
    blocks = _blocks()[:2]
    assert sa.assemble(blocks, reconciled) == sa.assemble(blocks, reconciled)


def _input(roles: list[str], texts: list[str] | None = None) -> tuple[list[dict], dict]:
    blocks = [
        {"block_ref": f"p0000:b{i:04d}", "raw_text": text, "page_seq": i // 2}
        for i, text in enumerate(texts or [f"块{i}" for i in range(len(roles))])
    ]
    return blocks, {"roles": {block["block_ref"]: role for block, role in zip(blocks, roles)}}


def _assert_accounted(blocks: list[dict], result: dict) -> None:
    # 独立于生产 audit 的覆盖检查，避免 assembler 与 gate 共享同一种错误。
    accounted = {item["block_ref"] for item in result["excluded"]}
    accounted.update(item["block_ref"] for item in result["diagnostic_records"])
    for candidate in result["candidates"]:
        accounted.update(candidate["shared_refs"])
        accounted.update(
            item["block_ref"] for item in candidate["statement_refs"] + candidate["solution_refs"]
        )
    assert {block["block_ref"] for block in blocks} <= accounted


@pytest.mark.parametrize(
    "roles",
    [
        ["PROBLEM_START", "SHARED_CONTEXT"],
        ["SHARED_CONTEXT"],
        ["SHARED_CONTEXT", "SHARED_CONTEXT"],
        ["SHARED_CONTEXT", "PROBLEM_START", "SHARED_CONTEXT", "NON_PROBLEM", "UNCERTAIN"],
        ["PROBLEM_START", "SHARED_CONTEXT", "SOLUTION_START"],
    ],
)
def test_trailing_shared_context_is_registered_without_guessing(roles: list[str]) -> None:
    blocks, reconciled = _input(roles)
    before = deepcopy((blocks, reconciled))
    result = sa.assemble(blocks, reconciled)
    _assert_accounted(blocks, result)
    expected = [
        block["block_ref"] for block in blocks if reconciled["roles"][block["block_ref"]] == "SHARED_CONTEXT"
    ]
    attached = {ref for c in result["candidates"] for ref in c["shared_refs"]}
    orphaned = {
        item["block_ref"] for item in result["diagnostic_records"] if item["code"] == "orphan_shared_context"
    }
    assert orphaned == set(expected) - attached
    assert (blocks, reconciled) == before


@pytest.mark.parametrize(
    "role,code",
    [
        ("PROBLEM_CONTINUATION", "orphan_continuation"),
        ("SOLUTION_START", "orphan_solution"),
        ("SOLUTION_CONTINUATION", "orphan_solution"),
    ],
)
def test_orphans_have_exact_block_diagnostics(role: str, code: str) -> None:
    blocks, reconciled = _input([role, "SHARED_CONTEXT"])
    result = sa.assemble(blocks, reconciled)
    assert {"code": code, "block_ref": blocks[0]["block_ref"]} in result["diagnostic_records"]
    assert result["candidate_count"] == 0
    _assert_accounted(blocks, result)


def test_cross_page_continuations_and_solutions_keep_their_owner() -> None:
    blocks, reconciled = _input(
        [
            "PROBLEM_START",
            "PROBLEM_CONTINUATION",
            "PROBLEM_CONTINUATION",
            "SOLUTION_START",
            "SOLUTION_CONTINUATION",
        ]
    )
    result = sa.assemble(blocks, reconciled)
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert [r["block_ref"] for r in candidate["statement_refs"]] == [b["block_ref"] for b in blocks[:3]]
    assert [r["block_ref"] for r in candidate["solution_refs"]] == [b["block_ref"] for b in blocks[3:]]
    _assert_accounted(blocks, result)


def test_split_preserves_document_order_and_moves_following_content_once() -> None:
    texts = ["1. 甲题 2. 乙题", "乙题续写", "乙题解析", "3. 丙题", "丙题解析"]
    blocks, reconciled = _input(
        ["PROBLEM_START", "PROBLEM_CONTINUATION", "SOLUTION_START", "PROBLEM_START", "SOLUTION_START"], texts
    )
    ref = blocks[0]["block_ref"]
    split = sa.resolve_split(texts[0], "2. 乙题")
    result = sa.assemble(blocks, reconciled, splits={ref: split})
    first, second, third = result["candidates"]
    assert [c["number"]["number"] for c in result["candidates"]] == ["1", "2", "3"]
    assert [c["candidate_id"] for c in result["candidates"]] == ["pc0001", "pc0002", "pc0003"]
    assert first["statement_refs"] == [{"block_ref": ref, "start": 0, "end": split["start"]}]
    assert first["solution_refs"] == []
    assert [r["block_ref"] for r in second["statement_refs"]] == [ref, blocks[1]["block_ref"]]
    assert [r["block_ref"] for r in second["solution_refs"]] == [blocks[2]["block_ref"]]
    assert [r["block_ref"] for r in third["solution_refs"]] == [blocks[4]["block_ref"]]
    pieces = [
        texts[0][r["start"] : r["end"]]
        for c in result["candidates"]
        for r in c["statement_refs"]
        if r["block_ref"] == ref
    ]
    assert "".join(pieces) == texts[0]


def test_split_in_continuation_keeps_earlier_solution_on_earlier_candidate() -> None:
    texts = ["1. 甲题", "甲题解析", "甲题补充 2. 乙题", "乙题解析"]
    blocks, reconciled = _input(
        ["PROBLEM_START", "SOLUTION_START", "PROBLEM_CONTINUATION", "SOLUTION_START"], texts
    )
    result = sa.assemble(
        blocks, reconciled, splits={blocks[2]["block_ref"]: sa.resolve_split(texts[2], "2. 乙题")}
    )
    first, second = result["candidates"]
    assert [r["block_ref"] for r in first["solution_refs"]] == [blocks[1]["block_ref"]]
    assert [r["block_ref"] for r in second["solution_refs"]] == [blocks[3]["block_ref"]]


def test_multiple_split_blocks_do_not_depend_on_mapping_order() -> None:
    texts = ["1. 甲 2. 乙", "乙续 3. 丙", "丙续"]
    blocks, reconciled = _input(["PROBLEM_START", "PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION"], texts)
    splits = {
        blocks[i]["block_ref"]: sa.resolve_split(texts[i], anchor)
        for i, anchor in enumerate(["2. 乙", "3. 丙"])
    }
    result = sa.assemble(blocks, reconciled, splits=splits)
    assert result == sa.assemble(blocks, reconciled, splits=dict(reversed(list(splits.items()))))
    assert [c["number"]["number"] for c in result["candidates"]] == ["1", "2", "3"]
    assert [r["block_ref"] for r in result["candidates"][-1]["statement_refs"]] == [
        blocks[1]["block_ref"],
        blocks[2]["block_ref"],
    ]


def test_pending_shared_context_can_be_consumed_by_split_created_problem() -> None:
    blocks, reconciled = _input(
        ["PROBLEM_START", "SHARED_CONTEXT", "PROBLEM_CONTINUATION"],
        ["1. 甲题", "共享材料", "甲题末尾 2. 乙题"],
    )
    result = sa.assemble(
        blocks,
        reconciled,
        splits={blocks[2]["block_ref"]: sa.resolve_split(blocks[2]["raw_text"], "2. 乙题")},
    )
    assert result["candidate_count"] == 2
    assert result["candidates"][1]["shared_refs"] == [blocks[1]["block_ref"]]
    assert result["diagnostic_records"] == []
    _assert_accounted(blocks, result)


def test_missing_and_unknown_roles_remain_auditable() -> None:
    blocks, reconciled = _input(["UNKNOWN_ROLE", "NON_PROBLEM", "PROBLEM_START"])
    reconciled["roles"].pop(blocks[2]["block_ref"])
    result = sa.assemble(blocks, reconciled)
    assert len(result["excluded"]) == 3
    assert {d["code"] for d in result["diagnostic_records"]} == {"unknown_role", "uncertain_role"}
    _assert_accounted(blocks, result)


@pytest.mark.parametrize(
    "start,end",
    [(-1, 2), (3, 3), (4, 3), (5, 6), (2, 6), (True, 2), (1.5, 3), ("2", 3), (None, 3), (2, None)],
)
def test_invalid_split_offsets_keep_the_full_original_range(start, end) -> None:
    blocks, reconciled = _input(["PROBLEM_START"], ["12345"])
    ref = blocks[0]["block_ref"]
    result = sa.assemble(blocks, reconciled, splits={ref: {"status": "resolved", "start": start, "end": end}})
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["statement_refs"] == [{"block_ref": ref, "start": 0, "end": 5}]
    assert any(d["code"] == "invalid_split" and d["block_ref"] == ref for d in result["diagnostic_records"])


def test_zero_offset_split_cannot_loop_or_create_empty_candidates() -> None:
    # 旧版会边遍历 candidates 边无限追加；用子进程超时，避免回归时挂住整个测试进程。
    code = """
import semantic_assembler as sa
result = sa.assemble([{'block_ref': 'b', 'raw_text': '甲乙'}], {'roles': {'b': 'PROBLEM_START'}}, splits={'b': {'status': 'resolved', 'start': 0, 'end': 1}})
assert result['candidate_count'] == 1
assert result['candidates'][0]['statement_refs'] == [{'block_ref': 'b', 'start': 0, 'end': 2}]
assert result['diagnostic_records'] == [{'code': 'invalid_split', 'block_ref': 'b'}]
"""
    subprocess.run([sys.executable, "-c", code], cwd=TOOL_DIR, timeout=5, check=True, capture_output=True)


def test_split_at_last_character_has_no_gap_or_overlap() -> None:
    blocks, reconciled = _input(["PROBLEM_START"], ["甲乙"])
    ref = blocks[0]["block_ref"]
    result = sa.assemble(blocks, reconciled, splits={ref: sa.resolve_split("甲乙", "乙")})
    assert [c["statement_refs"] for c in result["candidates"]] == [
        [{"block_ref": ref, "start": 0, "end": 1}],
        [{"block_ref": ref, "start": 1, "end": 2}],
    ]


@pytest.mark.parametrize(
    "role", ["SHARED_CONTEXT", "SOLUTION_START", "NON_PROBLEM", "UNCERTAIN", "PROBLEM_CONTINUATION"]
)
def test_unapplied_split_is_diagnosed(role: str) -> None:
    blocks, reconciled = _input([role], ["甲乙"])
    ref = blocks[0]["block_ref"]
    result = sa.assemble(blocks, reconciled, splits={ref: sa.resolve_split("甲乙", "乙")})
    assert any(d["code"] == "unapplied_split" and d["block_ref"] == ref for d in result["diagnostic_records"])
    _assert_accounted(blocks, result)


def test_split_for_absent_block_is_diagnosed() -> None:
    result = sa.assemble([], {}, splits={"missing": {"status": "resolved", "start": 1, "end": 2}})
    assert any(
        d["code"] == "split_unknown_block" and d["block_ref"] == "missing"
        for d in result["diagnostic_records"]
    )


def test_all_short_role_sequences_account_for_every_original_block() -> None:
    roles = [
        "PROBLEM_START",
        "PROBLEM_CONTINUATION",
        "SOLUTION_START",
        "SOLUTION_CONTINUATION",
        "SHARED_CONTEXT",
        "NON_PROBLEM",
        "UNCERTAIN",
        "UNKNOWN_ROLE",
    ]
    for length in range(5):
        for sequence in product(roles, repeat=length):
            blocks, reconciled = _input(list(sequence))
            result = sa.assemble(blocks, reconciled)
            _assert_accounted(blocks, result)


def test_coverage_audit_does_not_accept_prefix_collisions_in_legacy_diagnostics() -> None:
    blocks = [{"block_ref": "p0000:b0001", "raw_text": "甲"}, {"block_ref": "p0000:b00010", "raw_text": "乙"}]
    old = {"candidates": [], "excluded": [], "diagnostics": ["orphan_solution:p0000:b00010"]}
    assert sa.audit_block_coverage(blocks, old)["silent_loss_refs"] == ["p0000:b0001"]
    old["diagnostics"] = ["unresolved_split:p0000:b00010:ambiguous"]
    assert sa.audit_block_coverage(blocks, old)["silent_loss_refs"] == ["p0000:b0001"]


def test_coverage_audit_checks_entire_reference_ranges() -> None:
    blocks = [{"block_ref": "b", "raw_text": "12345"}]
    candidate = {
        "statement_refs": [{"block_ref": "b", "start": 0, "end": 2}],
        "solution_refs": [],
        "shared_refs": [],
    }
    result = {"candidates": [candidate], "excluded": [], "diagnostic_records": [], "diagnostics": []}
    assert sa.audit_block_coverage(blocks, result)["silent_loss_refs"] == ["b"]
    candidate["statement_refs"].append({"block_ref": "b", "start": 2, "end": 5})
    assert sa.audit_block_coverage(blocks, result)["silent_loss_refs"] == []
    candidate["statement_refs"][0]["start"] = -1
    assert sa.audit_block_coverage(blocks, result)["invalid_references"]


def test_empty_text_block_is_still_accounted_for() -> None:
    blocks, reconciled = _input(["PROBLEM_START"], [""])
    result = sa.assemble(blocks, reconciled)
    _assert_accounted(blocks, result)
    assert sa.audit_block_coverage(blocks, result)["silent_loss_refs"] == []


@pytest.mark.parametrize(
    "blocks",
    [
        [{"raw_text": "缺少身份"}],
        [{"block_ref": None, "raw_text": "空身份"}],
        [{"block_ref": "b", "raw_text": "甲"}, {"block_ref": "b", "raw_text": "乙"}],
    ],
)
def test_invalid_source_identity_fails_explicitly(blocks) -> None:
    with pytest.raises(ValueError, match="block_ref"):
        sa.assemble(blocks, {})
    with pytest.raises(ValueError, match="block_ref"):
        sa.audit_block_coverage(blocks, {})
