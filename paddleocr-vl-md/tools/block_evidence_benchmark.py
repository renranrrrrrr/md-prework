"""Block Evidence Benchmark V1 —— 只用真实证据做结构统计，不联网、不调用模型。

它回答的是"这份 evidence 能不能承载后面的 block 级工作"，而不是"切题对不对"：

* 文档 / 页 / 块规模与 ``label`` 分布；
* ``provider_block_order`` 的 present / null / monotonic / conflicts（数组顺序永远是 canonical）；
* 几何完整率（bbox / polygon）；
* 图片块与素材登记的对应关系；
* 公式块分布；
* **Evidence extraction loss**：provider raw page 里的块是否全部进入证据（必须为 0）。

用法：

    python tools/block_evidence_benchmark.py \
      --evidence-dir <目录，含 *.evidence.json> \
      --output <报告 JSON 路径> [--markdown <摘要 md 路径>]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import prework_paths  # noqa: E402


FORMULA_LABELS = ("display_formula", "inline_formula")
IMAGE_LABELS = ("image", "chart")


def load_evidence(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_block_count(raw_dir: pathlib.Path, page_seq: int) -> int | None:
    """读 provider raw page，返回 ``parsing_res_list`` 的条数（读不到返回 None）。"""

    candidates = [
        raw_dir / f"page-{page_seq:04d}.json",
        raw_dir / f"page-{page_seq + 1:04d}.json",  # 兼容早期 1-based 命名
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, Mapping) and isinstance(payload.get("parsing_res_list"), list):
            return len(payload["parsing_res_list"])
        if isinstance(payload, Mapping):
            pruned = payload.get("pruned_result")
            if isinstance(pruned, Mapping) and isinstance(pruned.get("parsing_res_list"), list):
                return len(pruned["parsing_res_list"])
    return None


def summarize_document(evidence_path: pathlib.Path) -> dict[str, Any]:
    """单份文档的结构统计。"""

    evidence = load_evidence(evidence_path)
    raw_dir = prework_paths.raw_dir_for(evidence_path)

    label_counts: dict[str, int] = {}
    blocks_total = 0
    order_present = 0
    order_null = 0
    order_conflicts = 0
    order_non_monotonic_pages = 0
    bbox_present = 0
    polygon_present = 0
    image_blocks = 0
    formula_blocks = 0
    block_counts_per_page: list[int] = []
    extraction_loss = 0
    raw_pages_read = 0
    pages_with_loss: list[int] = []

    for page in evidence.get("pages") or []:
        blocks = list(page.get("blocks") or [])
        block_counts_per_page.append(len(blocks))
        blocks_total += len(blocks)
        for block in blocks:
            label = str(block.get("label") or "")
            label_counts[label] = label_counts.get(label, 0) + 1
            if block.get("provider_block_order") is None:
                order_null += 1
            else:
                order_present += 1
            if block.get("bbox"):
                bbox_present += 1
            if block.get("polygon"):
                polygon_present += 1
            if label in IMAGE_LABELS:
                image_blocks += 1
            if label in FORMULA_LABELS:
                formula_blocks += 1
        stats = page.get("order_consistency") or {}
        order_conflicts += int(stats.get("block_order_conflicts") or 0)
        if stats.get("block_order_monotonic") is False:
            order_non_monotonic_pages += 1

        raw_count = _raw_block_count(raw_dir, int(page.get("page_seq") or 0))
        if raw_count is not None:
            raw_pages_read += 1
            if raw_count != len(blocks):
                loss = raw_count - len(blocks)
                extraction_loss += loss
                pages_with_loss.append(int(page.get("page_seq") or 0))

    assets = list(evidence.get("assets") or [])
    saved = sum(1 for asset in assets if asset.get("state") == "saved")
    missing = sum(1 for asset in assets if asset.get("state") == "missing")

    return {
        "document_id": evidence.get("document_id") or evidence_path.stem,
        "evidence_file": evidence_path.name,
        "page_count": int(evidence.get("page_count") or 0),
        "block_count": blocks_total,
        "blocks_per_page_min": min(block_counts_per_page) if block_counts_per_page else 0,
        "blocks_per_page_max": max(block_counts_per_page) if block_counts_per_page else 0,
        "label_counts": dict(sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))),
        "order": {
            "present": order_present,
            "null": order_null,
            "null_ratio": round(order_null / blocks_total, 4) if blocks_total else None,
            "conflicts": order_conflicts,
            "non_monotonic_pages": order_non_monotonic_pages,
        },
        "geometry": {
            "bbox_present_ratio": round(bbox_present / blocks_total, 4) if blocks_total else None,
            "polygon_present_ratio": round(polygon_present / blocks_total, 4) if blocks_total else None,
        },
        "images": {
            "image_blocks": image_blocks,
            "assets_total": len(assets),
            "assets_saved": saved,
            "assets_missing": missing,
        },
        "formula_blocks": formula_blocks,
        "extraction": {
            "raw_pages_read": raw_pages_read,
            "loss_blocks": extraction_loss,
            "pages_with_loss": pages_with_loss,
        },
    }


def build_report(evidence_paths: Sequence[pathlib.Path]) -> dict[str, Any]:
    documents = [summarize_document(path) for path in sorted(evidence_paths)]

    totals = {
        "documents": len(documents),
        "pages": sum(doc["page_count"] for doc in documents),
        "blocks": sum(doc["block_count"] for doc in documents),
        "formula_blocks": sum(doc["formula_blocks"] for doc in documents),
        "image_blocks": sum(doc["images"]["image_blocks"] for doc in documents),
        "assets_total": sum(doc["images"]["assets_total"] for doc in documents),
        "assets_saved": sum(doc["images"]["assets_saved"] for doc in documents),
        "assets_missing": sum(doc["images"]["assets_missing"] for doc in documents),
        "extraction_loss_blocks": sum(doc["extraction"]["loss_blocks"] for doc in documents),
        "documents_with_extraction_loss": sum(
            1 for doc in documents if doc["extraction"]["loss_blocks"]
        ),
        "raw_pages_read": sum(doc["extraction"]["raw_pages_read"] for doc in documents),
    }

    labels: dict[str, int] = {}
    for doc in documents:
        for label, count in doc["label_counts"].items():
            labels[label] = labels.get(label, 0) + count

    block_counts = [doc["block_count"] for doc in documents]
    report = {
        "schema_version": "md-prework/block-evidence-benchmark/v1",
        "totals": totals,
        "labels": dict(sorted(labels.items(), key=lambda item: (-item[1], item[0]))),
        "per_document_blocks": {
            "min": min(block_counts) if block_counts else 0,
            "p50": int(statistics.median(block_counts)) if block_counts else 0,
            "max": max(block_counts) if block_counts else 0,
        },
        "extraction": {
            "ok": totals["extraction_loss_blocks"] == 0 and totals["documents_with_extraction_loss"] == 0,
            "note": "loss = provider raw page 的块数 - evidence 的块数；必须为 0",
        },
        "documents": documents,
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        "# Block Evidence Benchmark V1",
        "",
        f"- 文档 {totals['documents']} 份，页 {totals['pages']}，块 {totals['blocks']}",
        f"- 公式块 {totals['formula_blocks']}，图片块 {totals['image_blocks']}，"
        f"素材 {totals['assets_saved']}/{totals['assets_total']} 已落盘（缺失 {totals['assets_missing']}）",
        f"- Evidence extraction loss：{totals['extraction_loss_blocks']}（"
        f"读取 provider raw 页 {totals['raw_pages_read']} 页，"
        f"有损文档 {totals['documents_with_extraction_loss']} 份）"
        f" → {'OK' if report['extraction']['ok'] else 'FAILED'}",
        "",
        "## label 分布",
        "",
        "| label | blocks |",
        "| --- | --- |",
    ]
    for label, count in report["labels"].items():
        lines.append(f"| `{label}` | {count} |")

    lines += [
        "",
        "## block_order 与几何完整率",
        "",
        "| 文档 | 块 | null order | null 比例 | conflicts | bbox | polygon |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for doc in report["documents"]:
        order = doc["order"]
        geometry = doc["geometry"]
        lines.append(
            f"| {doc['document_id']} | {doc['block_count']} | {order['null']} | "
            f"{order['null_ratio']} | {order['conflicts']} | "
            f"{geometry['bbox_present_ratio']} | {geometry['polygon_present_ratio']} |"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Block Evidence Benchmark V1（离线统计）")
    parser.add_argument("--evidence-dir", required=True, help="包含 *.evidence.json 的目录")
    parser.add_argument("--pattern", default="*.evidence.json", help="证据文件匹配模式")
    parser.add_argument("--output", required=True, help="报告 JSON 输出路径")
    parser.add_argument("--markdown", default="", help="可选的 Markdown 摘要输出路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    directory = pathlib.Path(args.evidence_dir)
    paths = prework_paths.iter_evidence(directory) if args.pattern == "*.evidence.json" else sorted(directory.glob(args.pattern))
    if not paths:
        print(f"没有找到证据文件：{directory}\\{args.pattern}", file=sys.stderr)
        return 2

    report = build_report(paths)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        markdown_path = pathlib.Path(args.markdown)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(report), encoding="utf-8")

    totals = report["totals"]
    print(
        json.dumps(
            {
                "documents": totals["documents"],
                "pages": totals["pages"],
                "blocks": totals["blocks"],
                "formula_blocks": totals["formula_blocks"],
                "extraction_loss_blocks": totals["extraction_loss_blocks"],
                "extraction_ok": report["extraction"]["ok"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["extraction"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
