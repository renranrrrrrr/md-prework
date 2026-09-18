"""Phase 5 第二段：``md-prework/semantic-prediction/v1`` 的结构校验与假 provider。

这一层只做两件事：

* 校验模型输出**结构**（枚举、引用完整性、覆盖完整性），不做任何语义判断；
* 提供 ``FakeSemanticProvider``，让"零模型端到端链"可以完全确定地跑通。

硬约束（GPT 裁决）：

* ``roles[]`` 必须逐块引用窗口内的 ``block_ref``；
* ``boundaries[]`` 必须覆盖窗口内**全部**相邻边界；
* ``splits[]`` 只给原文 anchor，不给 offset，也不允许改写正文；
* 核心 schema **不带数值 confidence**；不确定就输出 ``UNCERTAIN``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


PREDICTION_SCHEMA = "md-prework/semantic-prediction/v1"

ROLES = (
    "PROBLEM_START",
    "PROBLEM_CONTINUATION",
    "SOLUTION_START",
    "SOLUTION_CONTINUATION",
    "SHARED_CONTEXT",
    "NON_PROBLEM",
    "UNCERTAIN",
)
BOUNDARY_RELATIONS = ("SAME_PROBLEM", "NEW_PROBLEM", "NOT_RELATED", "UNCERTAIN")


class PredictionError(ValueError):
    """模型输出不符合 schema 或引用不完整。"""


def validate_prediction(prediction: Mapping[str, Any], window: Mapping[str, Any]) -> None:
    """按窗口校验预测结构；任何违规抛 ``PredictionError``（不静默降级）。"""

    if prediction.get("schema") != PREDICTION_SCHEMA:
        raise PredictionError(f"schema 必须是 {PREDICTION_SCHEMA}")
    if prediction.get("window_id") != window.get("window_id"):
        raise PredictionError("window_id 与窗口不匹配")

    refs = [str(block.get("block_ref")) for block in window.get("blocks") or []]
    if not refs:
        raise PredictionError("窗口为空")

    roles = prediction.get("roles")
    if not isinstance(roles, list) or not roles:
        raise PredictionError("roles 不能为空")
    seen: list[str] = []
    for item in roles:
        ref = str((item or {}).get("block_ref") or "")
        role = str((item or {}).get("role") or "")
        if role not in ROLES:
            raise PredictionError(f"未知 role：{role}")
        if ref not in refs:
            raise PredictionError(f"role 引用了窗口外的 block_ref：{ref}")
        if ref in seen:
            raise PredictionError(f"role 重复引用同一块：{ref}")
        seen.append(ref)
    if seen != refs:
        raise PredictionError("roles 必须按窗口顺序逐块覆盖（顺序或数量不符）")

    boundaries = prediction.get("boundaries")
    if not isinstance(boundaries, list):
        raise PredictionError("boundaries 必须是列表")
    expected_pairs = [(refs[i], refs[i + 1]) for i in range(len(refs) - 1)]
    actual_pairs = [
        (str((item or {}).get("left_ref") or ""), str((item or {}).get("right_ref") or ""))
        for item in boundaries
    ]
    if actual_pairs != expected_pairs:
        raise PredictionError("boundaries 必须覆盖窗口内全部相邻边界且顺序一致")
    for item in boundaries:
        relation = str((item or {}).get("relation") or "")
        if relation not in BOUNDARY_RELATIONS:
            raise PredictionError(f"未知 boundary relation：{relation}")

    for item in prediction.get("splits") or []:
        ref = str((item or {}).get("block_ref") or "")
        anchor = (item or {}).get("anchor")
        if ref not in refs:
            raise PredictionError(f"split 引用了窗口外的 block_ref：{ref}")
        if not isinstance(anchor, str) or not anchor.strip():
            raise PredictionError("split 必须给出非空原文 anchor")
    for item in prediction.get("uncertain") or []:
        kind = str((item or {}).get("kind") or "")
        if kind not in {"ROLE", "BOUNDARY", "SPLIT"}:
            raise PredictionError(f"未知 uncertain kind：{kind}")
        for ref in (item or {}).get("refs") or []:
            if str(ref) not in refs:
                raise PredictionError(f"uncertain 引用了窗口外的 block_ref：{ref}")
    if "confidence" in json.dumps(prediction):
        raise PredictionError("核心 schema 不允许出现数值 confidence")


@dataclass(frozen=True)
class FakeSemanticProvider:
    """零模型 provider：按预设规则产出合法预测，用于端到端演练。"""

    decide: Callable[[Mapping[str, Any]], Mapping[str, Any]]

    def predict(self, window: Mapping[str, Any]) -> dict[str, Any]:
        prediction = dict(self.decide(window))
        prediction.setdefault("schema", PREDICTION_SCHEMA)
        prediction.setdefault("window_id", window.get("window_id"))
        validate_prediction(prediction, window)
        return prediction


def sequential_decider(new_problem_every: int = 3) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """最朴素的假规则：每 N 块开一道新题，其余为延续，关系随之推导。"""

    def _decide(window: Mapping[str, Any]) -> Mapping[str, Any]:
        refs = [str(block.get("block_ref")) for block in window.get("blocks") or []]
        roles = []
        for index, ref in enumerate(refs):
            role = "PROBLEM_START" if index % new_problem_every == 0 else "PROBLEM_CONTINUATION"
            roles.append({"block_ref": ref, "role": role})
        boundaries = [
            {
                "left_ref": refs[i],
                "right_ref": refs[i + 1],
                "relation": "SAME_PROBLEM"
                if roles[i + 1]["role"] == "PROBLEM_CONTINUATION"
                else "NEW_PROBLEM",
            }
            for i in range(len(refs) - 1)
        ]
        return {"roles": roles, "boundaries": boundaries, "splits": [], "uncertain": []}

    return _decide


def with_window_id(windows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """给窗口补上稳定的 ``window_id``（``w0000`` 起）。"""

    return [
        {**dict(window), "window_id": f"w{index:04d}"} for index, window in enumerate(windows)
    ]
