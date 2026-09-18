"""overlap reconcile 与自适应扩窗触发的离线测试（零模型链）。"""

from __future__ import annotations

import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_reconcile as sr  # noqa: E402


def _window(refs: list[str]) -> dict:
    return {"window_id": "w0000", "blocks": [{"block_ref": ref, "label": "text"} for ref in refs]}


def _prediction(refs: list[str], roles: list[str], relations: list[str] | None = None) -> dict:
    return {
        "roles": [{"block_ref": ref, "role": role} for ref, role in zip(refs, roles)],
        "boundaries": [
            {"left_ref": refs[i], "right_ref": refs[i + 1],
             "relation": (relations or ["SAME_PROBLEM"] * (len(refs) - 1))[i]}
            for i in range(len(refs) - 1)
        ],
        "splits": [],
        "uncertain": [],
    }


def test_overlap_agreement_has_no_conflict() -> None:
    refs = ["a", "b", "c"]
    first = _prediction(refs, ["PROBLEM_START", "PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION"])
    second = _prediction(["b", "c", "d"], ["PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION", "PROBLEM_START"])
    merged = sr.reconcile([first, second])
    assert merged["has_conflict"] is False
    assert merged["roles"]["b"] == "PROBLEM_CONTINUATION"
    assert merged["roles"]["d"] == "PROBLEM_START"


def test_overlap_conflict_becomes_uncertain_and_triggers_expansion() -> None:
    first = _prediction(["a", "b"], ["PROBLEM_START", "PROBLEM_CONTINUATION"])
    second = _prediction(["a", "b"], ["SOLUTION_START", "PROBLEM_CONTINUATION"])
    merged = sr.reconcile([first, second])
    assert merged["has_conflict"] is True
    assert merged["roles"]["a"] == "UNCERTAIN", "冲突不得默认取多数或第一次"
    window = _window(["a", "b", "c", "d", "e"])
    prediction = _prediction(["a", "b", "c", "d", "e"],
                             ["PROBLEM_START", "PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION",
                              "PROBLEM_CONTINUATION", "PROBLEM_START"])
    reasons = sr.expansion_reasons(window, prediction, conflicts=merged)
    assert "OVERLAP_CONFLICT" in reasons


def test_uncertain_triggers_expansion() -> None:
    window = _window(["a", "b", "c"])
    prediction = _prediction(["a", "b", "c"], ["PROBLEM_START", "UNCERTAIN", "PROBLEM_CONTINUATION"])
    prediction["uncertain"] = [{"kind": "ROLE", "refs": ["b"], "reason": "看不清"}]
    assert "MODEL_UNCERTAIN" in sr.expansion_reasons(window, prediction)


def test_head_and_tail_continuation_trigger_expansion() -> None:
    window = _window(["a", "b", "c", "d", "e"])
    prediction = _prediction(["a", "b", "c", "d", "e"],
                             ["PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION",
                              "PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION"])
    reasons = sr.expansion_reasons(window, prediction)
    assert "HEAD_CONTINUATION" in reasons and "TAIL_CONTINUATION" in reasons


def test_edge_split_triggers_expansion() -> None:
    window = _window(["a", "b", "c", "d", "e"])
    prediction = _prediction(["a", "b", "c", "d", "e"],
                             ["PROBLEM_START"] + ["PROBLEM_CONTINUATION"] * 3 + ["PROBLEM_START"])
    prediction["splits"] = [{"block_ref": "e", "anchor": "5. 已知"}]
    assert "EDGE_SPLIT" in sr.expansion_reasons(window, prediction)


def test_clean_window_needs_no_expansion() -> None:
    window = _window(["a", "b", "c", "d", "e"])
    # 首块是新题起点、末块也是新题起点（问题边界已闭合）→ 不需要扩窗
    prediction = _prediction(["a", "b", "c", "d", "e"],
                             ["PROBLEM_START", "PROBLEM_CONTINUATION", "PROBLEM_CONTINUATION",
                              "PROBLEM_CONTINUATION", "PROBLEM_START"])
    assert sr.expansion_reasons(window, prediction) == [], "跨页/普通窗口不触发扩窗"


def test_signal_conflict_detected() -> None:
    window = {"window_id": "w0000", "blocks": [
        {"block_ref": "a", "label": "number"},
        {"block_ref": "b", "label": "text"},
    ]}
    prediction = _prediction(["a", "b"], ["NON_PROBLEM", "PROBLEM_CONTINUATION"])
    assert "SIGNAL_CONFLICT" in sr.expansion_reasons(window, prediction)
