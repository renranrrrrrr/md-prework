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
        expansion[window["window_id"]] = reconcile.expansion_reasons(
            window, prediction, conflicts=merged
        )

    splits: dict[str, dict[str, Any]] = {}
    for prediction in predictions.values():
        for item in prediction.get("splits") or []:
            ref = str(item.get("block_ref"))
            if ref in splits:
                continue
            text = next(
                (str(block.get("raw_text") or "") for block in document.get("blocks") or []
                 if str(block.get("block_ref")) == ref),
                "",
            )
            splits[ref] = assembler.resolve_split(text, str(item.get("anchor") or ""))

    candidates = assembler.assemble(document.get("blocks") or [], merged, splits=splits)
    return {
        "windows": windows,
        "predictions": predictions,
        "reconciled": merged,
        "expansion": expansion,
        "splits": splits,
        "candidates": candidates,
    }
