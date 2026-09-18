"""离线 split 溯源审计：每个 resolved split 是谁提的、当时什么角色、最后落到哪儿。

只读 ``semantic-run.json``：**不调 API、不写回语义产物、不改生产逻辑**。
生产侧 ``semantic_chain`` 对 split 是 first-one-wins（split 不像 role/boundary 那样过
reconcile），所以"某个 split 没落地"既可能是模型判错角色，也可能是 overlap 把它降级了。
没有这份溯源就下不了结论——本工具只把事实摆出来。

口径（对每个 ``status == resolved`` 的 split）：

* ``source_role``：**第一个**提案窗口里该块的角色（first-one-wins 的那个窗口）；
* ``proposers``：按归档窗口顺序列出的**每一条** split 提案（窗口 + 窗口内角色 + anchor）；
  同一窗口可以对同一块提多条，所以 ``proposer_count``（提案数）与
  ``proposer_window_count``（不同窗口数）是两个数，分开统计不要混；
* ``covering_windows``：所有覆盖该块的 overlap 窗口及其角色；
* ``final_role``：默认读**归档**的 ``reconciled``（冻结语义），``--recompute-reconcile`` 才重算；
* ``outcome`` / ``placement``：用当前 correctness 版 assembler 离线重放得到。

    python tools/semantic_split_audit.py --run-dir <blocks_20260918> \
        --output <报告目录>/semantic_split_audit.json --markdown <同上>.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

TOOLS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(TOOLS_DIR.parent))

import semantic_assembler as assembler  # noqa: E402
import semantic_replay_audit as replay  # noqa: E402

PROBLEM_ROLES = ("PROBLEM_START", "PROBLEM_CONTINUATION")
SOLUTION_ROLES = ("SOLUTION_START", "SOLUTION_CONTINUATION")
ALL_ROLES = (
    *PROBLEM_ROLES,
    *SOLUTION_ROLES,
    "SHARED_CONTEXT",
    "NON_PROBLEM",
    "UNCERTAIN",
)


def _proposal_order(payload: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """``block_ref → 按归档窗口顺序列出的 split 提案``（chain 的 first-one-wins 就是这个顺序）。"""

    proposals: dict[str, list[dict[str, str]]] = {}
    for window_id, prediction in (payload.get("predictions") or {}).items():
        for item in prediction.get("splits") or []:
            ref = str(item.get("block_ref"))
            proposals.setdefault(ref, []).append(
                {"window_id": str(window_id), "anchor": str(item.get("anchor") or "")}
            )
    return proposals


def _roles_per_window(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        str(window_id): {
            str(item.get("block_ref")): str(item.get("role") or "")
            for item in (prediction.get("roles") or [])
        }
        for window_id, prediction in (payload.get("predictions") or {}).items()
    }


def _windows_covering(payload: dict[str, Any]) -> dict[str, list[str]]:
    covered: dict[str, list[str]] = {}
    for window in payload.get("windows") or []:
        window_id = str(window.get("window_id"))
        for block in window.get("blocks") or []:
            covered.setdefault(str(block.get("block_ref")), []).append(window_id)
    return covered


def _placement(ref: str, assembled: dict[str, Any], blocks: list[dict[str, Any]]) -> str:
    """块的最终归属：statement / solution / shared / excluded / diagnostic-only / none。"""

    channels: list[str] = []
    for candidate in assembled.get("candidates") or []:
        if any(item.get("block_ref") == ref for item in candidate.get("statement_refs") or []):
            channels.append("statement")
        if any(item.get("block_ref") == ref for item in candidate.get("solution_refs") or []):
            channels.append("solution")
        if ref in (candidate.get("shared_refs") or []):
            channels.append("shared")
    if any(item.get("block_ref") == ref for item in assembled.get("excluded") or []):
        channels.append("excluded")
    if channels:
        return "+".join(dict.fromkeys(channels))
    # 三条记录通道都没走：只有"被诊断点名"和"真的丢了"两种可能
    lost = assembler.unaccounted_blocks([b for b in blocks if str(b.get("block_ref")) == ref], assembled)
    return "none" if lost else "diagnostic-only"


def audit_run(path: pathlib.Path, *, recompute_reconcile: bool = False) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    blocks = replay.blocks_from_windows(payload)
    merged, source = replay.resolve_reconciled(payload, recompute_reconcile=recompute_reconcile)
    roles = merged.get("roles") or {}
    role_conflicts = merged.get("role_conflicts") or {}
    splits = payload.get("splits") or {}
    assembled = assembler.assemble(blocks, merged, splits=splits)
    not_applied = {
        str(line).split(":", 1)[1]
        for line in assembled.get("diagnostics") or []
        if str(line).startswith("split_not_applied:")
    }
    proposals = _proposal_order(payload)
    window_roles = _roles_per_window(payload)
    covering = _windows_covering(payload)
    text_of = {str(block.get("block_ref")): str(block.get("raw_text") or "") for block in blocks}

    entries: list[dict[str, Any]] = []
    for ref, split in splits.items():
        if split.get("status") != "resolved":
            continue
        start, end = int(split["start"]), int(split["end"])
        texts = proposals.get(ref) or []
        anchor = texts[0]["anchor"] if texts else ""
        rescanned = assembler.resolve_split(text_of.get(ref, ""), anchor)
        entries.append(
            {
                "document_id": payload.get("document_id"),
                "block_ref": ref,
                "start": start,
                "end": end,
                "anchor": anchor,
                "first_proposer_window": texts[0]["window_id"] if texts else None,
                "source_role": (window_roles.get(texts[0]["window_id"], {}).get(ref) if texts else None)
                or "NO_ROLE",
                "proposers": [
                    {
                        "window_id": item["window_id"],
                        "anchor": item["anchor"],
                        "role": window_roles.get(item["window_id"], {}).get(ref) or "NO_ROLE",
                    }
                    for item in texts
                ],
                "covering_windows": [
                    {"window_id": wid, "role": window_roles.get(wid, {}).get(ref) or "NO_ROLE"}
                    for wid in covering.get(ref) or []
                ],
                "final_role": str(roles.get(ref) or "NO_ROLE"),
                "role_conflict": ref in role_conflicts,
                "conflict_roles": list(role_conflicts.get(ref) or []),
                "proposer_count": len(texts),
                # 提案次数 ≠ 提案窗口数：同一个窗口可以对同一块列出多条 split
                "proposer_window_count": len({item["window_id"] for item in texts}),
                "distinct_anchors": len({item["anchor"] for item in texts}),
                "outcome": "not_applied" if ref in not_applied else "applied",
                "placement": _placement(ref, assembled, blocks),
                "raw_text": text_of.get(ref, ""),
                # 自检：first-one-wins 重建出来的 anchor 必须仍能解析成归档的 start/end
                "anchor_reproduces_archived_range": rescanned.get("status") == "resolved"
                and (rescanned.get("start"), rescanned.get("end")) == (start, end),
            }
        )
    return {
        "run_file": path.name,
        "document_id": payload.get("document_id"),
        "reconcile_source": source,
        "blocks": len(blocks),
        "resolved_splits": len(entries),
        "archived_split_statuses": {
            str(ref): str(split.get("status")) for ref, split in splits.items()
        },
        "splits": entries,
    }


def _role_distribution(entries: list[dict[str, Any]], outcome: str) -> dict[str, int]:
    counts = {role: 0 for role in ALL_ROLES}
    other = 0
    for entry in entries:
        if entry["outcome"] != outcome:
            continue
        if entry["source_role"] in counts:
            counts[entry["source_role"]] += 1
        else:
            other += 1
    if other:
        counts["OTHER_OR_NO_ROLE"] = other
    return {role: n for role, n in counts.items() if n}


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [entry for run in runs for entry in run["splits"]]
    applied = [e for e in entries if e["outcome"] == "applied"]
    not_applied = [e for e in entries if e["outcome"] == "not_applied"]
    multi = [e for e in entries if e["proposer_count"] > 1]
    multi_window = [e for e in entries if e["proposer_window_count"] > 1]
    return {
        "runs": len(runs),
        "reconcile_source": runs[0]["reconcile_source"] if runs else "",
        "resolved": len(entries),
        "applied": len(applied),
        "not_applied": len(not_applied),
        "not_applied_source_role": _role_distribution(entries, "not_applied"),
        "applied_source_role": _role_distribution(entries, "applied"),
        "not_applied_source_problem_final_uncertain": sum(
            1
            for e in not_applied
            if e["source_role"] in PROBLEM_ROLES and e["final_role"] == "UNCERTAIN"
        ),
        "not_applied_source_problem_final_solution": sum(
            1 for e in not_applied if e["source_role"] in PROBLEM_ROLES and e["final_role"] in SOLUTION_ROLES
        ),
        "not_applied_source_solution": sum(1 for e in not_applied if e["source_role"] in SOLUTION_ROLES),
        "not_applied_source_uncertain": sum(1 for e in not_applied if e["source_role"] == "UNCERTAIN"),
        "multi_proposal_blocks": len(multi),
        "multi_proposal_conflicting_anchor": sum(1 for e in multi if e["distinct_anchors"] > 1),
        "multi_proposal_same_anchor": sum(1 for e in multi if e["distinct_anchors"] == 1),
        "multi_window_proposal_blocks": len(multi_window),
        "single_window_multi_proposal_blocks": len(multi) - len(multi_window),
        "role_conflict_splits": sum(1 for e in entries if e["role_conflict"]),
        "no_role_conflict_but_not_applied": sum(
            1 for e in not_applied if not e["role_conflict"]
        ),
        "anchor_reproduces_archived_range": sum(1 for e in entries if e["anchor_reproduces_archived_range"]),
        "anchor_reconstruction_mismatch": sum(1 for e in entries if not e["anchor_reproduces_archived_range"]),
        "placement_of_not_applied": _placement_distribution(not_applied),
        "placement_of_applied": _placement_distribution(applied),
    }


def _placement_distribution(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["placement"]] = counts.get(entry["placement"], 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _categories(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """抽样用的主要类别：applied、按来源角色分的 not_applied、多提案块、有 role conflict 的块。"""

    categories: dict[str, list[dict[str, Any]]] = {"applied": []}
    for role in ALL_ROLES:
        categories[f"not_applied_source={role}"] = []
    categories["multi_proposal"] = []
    categories["role_conflict_not_applied"] = []
    for entry in entries:
        if entry["outcome"] == "applied":
            categories["applied"].append(entry)
        else:
            key = f"not_applied_source={entry['source_role']}"
            categories.setdefault(key, []).append(entry)
            if entry["role_conflict"]:
                categories["role_conflict_not_applied"].append(entry)
        if entry["proposer_count"] > 1:
            categories["multi_proposal"].append(entry)
    return {name: items for name, items in categories.items() if items}


def render_markdown(runs: list[dict[str, Any]], totals: dict[str, Any], *, sample: int) -> str:
    lines = [
        "# 语义 split 溯源审计（离线，零模型调用）",
        "",
        f"- 重放 run 数：{totals['runs']}；reconcile 来源：**{totals['reconcile_source']}**"
        "（默认读归档的 `reconciled`）",
        f"- resolved split：{totals['resolved']}；applied：{totals['applied']}；"
        f"not_applied：{totals['not_applied']}",
        "",
        "## 汇总",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
    ]
    for key, label in (
        ("applied", "applied（物化进候选的 statement 区间）"),
        ("not_applied", "not_applied（resolved 但当前 contract 无处落地）"),
        (
            "not_applied_source_problem_final_uncertain",
            "not_applied：source=PROBLEM_* 而 final=UNCERTAIN",
        ),
        (
            "not_applied_source_problem_final_solution",
            "not_applied：source=PROBLEM_* 而 final=SOLUTION_*",
        ),
        ("not_applied_source_solution", "not_applied：source 本就是 SOLUTION_*"),
        ("not_applied_source_uncertain", "not_applied：source 本就是 UNCERTAIN"),
        ("multi_proposal_blocks", "同一块被提出多条 split（提案数 > 1）"),
        ("multi_proposal_conflicting_anchor", "└ 其中 anchor 不一致"),
        ("multi_proposal_same_anchor", "└ 其中 anchor 完全一致"),
        ("multi_window_proposal_blocks", "└ 其中来自 ≥2 个不同窗口"),
        ("single_window_multi_proposal_blocks", "└ 其中全部来自同一个窗口"),
        ("role_conflict_splits", "存在 role conflict 的 resolved split"),
        ("no_role_conflict_but_not_applied", "无 role conflict 但仍 not_applied"),
        ("anchor_reproduces_archived_range", "winner anchor 可复现归档 start/end（自检）"),
        ("anchor_reconstruction_mismatch", "└ 自检失败"),
    ):
        lines.append(f"| {label} | {totals[key]} |")
    lines += ["", "### not_applied 的第一提案窗口角色分布", "", "| source role | 数量 |", "| --- | ---: |"]
    for role, count in totals["not_applied_source_role"].items():
        lines.append(f"| {role} | {count} |")
    lines += ["", "### not_applied 的 final reconciled role 与块归属", "", "| final role | 数量 |", "| --- | ---: |"]
    final_roles: dict[str, int] = {}
    for run in runs:
        for entry in run["splits"]:
            if entry["outcome"] == "not_applied":
                final_roles[entry["final_role"]] = final_roles.get(entry["final_role"], 0) + 1
    for role, count in sorted(final_roles.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {role} | {count} |")
    lines += ["", "| 块最终归属（not_applied） | 数量 |", "| --- | ---: |"]
    for placement, count in totals["placement_of_not_applied"].items():
        lines.append(f"| {placement} | {count} |")
    lines += ["", "| 块最终归属（applied） | 数量 |", "| --- | ---: |"]
    for placement, count in totals["placement_of_applied"].items():
        lines.append(f"| {placement} | {count} |")

    lines += ["", "## 逐文档", "", "| document | resolved | applied | not_applied |", "| --- | ---: | ---: | ---: |"]
    for run in runs:
        applied = sum(1 for e in run["splits"] if e["outcome"] == "applied")
        lines.append(
            f"| {run['document_id']} | {run['resolved_splits']} | {applied} | "
            f"{run['resolved_splits'] - applied} |"
        )

    lines += ["", f"## 分类抽样（每类最多 {sample} 条）", ""]
    for name, items in sorted(_categories([e for run in runs for e in run["splits"]]).items()):
        lines.append(f"### {name}（共 {len(items)}）")
        lines.append("")
        for entry in items[:sample]:
            lines += [
                f"- `{entry['block_ref']}` @ {entry['document_id']}（{entry['outcome']}）",
                f"  - raw_text：{entry['raw_text']}",
                f"  - 最终区间：[{entry['start']}, {entry['end']})；anchor：{entry['anchor']!r}",
                f"  - 第一提案窗口：{entry['first_proposer_window']} role={entry['source_role']}",
                f"  - 提案次数 / 提案窗口数：{entry['proposer_count']} / {entry['proposer_window_count']}",
                "  - 全部提案（窗口 role 与 anchor，按归档窗口顺序）："
                + "、".join(f"{p['window_id']}({p['role']})={p['anchor']!r}" for p in entry["proposers"]),
                "  - 覆盖该块的所有窗口："
                + "、".join(f"{w['window_id']}({w['role']})" for w in entry["covering_windows"]),
                f"  - 归档 final role：{entry['final_role']}"
                + (f"（conflict={entry['conflict_roles']}）" if entry["role_conflict"] else ""),
                f"  - 块归属：{entry['placement']}",
                "",
            ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计 resolved split 的来源与落地情况（离线、零调用）")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", default="", help="JSON 报告落盘路径")
    parser.add_argument("--markdown", default="", help="Markdown 报告落盘路径")
    parser.add_argument("--sample", type=int, default=10, help="每类抽样条数（0 = 不输出抽样）")
    parser.add_argument(
        "--recompute-reconcile",
        action="store_true",
        help="用当前 semantic_reconcile 重算 final role（默认读归档，仅用于对比代码漂移）",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    paths = replay.iter_run_files(pathlib.Path(args.run_dir))
    runs = [audit_run(path, recompute_reconcile=args.recompute_reconcile) for path in paths]
    totals = summarize(runs)
    print(json.dumps(totals, ensure_ascii=False, indent=2))

    if args.output:
        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"totals": totals, "runs": runs}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.markdown:
        out = pathlib.Path(args.markdown)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(runs, totals, sample=max(0, args.sample)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
