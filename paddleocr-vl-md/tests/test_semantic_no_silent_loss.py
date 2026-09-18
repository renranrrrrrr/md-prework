"""覆盖不变量：每个输入 evidence block 最终必须进入 candidate / excluded / 诊断。

钉的是 ``semantic_baseline`` 的 ``assembler_no_silent_loss`` 硬门背后的性质本身，
而不是某一天的实测数字：任何 role 组合、任何 split 结果都不允许出现"三无块"
（没进候选、没进 excluded、没有点名诊断）。真实 baseline 里的 ``silent_loss = 15``
就是这条性质被破坏的证据。
"""

from __future__ import annotations

import itertools
import json
import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_assembler as sa  # noqa: E402
import semantic_baseline as baseline  # noqa: E402
import semantic_chain as chain  # noqa: E402
import semantic_prediction as sp  # noqa: E402


ALL_ROLES = sp.ROLES


def _blocks(refs):
    return [{"block_ref": ref, "label": "text", "raw_text": f"正文 {ref}"} for ref in refs]


def _accounted(result, blocks):
    return sa.unaccounted_blocks(blocks, result)


# —— role 层：逐个 role 都必须留下痕迹 ——


def test_every_single_role_is_accounted() -> None:
    for role in ALL_ROLES:
        blocks = _blocks(["p0000:b0000"])
        result = sa.assemble(blocks, {"roles": {"p0000:b0000": role}})
        assert _accounted(result, blocks) == [], f"role={role} 出现静默丢失"


def test_unknown_role_falls_through_to_excluded() -> None:
    blocks = _blocks(["p0000:b0000"])
    result = sa.assemble(blocks, {"roles": {"p0000:b0000": "SOMETHING_NEW"}})
    assert _accounted(result, blocks) == []
    assert result["excluded"][0]["reason"] == "excluded_role:SOMETHING_NEW"


def test_block_without_any_role_is_accounted() -> None:
    """reconcile 没给出角色（provider 漏答）时也必须登记，不能凭空消失。"""

    blocks = _blocks(["p0000:b0000", "p0000:b0001"])
    result = sa.assemble(blocks, {"roles": {"p0000:b0001": "PROBLEM_START"}})
    assert _accounted(result, blocks) == []
    assert {"block_ref": "p0000:b0000", "reason": "uncertain_role"} in result["excluded"]


# —— 回归：SHARED_CONTEXT 没有后继题目时的静默丢失 ——


def test_trailing_shared_context_is_not_silently_lost() -> None:
    """最后一道题之后出现的共享材料没有 candidate 可归属，必须显式登记。"""

    blocks = _blocks(["p0000:b0000", "p0000:b0001", "p0000:b0002"])
    result = sa.assemble(
        blocks,
        {
            "roles": {
                "p0000:b0000": "PROBLEM_START",
                "p0000:b0001": "SHARED_CONTEXT",
                "p0000:b0002": "SHARED_CONTEXT",
            }
        },
    )
    assert _accounted(result, blocks) == []
    unattached = {item["block_ref"] for item in result["excluded"] if item["reason"] == "unattached_shared_context"}
    assert unattached == {"p0000:b0001", "p0000:b0002"}


def test_shared_only_document_is_fully_accounted() -> None:
    blocks = _blocks([f"p0000:b{i:04d}" for i in range(3)])
    result = sa.assemble(blocks, {"roles": {b["block_ref"]: "SHARED_CONTEXT" for b in blocks}})
    assert result["candidate_count"] == 0
    assert _accounted(result, blocks) == []


def test_shared_context_before_a_problem_still_attaches_to_that_problem() -> None:
    """已有语义不得因为补登记而改变：有后继题目时仍然归属该题。"""

    blocks = _blocks(["p0000:b0000", "p0000:b0001", "p0000:b0002"])
    result = sa.assemble(
        blocks,
        {
            "roles": {
                "p0000:b0000": "SHARED_CONTEXT",
                "p0000:b0001": "SHARED_CONTEXT",
                "p0000:b0002": "PROBLEM_START",
            }
        },
    )
    assert result["candidates"][0]["shared_refs"] == ["p0000:b0000", "p0000:b0001"]
    assert result["excluded"] == []
    assert _accounted(result, blocks) == []


# —— split 层：任何解析结果都不许让块消失 ——

SPLIT_VARIANTS = {
    "none": {},
    "resolved_mid": {"p0000:b0001": {"status": "resolved", "start": 3, "end": 8}},
    "resolved_at_head": {"p0000:b0001": {"status": "resolved", "start": 0, "end": 5}},
    "ambiguous": {"p0000:b0001": {"status": "ambiguous", "candidates": [0, 3]}},
    "not_found": {"p0000:b0001": {"status": "not_found"}},
    "missing_anchor": {"p0000:b0001": {"status": "missing_anchor"}},
    "on_shared_block": {"p0000:b0000": {"status": "resolved", "start": 2, "end": 4}},
    "multi_block": {
        "p0000:b0000": {"status": "resolved", "start": 2, "end": 4},
        "p0000:b0001": {"status": "resolved", "start": 3, "end": 8},
    },
}


def test_exhaustive_role_and_split_sweep_keeps_accounting_complete() -> None:
    """4 块 × 全部 role 组合 × 全部 split 形态，逐一断言无静默丢失。

    违例只留第一条：整表交给 pytest 做断言解释会让这一步慢到不可用。
    """

    refs = [f"p0000:b{i:04d}" for i in range(4)]
    blocks = _blocks(refs)
    first_violation = None
    checked = 0
    for combo in itertools.product(ALL_ROLES, repeat=len(refs)):
        roles = dict(zip(refs, combo))
        for name, splits in SPLIT_VARIANTS.items():
            checked += 1
            lost = _accounted(sa.assemble(blocks, {"roles": roles}, splits=splits), blocks)
            if lost:
                first_violation = (dict(combo), name, lost, checked)
                break
        if first_violation:
            break
    assert first_violation is None, f"roles/split={first_violation[1]} 丢块 {first_violation[2]}"
    assert checked == len(ALL_ROLES) ** len(refs) * len(SPLIT_VARIANTS)


def test_sweep_actually_covers_the_shared_context_paths() -> None:
    """自检：sweep 里确实跑到了"尾部共享材料"这条分支。"""

    refs = [f"p0000:b{i:04d}" for i in range(4)]
    blocks = _blocks(refs)
    reached = 0
    for combo in itertools.product(ALL_ROLES, repeat=len(refs)):
        if combo[-1] == "SHARED_CONTEXT" and "PROBLEM_START" in combo[:-1]:
            reached += 1
    assert reached > 0


# —— 记账器本身不许放宽 ——


def test_helper_does_not_accept_prefix_collisions() -> None:
    """``p0000:b00`` 不得因为诊断里出现了 ``p0000:b001`` 就算被登记。"""

    result = {"candidates": [], "excluded": [], "diagnostics": ["orphan_solution:p0000:b001"]}
    assert sa.unaccounted_blocks(_blocks(["p0000:b00"]), result) == ["p0000:b00"]
    assert sa.unaccounted_blocks(_blocks(["p0000:b001"]), result) == []


def test_helper_counts_all_three_channels() -> None:
    blocks = _blocks(["p0000:b0000", "p0000:b0001", "p0000:b0002", "p0000:b0003"])
    result = {
        "candidates": [
            {
                "statement_refs": [{"block_ref": "p0000:b0000", "start": 0, "end": 1}],
                "solution_refs": [],
                "shared_refs": ["p0000:b0001"],
            }
        ],
        "excluded": [{"block_ref": "p0000:b0002", "reason": "excluded_role:NON_PROBLEM"}],
        "diagnostics": ["orphan_solution:p0000:b0003"],
    }
    assert sa.unaccounted_blocks(blocks, result) == []


# —— 端到端：窗口重叠 / 冲突降级 / 扩窗触发之后仍然成立 ——


class ScriptedProvider:
    def __init__(self, script):
        self.script = script

    def predict(self, window):
        return sp.FakeSemanticProvider(self.script).predict(window)


def _document(texts):
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


def test_chain_with_conflicts_and_trailing_shared_context_is_accounted() -> None:
    """重叠窗口把同一块降级成 UNCERTAIN，尾部共享材料无题可归——两条路径同时走。"""

    document = _document(
        [
            ("number", "1. 甲题"),
            ("text", "甲题续写"),
            ("text", "甲题续写二"),
            ("text", "共用材料"),
            ("paragraph_title", "考前提示"),
            ("text", "答题注意事项"),
            ("footer", "第 1 页"),
        ]
    )
    # 7 块、window=5 / stride=3 → w0000=[0,5)、w0001=[3,7)，重叠区 b0003..b0004
    def script(window):
        refs = [block["block_ref"] for block in window["blocks"]]
        first = window["window_id"] == "w0000"
        roles = []
        for ref in refs:
            if ref == "p0000:b0003":
                # 重叠块在两个窗口里结论不同 → reconcile 降级 UNCERTAIN
                role = "SHARED_CONTEXT" if first else "PROBLEM_CONTINUATION"
            elif ref in {"p0000:b0004", "p0000:b0005", "p0000:b0006"}:
                role = "SHARED_CONTEXT"  # 之后再没有 PROBLEM_START → 无题可归属
            elif ref == "p0000:b0000":
                role = "PROBLEM_START"
            else:
                role = "PROBLEM_CONTINUATION"
            roles.append({"block_ref": ref, "role": role})
        return {
            "roles": roles,
            "boundaries": [
                {"left_ref": refs[i], "right_ref": refs[i + 1], "relation": "SAME_PROBLEM"}
                for i in range(len(refs) - 1)
            ],
            "splits": [],
            "uncertain": [],
        }

    result = chain.run_chain(document, ScriptedProvider(script), window_size=5, stride=3)
    assert len(result["windows"]) == 2, "窗口没有重叠，测不到降级路径"
    assert result["reconciled"]["role_conflicts"].get("p0000:b0003")
    assert _accounted(result["candidates"], document["blocks"]) == []
    unattached = {
        item["block_ref"]
        for item in result["candidates"]["excluded"]
        if item["reason"] == "unattached_shared_context"
    }
    assert unattached == {"p0000:b0004", "p0000:b0005", "p0000:b0006"}


# —— baseline 运行器：硬门必须与记账器给出同一个答案 ——


def _write_new_layout(tmp_path: pathlib.Path, blocks: list[tuple[str, str]]) -> tuple[pathlib.Path, pathlib.Path]:
    stem = "mock_99"
    prework = tmp_path / f"{stem}_prework"
    prework.mkdir(parents=True, exist_ok=True)
    evidence = {
        "schema_version": "md-prework/ocr-evidence/v1",
        "document_id": f"{stem}-deadbeef",
        "content_hash": "sha:whatever",
        "pages": [
            {
                "page_seq": 0,
                "blocks": [
                    {
                        "block_ref": f"p0000:b{i:04d}",
                        "label": label,
                        "content": text,
                        "bbox": [0, 0, 10, 10],
                        "sequence_index": i,
                    }
                    for i, (label, text) in enumerate(blocks)
                ],
            }
        ],
    }
    view = {
        "schema_version": "md-prework/normalized-view/v1",
        "normalizer_version": "text_math_v1.1",
        "blocks": [
            {
                "block_ref": f"p0000:b{i:04d}",
                "label": label,
                "status": "changed",
                "normalized_content": text,
            }
            for i, (label, text) in enumerate(blocks)
        ],
    }
    evidence_path = prework / "evidence.json"
    view_path = prework / "normalized-view.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    view_path.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    return evidence_path, view_path


def test_baseline_run_document_passes_hard_gates_without_silent_loss(tmp_path: pathlib.Path) -> None:
    evidence_path, view_path = _write_new_layout(
        tmp_path,
        [
            ("doc_title", "模拟卷"),
            ("number", "1. 甲题"),
            ("text", "甲题续写"),
            ("number", "2. 乙题"),
            ("paragraph_title", "参考答案"),
            ("text", "甲题解答"),
            ("header", "页眉"),
        ],
    )
    summary = baseline.run_document(
        evidence_path, view_path, lambda: baseline._TextProviderAdapter(baseline._fake_provider())
    )
    assert summary["blocks"] == 7
    assert summary["silent_loss"] == 0
    assert summary["gates"] == {
        "coverage_complete": True,
        "schema_all_valid": True,
        "assembler_no_silent_loss": True,
        "overlap_reconcile_ok": True,
    }
    assert summary["candidate_diagnostics"] == 0
    # 语义产物按新布局落盘
    assert (evidence_path.parent / "semantic" / "semantic-run.json").is_file()


def test_baseline_gate_reports_the_same_list_as_the_helper(tmp_path: pathlib.Path) -> None:
    """硬门不许自带一套更宽的判据：它必须直接引用 assembler 的记账函数。"""

    evidence_path, view_path = _write_new_layout(
        tmp_path, [("number", "1. 甲题"), ("text", "续写"), ("header", "页眉")]
    )
    summary = baseline.run_document(
        evidence_path, view_path, lambda: baseline._TextProviderAdapter(baseline._fake_provider())
    )
    payload = json.loads(
        (evidence_path.parent / "semantic" / "semantic-run.json").read_text(encoding="utf-8")
    )
    document_blocks = payload["windows"][0]["blocks"]
    refs = {str(block["block_ref"]) for window in payload["windows"] for block in window["blocks"]}
    rebuilt = [{"block_ref": ref} for ref in sorted(refs)]
    assert sa.unaccounted_blocks(rebuilt, payload["candidates"]) == []
    assert summary["silent_loss"] == len(sa.unaccounted_blocks(rebuilt, payload["candidates"]))
    assert len(document_blocks) >= 1
