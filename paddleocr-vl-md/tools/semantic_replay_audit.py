"""离线审计已归档的语义运行产物：把记录的预测重放过一遍 assembler，检查覆盖不变量。

用途：``semantic-run.json`` 里已经存了每个窗口的 ``predictions``、当时的 ``reconciled``
与解析后的 ``splits``，因此**不需要再调用任何模型**就能复算"这些预测物化后有没有块消失"。
改 assembler 之后用它对着冻结的 baseline 重算一遍，就能给出 ``silent_loss`` 前后对比。

**冻结语义优先**：默认直接采用归档的 ``payload["reconciled"]``，不用当前代码重算 reconcile
——否则以后 semantic-v2 一改 reconcile，同一份冻结 baseline 就会"跟着变结论"，重放失去意义。
只有显式传 ``--recompute-reconcile`` 才走当前 ``semantic_reconcile.reconcile``。
归档里缺 ``reconciled`` 时直接报错，绝不悄悄退回重算。

只读：不写回任何产物，结果打到 stdout（可选 ``--markdown`` 落一份报告）。
``split_not_applied`` 只是可观测性计数，不影响退出码——门条件仍是 ``silent_loss == 0``。

    python tools/semantic_replay_audit.py --run-dir <blocks_20260918> [--markdown report.md]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import semantic_assembler as assembler  # noqa: E402
import semantic_reconcile as reconcile  # noqa: E402


RECONCILE_ARCHIVED = "archived"
RECONCILE_RECOMPUTED = "recomputed"


class FrozenReconcileMissing(ValueError):
    """归档产物里没有 ``reconciled``：冻结重放不许悄悄改用当前代码重算。"""


def resolve_reconciled(
    payload: dict[str, Any], *, recompute_reconcile: bool = False
) -> tuple[dict[str, Any], str]:
    """返回 ``(reconcile 结果, 来源)``；默认来源是归档，只有显式要求才重算。"""

    if recompute_reconcile:
        merged = reconcile.reconcile(list((payload.get("predictions") or {}).values()))
        return merged, RECONCILE_RECOMPUTED
    merged = payload.get("reconciled")
    if not isinstance(merged, dict) or not isinstance(merged.get("roles"), dict):
        raise FrozenReconcileMissing(
            "归档的 semantic-run.json 缺少 reconciled：拒绝静默退回当前 reconcile，"
            "要重算请显式加 --recompute-reconcile"
        )
    return merged, RECONCILE_ARCHIVED


def iter_run_files(root: pathlib.Path) -> list[pathlib.Path]:
    """新旧两种布局的语义运行产物：``<stem>.semantic-run.json`` 与 ``semantic/semantic-run.json``。"""

    return sorted(
        {
            *root.glob("*.semantic-run.json"),
            *root.glob("*_prework/semantic/semantic-run.json"),
            *root.glob("*/semantic-run.json"),
        }
    )


def blocks_from_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """按首次出现顺序还原文档级块流（窗口重叠，块只取一次）。"""

    seen: dict[str, dict[str, Any]] = {}
    for window in payload.get("windows") or []:
        for block in window.get("blocks") or []:
            seen.setdefault(str(block.get("block_ref")), block)
    return list(seen.values())


def audit_run(path: pathlib.Path, *, recompute_reconcile: bool = False) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    blocks = blocks_from_windows(payload)
    merged, source = resolve_reconciled(payload, recompute_reconcile=recompute_reconcile)
    assembled = assembler.assemble(blocks, merged, splits=payload.get("splits") or {})
    lost = assembler.unaccounted_blocks(blocks, assembled)
    roles = merged.get("roles") or {}
    unattached = {
        str(item.get("block_ref"))
        for item in assembled.get("excluded") or []
        if item.get("reason") == "unattached_shared_context"
    }
    not_applied = sorted(
        str(line).split(":", 1)[1]
        for line in assembled.get("diagnostics") or []
        if str(line).startswith("split_not_applied:")
    )
    # 角色是"为什么落不了地"的第一手证据：题面块 / 解析块 / 窗口外引用（NO_ROLE）
    not_applied_roles = {ref: str(roles.get(ref) or "NO_ROLE") for ref in not_applied}
    resolved = sorted(
        str(ref) for ref, split in (payload.get("splits") or {}).items()
        if split.get("status") == "resolved"
    )
    return {
        "run_file": path.name,
        "document_id": payload.get("document_id"),
        "reconcile_source": source,
        "blocks": len(blocks),
        "candidates": assembled.get("candidate_count"),
        "silent_loss": len(lost),
        "silent_loss_refs": lost,
        "role_of_lost": sorted({str(roles.get(ref)) for ref in lost}),
        "unattached_shared_context": len(unattached),
        "resolved_splits": len(resolved),
        "split_not_applied": len(not_applied),
        "split_not_applied_refs": not_applied,
        "split_not_applied_roles": not_applied_roles,
    }


def _merge_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """把各文档的"未落地 split → 角色"合并成角色分布（按数量降序）。"""

    merged: dict[str, int] = {}
    for row in rows:
        for role in (row.get("split_not_applied_roles") or {}).values():
            merged[role] = merged.get(role, 0) + 1
    return dict(sorted(merged.items(), key=lambda item: (-item[1], item[0])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重放归档的语义运行，审计覆盖不变量（离线、零调用）")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", default="", help="JSON 报告落盘路径")
    parser.add_argument("--markdown", default="", help="Markdown 报告落盘路径")
    parser.add_argument("--list-refs", action="store_true", help="逐条打印丢失块的 block_ref")
    parser.add_argument(
        "--recompute-reconcile",
        action="store_true",
        help="用当前 semantic_reconcile 重算，而不是读归档的 reconciled（仅用于对比代码漂移）",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = pathlib.Path(args.run_dir)
    rows = []
    failed: list[str] = []
    for path in iter_run_files(root):
        try:
            rows.append(audit_run(path, recompute_reconcile=args.recompute_reconcile))
        except FrozenReconcileMissing as error:
            failed.append(f"{path}: {error}")
    if failed:
        for line in failed:
            print(f"ERROR {line}", file=sys.stderr)
        return 2
    totals = {
        "runs": len(rows),
        "reconcile_source": rows[0]["reconcile_source"] if rows else "",
        "blocks": sum(row["blocks"] for row in rows),
        "candidates": sum(row["candidates"] for row in rows),
        "silent_loss": sum(row["silent_loss"] for row in rows),
        "unattached_shared_context": sum(row["unattached_shared_context"] for row in rows),
        "resolved_splits": sum(row["resolved_splits"] for row in rows),
        "split_not_applied": sum(row["split_not_applied"] for row in rows),
        "split_not_applied_by_role": _merge_counts(rows),
        "documents_with_loss": sum(1 for row in rows if row["silent_loss"]),
    }
    lines = [
        f"reconcile 来源：{totals['reconcile_source']}",
        "run 文件            块数  候选  silent_loss  尾部共享材料  resolved_split  split_not_applied",
        "-" * 88,
    ]
    for row in rows:
        lines.append(
            f"{row['run_file'][:20]:<22}{row['blocks']:>6}{row['candidates']:>6}"
            f"{row['silent_loss']:>13}{row['unattached_shared_context']:>16}"
            f"{row['resolved_splits']:>18}{row['split_not_applied']:>20}"
        )
    lines.append("-" * 88)
    lines.append(
        f"合计                {totals['blocks']:>6}{totals['candidates']:>6}"
        f"{totals['silent_loss']:>13}{totals['unattached_shared_context']:>16}"
        f"{totals['resolved_splits']:>18}{totals['split_not_applied']:>20}"
    )
    if totals["split_not_applied"]:
        lines.append(
            "未落地 split 的角色分布："
            + "、".join(f"{role}={n}" for role, n in totals["split_not_applied_by_role"].items())
        )
    if args.list_refs:
        for row in rows:
            for ref in row["silent_loss_refs"]:
                lines.append(f"  LOST {row['document_id']} {ref}")
            for ref, role in sorted((row["split_not_applied_roles"] or {}).items()):
                lines.append(f"  SPLIT-NOT-APPLIED {row['document_id']} {ref} role={role}")
    report = "\n".join(lines)
    print(report)

    if args.output:
        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"totals": totals, "runs": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.markdown:
        out = pathlib.Path(args.markdown)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "# 语义 baseline 覆盖不变量重放\n\n```text\n" + report + "\n```\n", encoding="utf-8"
        )
    return 0 if not totals["silent_loss"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
