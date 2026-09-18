"""prompt 模板、响应解析与非法响应重试的离线测试。"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_prompt as sprompt  # noqa: E402
from semantic_prediction import PREDICTION_SCHEMA, PredictionError  # noqa: E402


def _window(size: int = 3) -> dict:
    return {
        "window_id": "w0000",
        "window_size": size,
        "blocks": [
            {"block_ref": f"p0000:b{i:04d}", "label": "text", "raw_text": f"t{i}"}
            for i in range(size)
        ],
    }


def _valid_text(window: dict) -> str:
    refs = [block["block_ref"] for block in window["blocks"]]
    payload = {
        "schema": PREDICTION_SCHEMA,
        "window_id": window["window_id"],
        "roles": [{"block_ref": ref, "role": "PROBLEM_START"} for ref in refs],
        "boundaries": [
            {"left_ref": refs[i], "right_ref": refs[i + 1], "relation": "SAME_PROBLEM"}
            for i in range(len(refs) - 1)
        ],
        "splits": [],
        "uncertain": [],
    }
    return json.dumps(payload, ensure_ascii=False)


def test_prompt_contains_hard_constraints() -> None:
    text = sprompt.SYSTEM_PROMPT
    for required in ("不得改写", "不得输出字符 offset", "不得输出任何数值 confidence", "UNCERTAIN",
                     "PROBLEM_START", "SAME_PROBLEM", "页边界本身不是语义边界"):
        assert required in text, required


def test_user_message_only_contains_window_payload() -> None:
    message = sprompt.build_user_message(_window(2))
    assert "window_id" in message and "block_ref" in message
    assert "role" not in message.split("blocks")[0], "不得泄漏角色结论或参考答案"


def test_parse_response_strips_code_fence() -> None:
    window = _window()
    fenced = "```json\n" + _valid_text(window) + "\n```"
    assert sprompt.parse_response(fenced)["window_id"] == "w0000"
    with pytest.raises(PredictionError):
        sprompt.parse_response("not json at all")


def test_predict_with_retry_succeeds_after_invalid_first_response() -> None:
    window = _window()
    calls = {"n": 0}

    def provider(system: str, user: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"schema": "wrong"}'
        assert system == sprompt.SYSTEM_PROMPT
        return _valid_text(window)

    result = sprompt.predict_with_retry(provider, window, attempts=2)
    assert calls["n"] == 2
    assert [record.ok for record in result.attempts] == [False, True]
    assert result.attempts[0].error


def test_predict_with_retry_exhausted_keeps_raw() -> None:
    window = _window()

    def provider(system: str, user: str) -> str:
        return "抱歉，我无法完成"

    with pytest.raises(PredictionError) as excinfo:
        sprompt.predict_with_retry(provider, window, attempts=2)
    assert "2 次尝试" in str(excinfo.value)
