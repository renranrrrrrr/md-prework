"""Phase 5 第一步：确定性 5-block 语义窗口组装（不调用任何模型）。

本层只做**确定性**的窗口与 payload 组装，用来证明"同一份 Evidence 能稳定产出
5 / 9 / 13 block 的语义请求 payload"。这里**不写任何 LLM prompt**。

不变量（按 GPT 的裁决逐条钉死）：

* 窗口只**引用** ``block_ref``，不复制、不修改 Evidence；
* 窗口内顺序严格按文档 ``(page_seq, sequence_index)``；
* 跨窗口重叠的 block 保持同一 identity（同一 ``block_ref`` 同一 payload）；
* ``fatal`` / ``preserved`` 的块仍然进入窗口（并带 ``normalization_status``）；
* 扩窗只增加上下文，不改变任何既有块的 payload；
* 生成完全确定：同一输入两次运行字节级相同。

第一版固定参数：``window_size = 5``、``stride = 3``、自适应 ``5 → 9 → 13``，
**页边界不重置窗口**（窗口在整份文档的 block 流上滑动）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any, Mapping, Sequence


SEMANTIC_REQUEST_SCHEMA = "md-prework/semantic-request/v1"
DEFAULT_WINDOW_SIZE = 5
DEFAULT_STRIDE = 3
ADAPTIVE_WINDOW_SIZES = (5, 9, 13)

_MATH_ENV_RE = re.compile(r"\$[^$\n]*\$")


def block_payload(
    block: Mapping[str, Any],
    view_block: Mapping[str, Any] | None,
    *,
    is_page_start: bool = False,
) -> dict[str, Any]:
    """单个 block 的窗口 payload（九个字段 + 确定性 signals）。"""

    label = str(block.get("label") or "")
    status = str((view_block or {}).get("status") or "unknown")
    normalized = (view_block or {}).get("normalized_content")
    if status in {"preserved", "fatal"} and normalized is None:
        normalized = None if status == "fatal" else block.get("content")
    signals: list[str] = [f"label:{label}", f"normalization:{status}"]
    if is_page_start:
        signals.append("page_start")
    if label in {"display_formula", "inline_formula"}:
        signals.append("formula_block")
    if _MATH_ENV_RE.search(str(block.get("content") or "")):
        signals.append("has_math_env")
    return {
        "block_ref": block.get("block_ref"),
        "label": label,
        "raw_text": block.get("content") or "",
        "normalized_text": normalized,
        "normalization_status": status,
        "page_seq": block.get("page_seq"),
        "bbox": block.get("bbox"),
        "sequence_index": block.get("sequence_index"),
        "signals": signals,
    }


def load_document(evidence_path: pathlib.Path, view_path: pathlib.Path) -> dict[str, Any]:
    """把 Evidence + Normalized View 拼成文档级 block 流（不修改任何输入）。"""

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    view = json.loads(view_path.read_text(encoding="utf-8"))
    view_by_ref = {str(item.get("block_ref")): item for item in view.get("blocks") or []}

    ordered: list[dict[str, Any]] = []
    for page in sorted(evidence.get("pages") or [], key=lambda item: item.get("page_seq") or 0):
        blocks = sorted(page.get("blocks") or [], key=lambda item: item.get("sequence_index") or 0)
        for position, block in enumerate(blocks):
            merged = dict(block)
            merged["page_seq"] = page.get("page_seq")
            ordered.append(
                block_payload(
                    merged,
                    view_by_ref.get(str(block.get("block_ref"))),
                    is_page_start=position == 0,
                )
            )
    return {
        "document_id": evidence.get("document_id"),
        "evidence_hash": evidence.get("content_hash"),
        "normalizer_version": view.get("normalizer_version"),
        "blocks": ordered,
    }


def build_windows(
    document: Mapping[str, Any],
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    stride: int = DEFAULT_STRIDE,
) -> list[dict[str, Any]]:
    """按固定窗口与步长切窗口；最后一段不足窗口也保留（不丢尾部 block）。"""

    blocks = list(document.get("blocks") or [])
    windows: list[dict[str, Any]] = []
    if not blocks:
        return windows
    step = max(1, stride)
    size = max(1, window_size)
    index = 0
    while index < len(blocks):
        chunk = blocks[index : index + size]
        windows.append(
            {
                "window_index": len(windows),
                "start_index": index,
                "end_index": index + len(chunk),
                "window_size": len(chunk),
                "adaptive_size": size,
                "blocks": chunk,
            }
        )
        if index + size >= len(blocks):
            break
        index += step
    return windows


def expand_window(
    document: Mapping[str, Any],
    window: Mapping[str, Any],
    *,
    size: int,
) -> dict[str, Any]:
    """把窗口对称扩到 ``size``（5→9→13）；只增加上下文，不改动原有块 payload。"""

    blocks = list(document.get("blocks") or [])
    start = int(window.get("start_index") or 0)
    end = int(window.get("end_index") or start)
    center = (start + end) // 2
    extra = max(0, size - (end - start))
    left = extra // 2
    right = extra - left
    new_start = max(0, start - left)
    new_end = min(len(blocks), end + right)
    # 保证扩窗后至少达到目标大小（受文档边界限制）
    while new_end - new_start < min(size, len(blocks)) and (new_start > 0 or new_end < len(blocks)):
        if new_start > 0:
            new_start -= 1
        elif new_end < len(blocks):
            new_end += 1
        else:
            break
    return {
        "window_index": window.get("window_index"),
        "start_index": new_start,
        "end_index": new_end,
        "window_size": new_end - new_start,
        "adaptive_size": size,
        "center_index": center,
        "blocks": blocks[new_start:new_end],
        "expanded_from": window.get("window_size"),
    }


def build_request(
    document: Mapping[str, Any],
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    stride: int = DEFAULT_STRIDE,
) -> dict[str, Any]:
    payload = {
        "schema_version": SEMANTIC_REQUEST_SCHEMA,
        "document_id": document.get("document_id"),
        "evidence_hash": document.get("evidence_hash"),
        "normalizer_version": document.get("normalizer_version"),
        "block_count": len(document.get("blocks") or []),
        "window_size": window_size,
        "stride": stride,
        "adaptive_window_sizes": list(ADAPTIVE_WINDOW_SIZES),
        "windows": build_windows(document, window_size=window_size, stride=stride),
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确定性 5-block 语义窗口组装（不调用模型）")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--view", default="", help="Normalized View；默认同名 .normalized.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument("--stride", type=int, default=DEFAULT_STRIDE)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    evidence_path = pathlib.Path(args.evidence)
    if args.view:
        view_path = pathlib.Path(args.view)
    else:
        import prework_paths

        view_path = prework_paths.view_path_for(evidence_path)
    document = load_document(evidence_path, view_path)
    request = build_request(document, window_size=args.window_size, stride=args.stride)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "document_id": request["document_id"],
                "blocks": request["block_count"],
                "windows": len(request["windows"]),
                "window_size": request["window_size"],
                "stride": request["stride"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
