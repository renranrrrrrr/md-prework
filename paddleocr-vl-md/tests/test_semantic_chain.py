"""零模型端到端链的九个场景（GPT 点名）。"""

from __future__ import annotations

import json
import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_chain as chain  # noqa: E402
import semantic_prediction as sp  # noqa: E402


class ScriptedProvider:
    """把决策函数包成合法预测（由 FakeSemanticProvider 负责校验）。"""

    def __init__(self, script):
        self.script = script

    def predict(self, window):
        return sp.FakeSemanticProvider(self.script).predict(window)


def _document(texts: list[tuple[str, str]]) -> dict:
    blocks = []
    for index, (label, text) in enumerate(texts):
        blocks.append(
            {
                "block_ref": f"p0000:b{index:04d}",
                "label": label,
                "raw_text": text,
                "normalized_text": text,
                "normalization_status": "changed",
                "page_seq": 0,
                "bbox": [0, 0, 10, 10],
                "sequence_index": index,
                "signals": [f"label:{label}"],
            }
        )
    return {"document_id": "doc", "evidence_hash": "h", "normalizer_version": "v1", "blocks": blocks}


def _roles(window, roles):
    refs = [block["block_ref"] for block in window["blocks"]]
    assert len(refs) == len(roles)
    return {
        "roles": [{"block_ref": ref, "role": role} for ref, role in zip(refs, roles)],
        "boundaries": [
            {"left_ref": refs[i], "right_ref": refs[i + 1], "relation": "SAME_PROBLEM"}
            for i in range(len(refs) - 1)
        ],
        "splits": [],
        "uncertain": [],
    }


def test_scenario_normal_and_cross_block_problem() -> None:
    document = _document([("number", "1. 甲题"), ("text", "甲题续写"), ("number", "2. 乙题")])
    script = lambda w: _roles(w, ["PROBLEM_START", "PROBLEM_CONTINUATION", "PROBLEM_START"][: len(w["blocks"])])
    result = chain.run_chain(document, ScriptedProvider(script), window_size=3)
    assert result["candidates"]["candidate_count"] == 2
    assert result["reconciled"]["has_conflict"] is False


def test_scenario_two_problems_in_one_block_via_split() -> None:
    document = _document([("text", "1. 甲题 2. 乙题")])

    def script(window):
        refs = [block["block_ref"] for block in window["blocks"]]
        return {
            "roles": [{"block_ref": ref, "role": "PROBLEM_START"} for ref in refs],
            "boundaries": [],
            "splits": [{"block_ref": refs[0], "anchor": "2. 乙题"}],
            "uncertain": [],
        }

    result = chain.run_chain(document, ScriptedProvider(script), window_size=1)
    assert result["candidates"]["candidate_count"] == 2
    assert result["splits"]["p0000:b0000"]["status"] == "resolved"


def test_scenario_overlap_agreement() -> None:
    document = _document([("text", f"块{i}") for i in range(6)])
    # 角色只看块的绝对位置，因此重叠块在不同窗口里结论一致（无冲突）
    script = lambda w: _roles(
        w,
        [
            "PROBLEM_START" if block["sequence_index"] == 0 else "PROBLEM_CONTINUATION"
            for block in w["blocks"]
        ],
    )
    result = chain.run_chain(document, ScriptedProvider(script), window_size=5, stride=3)
    assert result["reconciled"]["has_conflict"] is False
    # 无冲突、无 UNCERTAIN；只可能因为首/尾是续写而要求扩窗（这是设计行为）
    assert all(
        set(reasons) <= {"HEAD_CONTINUATION", "TAIL_CONTINUATION"}
        for reasons in result["expansion"].values()
    )


def test_scenario_overlap_conflict_triggers_expansion() -> None:
    document = _document([("text", f"块{i}") for i in range(6)])

    def script(window):
        refs = [block["block_ref"] for block in window["blocks"]]
        first = "PROBLEM_START" if window["window_id"] == "w0000" else "SOLUTION_START"
        return {
            "roles": [{"block_ref": refs[0], "role": first}]
            + [{"block_ref": ref, "role": "PROBLEM_CONTINUATION"} for ref in refs[1:]],
            "boundaries": [
                {"left_ref": refs[i], "right_ref": refs[i + 1], "relation": "SAME_PROBLEM"}
                for i in range(len(refs) - 1)
            ],
            "splits": [],
            "uncertain": [],
        }

    result = chain.run_chain(document, ScriptedProvider(script), window_size=5, stride=3)
    assert result["reconciled"]["has_conflict"] is True
    assert any(
        "OVERLAP_CONFLICT" in reasons or "TAIL_CONTINUATION" in reasons
        for reasons in result["expansion"].values()
    )


def test_scenario_uncertain_triggers_expansion() -> None:
    document = _document([("text", f"块{i}") for i in range(5)])

    def script(window):
        refs = [block["block_ref"] for block in window["blocks"]]
        return {
            "roles": [{"block_ref": refs[0], "role": "PROBLEM_START"}]
            + [{"block_ref": ref, "role": "PROBLEM_CONTINUATION"} for ref in refs[1:-1]]
            + [{"block_ref": refs[-1], "role": "PROBLEM_START"}],
            "boundaries": [
                {"left_ref": refs[i], "right_ref": refs[i + 1], "relation": "SAME_PROBLEM"}
                for i in range(len(refs) - 1)
            ],
            "splits": [],
            "uncertain": [{"kind": "ROLE", "refs": [refs[1]], "reason": "看不清"}],
        }

    result = chain.run_chain(document, ScriptedProvider(script), window_size=5)
    assert "MODEL_UNCERTAIN" in result["expansion"]["w0000"]


def test_scenario_fatal_block_still_uses_raw_text() -> None:
    document = _document([("text", "1. 甲题")])
    document["blocks"][0]["normalization_status"] = "fatal"
    document["blocks"][0]["normalized_text"] = None
    result = chain.run_chain(
        document, ScriptedProvider(lambda w: _roles(w, ["PROBLEM_START"])), window_size=1
    )
    block = result["windows"][0]["blocks"][0]
    assert block["normalization_status"] == "fatal" and block["normalized_text"] is None
    assert block["raw_text"] == "1. 甲题"


def test_scenario_duplicate_anchor_is_not_auto_split() -> None:
    document = _document([("text", "3. 三 3. 三")])

    def script(window):
        refs = [block["block_ref"] for block in window["blocks"]]
        return {
            "roles": [{"block_ref": refs[0], "role": "PROBLEM_START"}],
            "boundaries": [],
            "splits": [{"block_ref": refs[0], "anchor": "3. 三"}],
            "uncertain": [],
        }

    result = chain.run_chain(document, ScriptedProvider(script), window_size=1)
    assert result["splits"]["p0000:b0000"]["status"] == "ambiguous"
    assert result["candidates"]["candidate_count"] == 1


def test_scenario_chain_is_byte_identical() -> None:
    document = _document([("number", "1. 甲"), ("text", "续"), ("number", "2. 乙"), ("text", "续乙")])
    script = lambda w: _roles(
        w,
        ["PROBLEM_START", "PROBLEM_CONTINUATION", "PROBLEM_START", "PROBLEM_CONTINUATION"][: len(w["blocks"])],
    )
    first = json.dumps(
        chain.run_chain(document, ScriptedProvider(script), window_size=3), ensure_ascii=False, sort_keys=True
    )
    second = json.dumps(
        chain.run_chain(document, ScriptedProvider(script), window_size=3), ensure_ascii=False, sort_keys=True
    )
    assert first == second
