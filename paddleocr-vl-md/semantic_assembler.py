"""Phase 5 第四段：SPLIT anchor 解析与确定性 assembler（零模型链，不调用模型）。

职责：

* ``resolve_split``：把模型给的**原文 anchor** 解析成字符区间。重复 anchor **不默认取第一次**，
  而是返回 ``ambiguous`` 交给上层（扩窗/人工）；
* ``assemble``：把 reconcile 后的角色/边界 + 已解析的 split 物化成 Problem Candidate。
  Candidate 只引用 ``block_ref`` + ``[start, end)`` 区间，**从不改写正文**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


PROBLEM_START = "PROBLEM_START"
PROBLEM_CONTINUATION = "PROBLEM_CONTINUATION"
SOLUTION_START = "SOLUTION_START"
SOLUTION_CONTINUATION = "SOLUTION_CONTINUATION"
SHARED_CONTEXT = "SHARED_CONTEXT"


def resolve_split(text: str, anchor: str) -> dict[str, Any]:
    """把 anchor 解析成 ``[start, end)``；重复出现时返回 ambiguous（不猜）。"""

    if not anchor:
        return {"status": "missing_anchor"}
    positions: list[int] = []
    cursor = text.find(anchor)
    while cursor >= 0:
        positions.append(cursor)
        cursor = text.find(anchor, cursor + 1)
    if not positions:
        return {"status": "not_found"}
    if len(positions) > 1:
        return {"status": "ambiguous", "candidates": positions}
    start = positions[0]
    return {"status": "resolved", "start": start, "end": start + len(anchor)}


@dataclass
class Candidate:
    """一道候选题目（只保存引用，不复制正文）。"""

    candidate_id: str
    statement_refs: list[dict[str, Any]] = field(default_factory=list)
    solution_refs: list[dict[str, Any]] = field(default_factory=list)
    shared_refs: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "statement_refs": list(self.statement_refs),
            "solution_refs": list(self.solution_refs),
            "shared_refs": list(self.shared_refs),
            "diagnostics": list(self.diagnostics),
        }


def assemble(
    blocks: Sequence[Mapping[str, Any]],
    reconciled: Mapping[str, Any],
    *,
    splits: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """物化 Problem Candidate。

    ``reconciled["roles"]`` 给出每个 ``block_ref`` 的角色；``splits`` 给出已解析的切分点
    （``{block_ref: {"status": "resolved", "start": n, "end": m}}``）。
    """

    roles = dict(reconciled.get("roles") or {})
    splits = dict(splits or {})
    candidates: list[Candidate] = []
    current: Candidate | None = None
    pending_shared: list[str] = []
    diagnostics: list[str] = []

    def _new_candidate() -> Candidate:
        candidate = Candidate(candidate_id=f"pc{len(candidates) + 1:04d}")
        candidate.shared_refs.extend(pending_shared)
        pending_shared.clear()
        candidates.append(candidate)
        return candidate

    for block in blocks:
        ref = str(block.get("block_ref"))
        role = str(roles.get(ref) or "UNCERTAIN")
        text = str(block.get("raw_text") or "")
        full_range = {"block_ref": ref, "start": 0, "end": len(text)}

        if role == SHARED_CONTEXT:
            pending_shared.append(ref)
            continue
        if role == PROBLEM_START:
            current = _new_candidate()
            current.statement_refs.append(full_range)
            continue
        if role == PROBLEM_CONTINUATION:
            if current is None:
                diagnostics.append(f"orphan_continuation:{ref}")
                continue
            current.statement_refs.append(full_range)
            continue
        if role in {SOLUTION_START, SOLUTION_CONTINUATION}:
            if current is None:
                diagnostics.append(f"orphan_solution:{ref}")
                continue
            current.solution_refs.append(full_range)
            continue
        if role == "UNCERTAIN":
            diagnostics.append(f"uncertain_role:{ref}")
            continue
        # NON_PROBLEM 等：不进入候选题

    # SPLIT 只影响"块内两题"的切分：把该块按区间劈成两段引用
    for ref, split in splits.items():
        if split.get("status") != "resolved":
            diagnostics.append(f"unresolved_split:{ref}:{split.get('status')}")
            continue
        start, end = int(split["start"]), int(split["end"])
        for candidate in candidates:
            for index, item in enumerate(candidate.statement_refs):
                if item["block_ref"] == ref and item["start"] == 0:
                    block_len = item["end"]
                    # anchor 标记新题开始：前半段留给当前 candidate，后半段开新 candidate
                    candidate.statement_refs[index] = {"block_ref": ref, "start": 0, "end": start}
                    tail = candidate.statement_refs[index + 1 :]
                    candidate.statement_refs = candidate.statement_refs[: index + 1]
                    new_candidate = Candidate(candidate_id=f"pc{len(candidates) + 1:04d}")
                    new_candidate.statement_refs = [
                        {"block_ref": ref, "start": start, "end": block_len},
                        *tail,
                    ]
                    new_candidate.solution_refs = list(candidate.solution_refs)
                    candidates.append(new_candidate)
                    break

    return {
        "schema_version": "md-prework/problem-candidates/v1",
        "candidate_count": len(candidates),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "diagnostics": diagnostics,
    }
