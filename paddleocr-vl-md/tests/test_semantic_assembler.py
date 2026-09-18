"""SPLIT anchor 解析与确定性 assembler 的离线测试。"""

from __future__ import annotations

import pathlib
import sys

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
    reconciled = {"roles": {"b0": "PROBLEM_START", "b1": "PROBLEM_CONTINUATION", "b2": "PROBLEM_START"}}
    split = sa.resolve_split("1. 一 2. 二", "2. 二")
    result = sa.assemble(
        [{"block_ref": "b0", "raw_text": "1. 一 2. 二"}], {"roles": {"b0": "PROBLEM_START"}}, splits={"b0": split}
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


# —— resolved 但无处落地的 split 必须可见（可观测性，不改判定）——


def test_resolved_split_on_solution_block_is_reported_as_not_applied() -> None:
    blocks = _blocks()
    reconciled = {"roles": {"b3": "SOLUTION_START", "b4": "SOLUTION_CONTINUATION"}}
    split = sa.resolve_split("解析内容 2. 附加题", "内容")
    assert split["status"] == "resolved" and 0 < split["start"] < len("解析内容")

    applied_to = sa.assemble(blocks, reconciled, splits={"b4": split})
    assert "split_not_applied:b4" in applied_to["diagnostics"]
    baseline = sa.assemble(blocks, reconciled)
    assert applied_to["candidates"] == baseline["candidates"], "不许改变候选结构"
    assert applied_to["candidate_count"] == baseline["candidate_count"]


def test_resolved_split_on_shared_or_non_problem_block_is_reported() -> None:
    for role in ("SHARED_CONTEXT", "NON_PROBLEM", "UNCERTAIN"):
        result = sa.assemble(
            _blocks(),
            {"roles": {"b1": role}},
            splits={"b1": {"status": "resolved", "start": 1, "end": 3}},
        )
        assert "split_not_applied:b1" in result["diagnostics"], role


def test_applied_split_does_not_emit_not_applied() -> None:
    text = "1. 一 2. 二"
    result = sa.assemble(
        [{"block_ref": "b0", "raw_text": text}],
        {"roles": {"b0": "PROBLEM_START"}},
        splits={"b0": sa.resolve_split(text, "2. 二")},
    )
    assert result["candidate_count"] == 2
    assert not any(item.startswith("split_not_applied") for item in result["diagnostics"])
    assert _ranges(result) == [
        {"block_ref": "b0", "start": 0, "end": 5},
        {"block_ref": "b0", "start": 5, "end": len(text)},
    ]


# —— anchor 落在块首/越界的 resolved split 不许物化 ——


def _ranges(result: dict) -> list[dict]:
    return [
        item
        for candidate in result["candidates"]
        for item in candidate["statement_refs"] + candidate["solution_refs"]
    ]


def test_split_anchor_at_block_head_is_not_materialised() -> None:
    """start == 0 意味着整块本就从新题开头，切出来会是 [0, 0) 的零长度引用。"""

    blocks = [{"block_ref": "b0", "raw_text": "2. 第二题"}]
    reconciled = {"roles": {"b0": "PROBLEM_START"}}
    split = sa.resolve_split("2. 第二题", "2. 第二题")
    assert split["status"] == "resolved" and split["start"] == 0

    baseline = sa.assemble(blocks, reconciled)
    result = sa.assemble(blocks, reconciled, splits={"b0": split})
    assert result["candidates"] == baseline["candidates"], "块首 anchor 不许改动候选"
    assert result["candidate_count"] == baseline["candidate_count"]
    assert "split_not_applied:b0" in result["diagnostics"]
    assert all(item["start"] < item["end"] for item in _ranges(result))


def test_split_anchor_beyond_block_end_is_not_materialised() -> None:
    blocks = [{"block_ref": "b0", "raw_text": "1. 一题"}]
    reconciled = {"roles": {"b0": "PROBLEM_START"}}
    baseline = sa.assemble(blocks, reconciled)
    out_of_range = {"status": "resolved", "start": len("1. 一题"), "end": len("1. 一题") + 2}

    result = sa.assemble(blocks, reconciled, splits={"b0": out_of_range})
    assert result["candidates"] == baseline["candidates"]
    assert "split_not_applied:b0" in result["diagnostics"]


def test_mid_anchor_still_splits_into_two_non_empty_ranges() -> None:
    """保住正常路径：中间 anchor 仍然切成前后两段，且都不为空。"""

    text = "1. 一题 2. 二题"
    result = sa.assemble(
        [{"block_ref": "b0", "raw_text": text}],
        {"roles": {"b0": "PROBLEM_START"}},
        splits={"b0": sa.resolve_split(text, "2. 二题")},
    )
    assert result["candidate_count"] == 2
    assert all(item["start"] < item["end"] for item in _ranges(result)), _ranges(result)
    assert sa.unaccounted_blocks([{"block_ref": "b0", "raw_text": text}], result) == []


def test_unresolved_split_keeps_its_own_diagnostic() -> None:
    """ambiguous / not_found 走 unresolved_split，不与 split_not_applied 混用。"""

    for status, anchor in (("ambiguous", "3. 三"), ("not_found", "9. 九")):
        result = sa.assemble(
            _blocks(),
            {"roles": {"b0": "PROBLEM_START"}},
            splits={"b0": sa.resolve_split("3. 三 3. 三", anchor)},
        )
        expected = f"unresolved_split:b0:{status}"
        assert expected in result["diagnostics"], status
        assert not any(item.startswith("split_not_applied") for item in result["diagnostics"]), status


def test_split_on_unknown_block_ref_is_reported() -> None:
    result = sa.assemble(
        _blocks(), {"roles": {"b0": "PROBLEM_START"}},
        splits={"b9": {"status": "resolved", "start": 0, "end": 2}},
    )
    assert "split_not_applied:b9" in result["diagnostics"]
    assert sa.unaccounted_blocks(_blocks(), result) == []
