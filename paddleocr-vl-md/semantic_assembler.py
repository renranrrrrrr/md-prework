"""Phase 5 第四段：SPLIT anchor 解析与确定性 assembler（零模型链，不调用模型）。

职责：

* ``resolve_split``：把模型给的**原文 anchor** 解析成字符区间。重复 anchor **不默认取第一次**，
  而是返回 ``ambiguous`` 交给上层（扩窗/人工）；
* ``assemble``：把 reconcile 后的角色/边界 + 已解析的 split 物化成 Problem Candidate。
  Candidate 只引用 ``block_ref`` + ``[start, end)`` 区间，**从不改写正文**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence


PROBLEM_START = "PROBLEM_START"
PROBLEM_CONTINUATION = "PROBLEM_CONTINUATION"
SOLUTION_START = "SOLUTION_START"
SOLUTION_CONTINUATION = "SOLUTION_CONTINUATION"
SHARED_CONTEXT = "SHARED_CONTEXT"


def _validate_block_refs(blocks: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for index, block in enumerate(blocks):
        ref = block.get("block_ref")
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"invalid_block_ref: block index {index}")
        if ref in seen:
            raise ValueError(f"duplicate_block_ref:{ref}")
        seen.add(ref)


#: 题号抽取（只作 provenance 与回归对照用，不参与身份判定）。
_ARABIC_RE = re.compile(r"^\s*(?P<number>\d{1,3})\s*[.、]\s*")
_CHINESE_RE = re.compile(r"^\s*(?P<number>[一二三四五六七八九十]{1,3})\s*[、.]\s*")


def extract_question_number(text: str) -> dict[str, str]:
    """从候选题首段文本里抽取题号；抽不到就返回空 dict（不猜）。"""

    head = (text or "").strip()[:40]
    match = _ARABIC_RE.match(head)
    if match:
        return {"number": match.group("number"), "numbering_system": "arabic"}
    match = _CHINESE_RE.match(head)
    if match:
        return {"number": match.group("number"), "numbering_system": "chinese"}
    return {}


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
    number: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "statement_refs": list(self.statement_refs),
            "solution_refs": list(self.solution_refs),
            "shared_refs": list(self.shared_refs),
            "diagnostics": list(self.diagnostics),
            "number": dict(self.number),
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

    _validate_block_refs(blocks)
    roles = dict(reconciled.get("roles") or {})
    splits = dict(splits or {})
    candidates: list[Candidate] = []
    excluded: list[dict[str, str]] = []
    current: Candidate | None = None
    pending_shared: list[str] = []
    diagnostics: list[str] = []
    diagnostic_records: list[dict[str, str]] = []

    def _diagnose(code: str, ref: str, detail: str = "") -> None:
        # 保留 v1 的可读字符串；机器核查必须使用完整的 block_ref，不能做子串匹配。
        diagnostics.append(f"{code}:{ref}" + (f":{detail}" if detail else ""))
        record = {"code": code, "block_ref": ref}
        if detail:
            record["detail"] = detail
        diagnostic_records.append(record)

    def _new_candidate() -> Candidate:
        candidate = Candidate(candidate_id=f"pc{len(candidates) + 1:04d}")
        candidate.shared_refs.extend(pending_shared)
        pending_shared.clear()
        candidates.append(candidate)
        return candidate

    block_refs = {str(block.get("block_ref")) for block in blocks}
    for ref in sorted(splits.keys() - block_refs):
        _diagnose("split_unknown_block", ref)

    for block in blocks:
        ref = str(block.get("block_ref"))
        role = str(roles.get(ref) or "UNCERTAIN")
        text = str(block.get("raw_text") or "")
        full_range = {"block_ref": ref, "start": 0, "end": len(text)}
        split_start: int | None = None
        if ref in splits:
            split = splits[ref]
            if split.get("status") != "resolved":
                _diagnose("unresolved_split", ref, str(split.get("status")))
            else:
                start, end = split.get("start"), split.get("end")
                # anchor 必须完全落在原文内，且确实在块内部开始第二题。
                # 不强转字符串/浮点数；start=0 会制造空题，旧版还会在遍历时无限追加。
                if type(start) is not int or type(end) is not int or not 0 < start < end <= len(text):
                    _diagnose("invalid_split", ref)
                elif role not in {PROBLEM_START, PROBLEM_CONTINUATION} or (
                    role == PROBLEM_CONTINUATION and current is None
                ):
                    _diagnose("unapplied_split", ref, role)
                else:
                    split_start = start

        if role == SHARED_CONTEXT:
            pending_shared.append(ref)
            continue
        if role in {PROBLEM_START, PROBLEM_CONTINUATION}:
            if role == PROBLEM_START:
                current = _new_candidate()
                current.number = extract_question_number(text)
            if current is None:
                _diagnose("orphan_continuation", ref)
                continue
            if split_start is None:
                current.statement_refs.append(full_range)
            else:
                # 按文档流即时切分：其后的续写/解析只属于新题，既有解析仍属于原题。
                current.statement_refs.append({"block_ref": ref, "start": 0, "end": split_start})
                current = _new_candidate()
                current.statement_refs.append({"block_ref": ref, "start": split_start, "end": len(text)})
                current.number = extract_question_number(text[split_start:])
            continue
        if role in {SOLUTION_START, SOLUTION_CONTINUATION}:
            if current is None:
                _diagnose("orphan_solution", ref)
                continue
            current.solution_refs.append(full_range)
            continue
        if role == "UNCERTAIN":
            _diagnose("uncertain_role", ref)
            excluded.append({"block_ref": ref, "reason": "uncertain_role"})
            continue
        # 未知角色不能悄悄伪装成已确认的非题目内容。
        if role != "NON_PROBLEM":
            _diagnose("unknown_role", ref, role)
        excluded.append({"block_ref": ref, "reason": f"excluded_role:{role}"})

    # 文档结束时仍没有后续题目：保留块身份供人工判断，不猜测归给上一题。
    for ref in pending_shared:
        _diagnose("orphan_shared_context", ref)

    return {
        "schema_version": "md-prework/problem-candidates/v1",
        "candidate_count": len(candidates),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "excluded": excluded,
        "diagnostics": diagnostics,
        "diagnostic_records": diagnostic_records,
    }


def audit_block_coverage(blocks: Sequence[Mapping[str, Any]], assembly: Mapping[str, Any]) -> dict[str, Any]:
    """精确核对块身份与引用区间；不把部分引用当成整块已覆盖。

    兼容旧产物中有限的已知诊断格式。新产物使用 ``diagnostic_records``，
    从而避免 ``b0001`` 被 ``b00010`` 的诊断误记为已登记。
    """

    _validate_block_refs(blocks)
    lengths = {str(b.get("block_ref")): len(str(b.get("raw_text") or "")) for b in blocks}
    ranges: dict[str, list[tuple[int, int]]] = {}
    invalid: list[dict[str, Any]] = []
    for candidate in assembly.get("candidates") or []:
        for item in (candidate.get("statement_refs") or []) + (candidate.get("solution_refs") or []):
            ref = str(item.get("block_ref"))
            start, end = item.get("start"), item.get("end")
            if (
                ref not in lengths
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end <= lengths[ref]
            ):
                invalid.append(dict(item))
            else:
                ranges.setdefault(ref, []).append((start, end))
        for ref in candidate.get("shared_refs") or []:
            ref = str(ref)
            if ref not in lengths:
                invalid.append({"block_ref": ref, "kind": "shared_ref"})
            else:
                ranges.setdefault(ref, []).append((0, lengths[ref]))

    referenced: set[str] = set()
    for ref, intervals in ranges.items():
        cursor = 0
        for start, end in sorted(intervals):
            if start > cursor:
                break
            cursor = max(cursor, end)
        if cursor == lengths[ref]:
            referenced.add(ref)

    excluded = {str(item.get("block_ref")) for item in assembly.get("excluded") or []}
    if "diagnostic_records" in assembly:
        diagnosed = {
            str(item.get("block_ref"))
            for item in assembly.get("diagnostic_records") or []
            if item.get("code")
        }
    else:
        legacy = set(assembly.get("diagnostics") or [])
        diagnosed = {
            ref
            for ref in lengths
            if any(
                f"{code}:{ref}" in legacy
                for code in ("orphan_continuation", "orphan_solution", "uncertain_role")
            )
            or any(item.rsplit(":", 1)[0] == f"unresolved_split:{ref}" for item in legacy)
        }
    return {
        "referenced_refs": sorted(referenced),
        "excluded_refs": sorted(excluded),
        "diagnosed_refs": sorted(diagnosed),
        "silent_loss_refs": sorted(lengths.keys() - referenced - excluded - diagnosed),
        "partial_reference_refs": sorted(ranges.keys() - referenced),
        "invalid_references": invalid,
    }
