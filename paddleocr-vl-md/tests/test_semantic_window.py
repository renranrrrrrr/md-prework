"""Phase 5 窗口组装的确定性不变量测试（离线）。"""

from __future__ import annotations

import json
import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import semantic_window as sw  # noqa: E402


def _document(block_count: int = 12) -> dict:
    blocks = []
    for index in range(block_count):
        page = 0 if index < 7 else 1  # 跨页：第 8 个块起进入第 2 页
        blocks.append(
            sw.block_payload(
                {
                    "block_ref": f"p{page:04d}:b{index:04d}",
                    "label": "text" if index % 4 else "display_formula",
                    "content": f"内容 {index}",
                    "page_seq": page,
                    "sequence_index": index,
                    "bbox": [0, 0, 1, 1],
                },
                {"block_ref": f"p{page:04d}:b{index:04d}", "status": "changed", "normalized_content": f"内容 {index}!"},
                is_page_start=index in (0, 7),
            )
        )
    return {"document_id": "doc-1", "evidence_hash": "abc", "normalizer_version": "normalized-view/v1", "blocks": blocks}


def test_window_size_and_stride() -> None:
    windows = sw.build_windows(_document(12))
    assert [w["start_index"] for w in windows] == [0, 3, 6, 9]
    assert [w["window_size"] for w in windows] == [5, 5, 5, 3], "尾部不足窗口也保留，不丢块"


def test_page_boundary_does_not_reset_window() -> None:
    windows = sw.build_windows(_document(12))
    crossing = [w for w in windows if {b["page_seq"] for b in w["blocks"]} == {0, 1}]
    assert crossing, "窗口必须跨页滑动，不能按页重置"


def test_overlap_blocks_keep_identity() -> None:
    windows = sw.build_windows(_document(12))
    by_ref: dict[str, dict] = {}
    for window in windows:
        for block in window["blocks"]:
            ref = block["block_ref"]
            if ref in by_ref:
                assert by_ref[ref] == block, "同一 block_ref 在不同窗口必须是同一 payload"
            by_ref[ref] = block
    assert len(by_ref) == 12


def test_payload_fields_and_status_handling() -> None:
    document = _document(6)
    document["blocks"][1]["normalization_status"] = "fatal"
    document["blocks"][1]["normalized_text"] = None
    document["blocks"][2]["normalization_status"] = "preserved"
    document["blocks"][2]["normalized_text"] = "原样"
    windows = sw.build_windows(document)
    first = windows[0]["blocks"]
    assert {"block_ref", "label", "raw_text", "normalized_text", "normalization_status",
            "page_seq", "bbox", "sequence_index", "signals"} <= set(first[0])
    assert first[1]["normalization_status"] == "fatal" and first[1]["normalized_text"] is None
    assert first[2]["normalization_status"] == "preserved" and first[2]["normalized_text"] == "原样"
    assert any(signal.startswith("page_start") for signal in first[0]["signals"])


def test_expand_window_only_adds_context() -> None:
    document = _document(20)
    windows = sw.build_windows(document)
    base = windows[2]
    expanded = sw.expand_window(document, base, size=9)
    assert expanded["window_size"] >= base["window_size"]
    base_refs = [b["block_ref"] for b in base["blocks"]]
    expanded_refs = [b["block_ref"] for b in expanded["blocks"]]
    assert set(base_refs) <= set(expanded_refs), "扩窗只加不减"
    for block in expanded["blocks"]:
        if block["block_ref"] in base_refs:
            index = base_refs.index(block["block_ref"])
            assert block == base["blocks"][index], "原有块 payload 不得被改写"
    assert sw.expand_window(document, base, size=13)["window_size"] >= expanded["window_size"]


def test_request_is_deterministic() -> None:
    document = _document(12)
    first = json.dumps(sw.build_request(document), ensure_ascii=False, sort_keys=True)
    second = json.dumps(sw.build_request(_document(12)), ensure_ascii=False, sort_keys=True)
    assert first == second, "同一输入两次组装必须字节级一致"


def test_empty_document() -> None:
    assert sw.build_windows({"blocks": []}) == []
