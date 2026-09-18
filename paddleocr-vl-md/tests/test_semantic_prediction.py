"""semantic-prediction/v1 结构校验与假 provider 的离线测试。"""

from __future__ import annotations

import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_prediction as sp  # noqa: E402


def _window(size: int = 5) -> dict:
    blocks = [
        {"block_ref": f"p0000:b{index:04d}", "label": "text", "raw_text": f"t{index}"}
        for index in range(size)
    ]
    return {"window_id": "w0000", "blocks": blocks, "window_size": size}


def test_fake_provider_produces_valid_prediction() -> None:
    provider = sp.FakeSemanticProvider(sp.sequential_decider())
    prediction = provider.predict(_window())
    assert prediction["schema"] == sp.PREDICTION_SCHEMA
    assert [item["block_ref"] for item in prediction["roles"]] == [
        f"p0000:b{index:04d}" for index in range(5)
    ]
    assert len(prediction["boundaries"]) == 4, "必须覆盖全部相邻边界"


def test_roles_must_cover_window_in_order() -> None:
    window = _window()
    prediction = {
        "schema": sp.PREDICTION_SCHEMA,
        "window_id": "w0000",
        "roles": [{"block_ref": "p0000:b0001", "role": "PROBLEM_START"}],
        "boundaries": [],
    }
    with pytest.raises(sp.PredictionError):
        sp.validate_prediction(prediction, window)


def test_boundaries_must_be_complete_and_ordered() -> None:
    window = _window()
    roles = [{"block_ref": f"p0000:b{i:04d}", "role": "PROBLEM_CONTINUATION"} for i in range(5)]
    prediction = {
        "schema": sp.PREDICTION_SCHEMA,
        "window_id": "w0000",
        "roles": roles,
        "boundaries": [
            {"left_ref": "p0000:b0001", "right_ref": "p0000:b0002", "relation": "SAME_PROBLEM"}
        ],
    }
    with pytest.raises(sp.PredictionError):
        sp.validate_prediction(prediction, window)


def test_unknown_enum_is_rejected() -> None:
    window = _window(2)
    prediction = {
        "schema": sp.PREDICTION_SCHEMA,
        "window_id": "w0000",
        "roles": [
            {"block_ref": "p0000:b0000", "role": "MAYBE_PROBLEM"},
            {"block_ref": "p0000:b0001", "role": "PROBLEM_CONTINUATION"},
        ],
        "boundaries": [
            {"left_ref": "p0000:b0000", "right_ref": "p0000:b0001", "relation": "SAME_PROBLEM"}
        ],
    }
    with pytest.raises(sp.PredictionError):
        sp.validate_prediction(prediction, window)


def test_splits_need_exact_anchor_and_inside_window() -> None:
    window = _window(2)
    base = {
        "schema": sp.PREDICTION_SCHEMA,
        "window_id": "w0000",
        "roles": [
            {"block_ref": "p0000:b0000", "role": "PROBLEM_START"},
            {"block_ref": "p0000:b0001", "role": "PROBLEM_CONTINUATION"},
        ],
        "boundaries": [
            {"left_ref": "p0000:b0000", "right_ref": "p0000:b0001", "relation": "SAME_PROBLEM"}
        ],
    }
    sp.validate_prediction({**base, "splits": [{"block_ref": "p0000:b0000", "anchor": "4. 已知"}]}, window)
    with pytest.raises(sp.PredictionError):
        sp.validate_prediction({**base, "splits": [{"block_ref": "p0000:b0000", "anchor": "  "}]}, window)
    with pytest.raises(sp.PredictionError):
        sp.validate_prediction({**base, "splits": [{"block_ref": "p9999:b0000", "anchor": "x"}]}, window)


def test_numeric_confidence_is_rejected() -> None:
    window = _window(2)
    prediction = {
        "schema": sp.PREDICTION_SCHEMA,
        "window_id": "w0000",
        "roles": [
            {"block_ref": "p0000:b0000", "role": "PROBLEM_START", "confidence": 0.93},
            {"block_ref": "p0000:b0001", "role": "PROBLEM_CONTINUATION"},
        ],
        "boundaries": [
            {"left_ref": "p0000:b0000", "right_ref": "p0000:b0001", "relation": "SAME_PROBLEM"}
        ],
    }
    with pytest.raises(sp.PredictionError):
        sp.validate_prediction(prediction, window)


def test_uncertain_refs_must_be_inside_window() -> None:
    window = _window(2)
    prediction = {
        "schema": sp.PREDICTION_SCHEMA,
        "window_id": "w0000",
        "roles": [
            {"block_ref": "p0000:b0000", "role": "UNCERTAIN"},
            {"block_ref": "p0000:b0001", "role": "PROBLEM_CONTINUATION"},
        ],
        "boundaries": [
            {"left_ref": "p0000:b0000", "right_ref": "p0000:b0001", "relation": "UNCERTAIN"}
        ],
        "uncertain": [{"kind": "BOUNDARY", "refs": ["p0000:b9999"], "reason": "上下文不足"}],
    }
    with pytest.raises(sp.PredictionError):
        sp.validate_prediction(prediction, window)


def test_window_ids_are_stable() -> None:
    windows = sp.with_window_id([{"blocks": []}, {"blocks": []}])
    assert [w["window_id"] for w in windows] == ["w0000", "w0001"]
