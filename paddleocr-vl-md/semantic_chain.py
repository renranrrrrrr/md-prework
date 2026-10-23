"""零模型端到端链：payload → fake provider → reconcile → 扩窗 → split → assembler。

只做编排，不调用任何模型；输出完全确定，便于验证"同一份 Evidence 每次得到同样结果"。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

import semantic_assembler as assembler
import semantic_prediction as prediction_module
import semantic_reconcile as reconcile
import semantic_window as windowing


class PredictionProvider(Protocol):
    def predict(self, window: Mapping[str, Any]) -> dict[str, Any]: ...


def run_chain(
    document: Mapping[str, Any],
    provider: PredictionProvider,
    *,
    window_size: int = windowing.DEFAULT_WINDOW_SIZE,
    stride: int = windowing.DEFAULT_STRIDE,
) -> dict[str, Any]:
    """跑完整条链；每一步都是确定性的。"""

    windows = prediction_module.with_window_id(
        windowing.build_windows(document, window_size=window_size, stride=stride)
    )
    predictions = {window["window_id"]: provider.predict(window) for window in windows}

    merged = reconcile.reconcile(list(predictions.values()))

    expansion: dict[str, list[str]] = {}
    for window in windows:
        prediction = predictions[window["window_id"]]
        expansion[window["window_id"]] = reconcile.expansion_reasons(window, prediction, conflicts=merged)

    splits = resolve_prediction_splits(document.get("blocks") or [], list(predictions.values()))

    candidates = assembler.assemble(document.get("blocks") or [], merged, splits=splits)
    return {
        "windows": windows,
        "predictions": predictions,
        "reconciled": merged,
        "expansion": expansion,
        "splits": splits,
        "candidates": candidates,
    }


def resolve_prediction_splits(
    blocks: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """汇总所有窗口的切分提议；不同边界或未解析提议并存时保留冲突，不取第一次。

    同一位置的长短 anchor 视为同一边界。一块内多个不同起点暂不自动多次切分，
    交由诊断与人工处理；顺序变化不应改变结果。
    """

    texts = {str(block.get("block_ref")): str(block.get("raw_text") or "") for block in blocks}
    anchors: dict[str, set[str]] = {}
    for prediction in predictions:
        for item in prediction.get("splits") or []:
            ref = str(item.get("block_ref"))
            anchors.setdefault(ref, set()).add(str(item.get("anchor") or ""))
    splits: dict[str, dict[str, Any]] = {}
    for ref in sorted(anchors):
        proposals = [
            {"anchor": anchor, **assembler.resolve_split(texts.get(ref, ""), anchor)}
            for anchor in sorted(anchors[ref])
        ]
        if (
            all(item["status"] == "resolved" for item in proposals)
            and len({item["start"] for item in proposals}) == 1
        ):
            splits[ref] = {
                "status": "resolved",
                "start": proposals[0]["start"],
                "end": min(item["end"] for item in proposals),
            }
        elif len(proposals) == 1:
            splits[ref] = {key: value for key, value in proposals[0].items() if key != "anchor"}
        else:
            splits[ref] = {"status": "conflict", "proposals": proposals}
    return splits
