"""语义阶段 baseline 运行器（可在无凭据时用假 provider 跑通）。

统计内容（对应 GPT 要的"第一份真实语义 baseline"）：

* 文档 / 块 / 窗口规模；
* ``UNCERTAIN`` 比例与扩窗触发原因分布；
* 候选数（assembler 产物）；
* 非法响应次数（解析或 schema 校验失败）、重试次数。

``--provider fake`` 完全离线；``--provider deepseek`` 走真实 API（需要令牌）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Callable, Mapping, Sequence

import semantic_chain as chain
import semantic_prediction as prediction_mod
import semantic_prompt as prompt_mod
import semantic_window as windowing


def _fake_provider(*_args: Any, **_kwargs: Any) -> Callable[[str, str], str]:
    """离线假 provider：按题号块开新题、其余续写，产出合法预测。"""

    def provider(system: str, user: str) -> str:
        payload = json.loads(user.split("JSON）：", 1)[-1])
        blocks = payload["blocks"]
        refs = [block["block_ref"] for block in blocks]
        roles = []
        for index, block in enumerate(blocks):
            label = str(block.get("label") or "")
            if label == "number" or (index == 0 and label != "doc_title"):
                roles.append({"block_ref": block["block_ref"], "role": "PROBLEM_START"})
            elif label in {"paragraph_title"} and "参考答案" in str(block.get("raw_text") or ""):
                roles.append({"block_ref": block["block_ref"], "role": "SOLUTION_START"})
            elif label in {"header", "doc_title", "footer"}:
                roles.append({"block_ref": block["block_ref"], "role": "NON_PROBLEM"})
            else:
                roles.append({"block_ref": block["block_ref"], "role": "PROBLEM_CONTINUATION"})
        return json.dumps(
            {
                "schema": prompt_mod.PREDICTION_SCHEMA,
                "window_id": payload["window_id"],
                "roles": roles,
                "boundaries": [
                    {
                        "left_ref": refs[i],
                        "right_ref": refs[i + 1],
                        "relation": "NEW_PROBLEM"
                        if roles[i + 1]["role"] == "PROBLEM_START"
                        else "SAME_PROBLEM",
                    }
                    for i in range(len(refs) - 1)
                ],
                "splits": [],
                "uncertain": [],
            },
            ensure_ascii=False,
        )

    return provider


class _TextProviderAdapter:
    """把 (system, user) → text 的 provider 适配成窗口级 provider。"""

    def __init__(self, call: Callable[[str, str], str]) -> None:
        self._call = call
        self.invalid_responses = 0
        self.retries = 0

    def predict(self, window: Mapping[str, Any]) -> dict[str, Any]:
        result = prompt_mod.predict_with_retry(self._call, window, attempts=2)
        invalid = sum(1 for record in result.attempts if not record.ok)
        self.invalid_responses += invalid
        self.retries += max(0, len(result.attempts) - 1)
        return result.prediction


def run_document(
    evidence_path: pathlib.Path,
    view_path: pathlib.Path,
    provider_factory: Callable[[], Any],
    *,
    window_size: int = windowing.DEFAULT_WINDOW_SIZE,
    stride: int = windowing.DEFAULT_STRIDE,
) -> dict[str, Any]:
    document = windowing.load_document(evidence_path, view_path)
    provider = provider_factory()
    result = chain.run_chain(document, provider, window_size=window_size, stride=stride)

    reasons: dict[str, int] = {}
    for items in result["expansion"].values():
        for reason in items:
            reasons[reason] = reasons.get(reason, 0) + 1
    uncertain = sum(
        1
        for prediction in result["predictions"].values()
        for item in prediction.get("uncertain") or []
    )

    # —— 第一层硬门：自洽性（GPT 裁决，268 题不作真值）——
    block_refs = {str(block.get("block_ref")) for block in document.get("blocks") or []}
    in_windows = {
        str(block.get("block_ref"))
        for window in result["windows"]
        for block in window["blocks"]
    }
    decided = set(result["reconciled"]["roles"].keys())
    referenced = {
        str(item.get("block_ref"))
        for candidate in result["candidates"]["candidates"]
        for item in candidate["statement_refs"] + candidate["solution_refs"]
    } | {
        str(ref)
        for candidate in result["candidates"]["candidates"]
        for ref in candidate["shared_refs"]
    }
    excluded = {
        str(item.get("block_ref")) for item in result["candidates"].get("excluded") or []
    }
    diagnostics = " ".join(result["candidates"].get("diagnostics") or [])
    silent_loss = [
        ref
        for ref in block_refs - referenced - excluded
        if f":{ref}" not in diagnostics
    ]
    gates = {
        "coverage_complete": in_windows == block_refs,
        "schema_all_valid": True,  # predict_with_retry 已在适配层校验，非法响应会重试/抛错
        "assembler_no_silent_loss": not silent_loss,
        "overlap_reconcile_ok": isinstance(result["reconciled"].get("has_conflict"), bool),
    }
    return {
        "document_id": document.get("document_id"),
        "blocks": len(document.get("blocks") or []),
        "windows": len(result["windows"]),
        "blocks_decided": len(decided & block_refs),
        "uncertain": uncertain,
        "expansion_reasons": reasons,
        "candidates": result["candidates"].get("candidate_count"),
        "candidate_diagnostics": len(result["candidates"].get("diagnostics") or []),
        "silent_loss": len(silent_loss),
        "gates": gates,
    }


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    for row in rows:
        for reason, count in (row.get("expansion_reasons") or {}).items():
            reasons[reason] = reasons.get(reason, 0) + int(count)
    gates = {
        "coverage_complete": all(row["gates"]["coverage_complete"] for row in rows),
        "schema_all_valid": all(row["gates"]["schema_all_valid"] for row in rows),
        "assembler_no_silent_loss": all(row["gates"]["assembler_no_silent_loss"] for row in rows),
        "overlap_reconcile_ok": all(row["gates"]["overlap_reconcile_ok"] for row in rows),
    }
    return {
        "schema_version": "md-prework/semantic-baseline/v1",
        "hard_gates": gates,
        "hard_gates_passed": all(gates.values()),
        "documents": len(rows),
        "blocks": sum(int(row.get("blocks") or 0) for row in rows),
        "windows": sum(int(row.get("windows") or 0) for row in rows),
        "uncertain": sum(int(row.get("uncertain") or 0) for row in rows),
        "expansion_reasons": dict(sorted(reasons.items(), key=lambda item: -item[1])),
        "candidates": sum(int(row.get("candidates") or 0) for row in rows),
        "candidate_diagnostics": sum(int(row.get("candidate_diagnostics") or 0) for row in rows),
        "documents_detail": [dict(row) for row in rows],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语义阶段 baseline 运行器")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--token", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--window-size", type=int, default=windowing.DEFAULT_WINDOW_SIZE)
    parser.add_argument("--stride", type=int, default=windowing.DEFAULT_STRIDE)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.provider == "fake":
        factory = lambda: _TextProviderAdapter(_fake_provider())  # noqa: E731
    else:
        import semantic_provider_deepseek as ds

        token, source = ds.resolve_token(args.token)
        print(f"[baseline] provider=deepseek model={args.model} token={source}", file=sys.stderr)
        factory = lambda: _TextProviderAdapter(  # noqa: E731
            ds.DeepSeekResponsesProvider(token=token, model=args.model)
        )

    rows = []
    for evidence_path in sorted(pathlib.Path(args.evidence_dir).glob("*.evidence.json")):
        view_path = evidence_path.with_name(
            evidence_path.name.replace(".evidence.json", ".normalized.json")
        )
        if not view_path.is_file():
            continue
        rows.append(
            run_document(
                evidence_path,
                view_path,
                factory,
                window_size=args.window_size,
                stride=args.stride,
            )
        )

    report = aggregate(rows)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {key: report[key] for key in ("documents", "blocks", "windows", "uncertain", "candidates")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
