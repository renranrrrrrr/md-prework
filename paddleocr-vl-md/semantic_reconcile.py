"""Phase 5 第三段：overlap reconcile 与自适应扩窗触发（零模型链，确定性）。

两个职责：

1. **reconcile**：把同一文档多个窗口的预测按 ``block_ref`` 汇总。同一块/同一边界被
   不同窗口给出不同结论时，**不猜**——标成 ``UNCERTAIN`` 并记录冲突。
2. **expansion_reasons**：按 GPT 的 A–F 判定"这个窗口要不要从 5 扩到 9/13"。
   页边界本身**不**触发扩窗。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


UNCERTAIN = "UNCERTAIN"
CONTINUATION_ROLES = {"PROBLEM_CONTINUATION", "SOLUTION_CONTINUATION"}

#: 确定性信号与模型结论冲突的判据（F）：题号块被当成非题目、公式块被当成题目起点。
NUMBER_LABEL = "number"
FORMULA_LABELS = {"display_formula", "inline_formula"}


def reconcile(predictions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """汇总多窗口预测；冲突一律降级为 ``UNCERTAIN`` 并记录（不取多数、不取第一次）。"""

    roles: dict[str, set[str]] = {}
    relations: dict[tuple[str, str], set[str]] = {}
    for prediction in predictions:
        for item in prediction.get("roles") or []:
            roles.setdefault(str(item.get("block_ref")), set()).add(str(item.get("role")))
        for item in prediction.get("boundaries") or []:
            key = (str(item.get("left_ref")), str(item.get("right_ref")))
            relations.setdefault(key, set()).add(str(item.get("relation")))

    role_conflicts = {ref: sorted(values) for ref, values in roles.items() if len(values) > 1}
    boundary_conflicts = {
        f"{left}->{right}": sorted(values)
        for (left, right), values in relations.items()
        if len(values) > 1
    }
    return {
        "roles": {
            ref: (next(iter(values)) if len(values) == 1 else UNCERTAIN)
            for ref, values in roles.items()
        },
        "boundaries": {
            left + "->" + right: (
                next(iter(values)) if len(values) == 1 else UNCERTAIN
            )
            for (left, right), values in relations.items()
        },
        "role_conflicts": role_conflicts,
        "boundary_conflicts": boundary_conflicts,
        "has_conflict": bool(role_conflicts or boundary_conflicts),
    }


def expansion_reasons(
    window: Mapping[str, Any],
    prediction: Mapping[str, Any],
    *,
    conflicts: Mapping[str, Any] | None = None,
) -> list[str]:
    """返回该窗口需要扩窗的原因（空列表 = 不需要扩窗）。"""

    reasons: list[str] = []
    blocks = list(window.get("blocks") or [])
    if not blocks:
        return reasons
    refs = [str(block.get("block_ref")) for block in blocks]

    # A. 模型自报不确定
    if prediction.get("uncertain"):
        reasons.append("MODEL_UNCERTAIN")

    # B. overlap 窗口结论冲突（只算落在本窗口内的）
    if conflicts and conflicts.get("has_conflict"):
        role_hits = [ref for ref in (conflicts.get("role_conflicts") or {}) if ref in refs]
        boundary_hits = [
            key
            for key in (conflicts.get("boundary_conflicts") or {})
            if key.split("->")[0] in refs and key.split("->")[-1] in refs
        ]
        if role_hits or boundary_hits:
            reasons.append("OVERLAP_CONFLICT")

    # C. split 落在窗口边缘
    for item in prediction.get("splits") or []:
        if str(item.get("block_ref")) in {refs[0], refs[-1]}:
            reasons.append("EDGE_SPLIT")
            break

    role_by_ref = {
        str(item.get("block_ref")): str(item.get("role")) for item in prediction.get("roles") or []
    }
    # D/E. 首尾块被判 continuation → 前文/后文不足
    if role_by_ref.get(refs[0]) in CONTINUATION_ROLES:
        reasons.append("HEAD_CONTINUATION")
    if role_by_ref.get(refs[-1]) in CONTINUATION_ROLES:
        reasons.append("TAIL_CONTINUATION")

    # F. 确定性信号与模型结论冲突
    for block in blocks:
        ref = str(block.get("block_ref"))
        label = str(block.get("label") or "")
        role = role_by_ref.get(ref)
        if label == NUMBER_LABEL and role in {"NON_PROBLEM", "SHARED_CONTEXT"}:
            reasons.append("SIGNAL_CONFLICT")
            break
        if label in FORMULA_LABELS and role == "PROBLEM_START":
            reasons.append("SIGNAL_CONFLICT")
            break
    return reasons
