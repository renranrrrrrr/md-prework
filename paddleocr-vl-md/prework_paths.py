"""md-prework 产物布局的唯一真源（新旧两种布局都认）。

新布局（本轮起）::

    <docs>/<stem>.pdf                    # 原始 PDF
    <docs>/<stem>.md                     # 最终规范化成品
    <docs>/<stem>_prework/
        evidence.json                    # Stable Evidence（不可变）
        normalized-view.json             # 派生视图（不可变证据的派生）
        raw/                             # Paddle 原始 Markdown 与 page-level raw payload
        media/                           # 图片素材
        semantic/                        # 语义层产物（预留）
        diagnostics/                     # 运行诊断（producer.json 等）

旧布局（兼容读取，不再产出）::

    <docs>/<stem>.evidence.json
    <docs>/<stem>.normalized.json
    <docs>/<stem>_raw/page-NNNN.json
    <docs>/<stem>_media/
"""

from __future__ import annotations

import json
import pathlib


def prework_dir_for(evidence_path: pathlib.Path) -> pathlib.Path | None:
    """证据所在的 ``_prework`` 目录；旧布局返回 None。"""

    if evidence_path.name == "evidence.json" and evidence_path.parent.name.endswith("_prework"):
        return evidence_path.parent
    return None


def view_path_for(evidence_path: pathlib.Path) -> pathlib.Path:
    """Normalized View 的位置（新布局优先，其次旧命名）。"""

    prework = prework_dir_for(evidence_path)
    if prework is not None:
        return prework / "normalized-view.json"
    return evidence_path.with_name(
        evidence_path.name.replace(".evidence.json", ".normalized.json")
    )


def raw_dir_for(evidence_path: pathlib.Path) -> pathlib.Path:
    """provider 原始产物的目录（新布局优先，其次旧命名）。"""

    prework = prework_dir_for(evidence_path)
    if prework is not None:
        return prework / "raw"
    stem = evidence_path.name.replace(".evidence.json", "")
    return evidence_path.parent / f"{stem}_raw"


def semantic_dir_for(evidence_path: pathlib.Path) -> pathlib.Path:
    prework = prework_dir_for(evidence_path)
    if prework is not None:
        return prework / "semantic"
    return evidence_path.parent


def diagnostics_dir_for(evidence_path: pathlib.Path) -> pathlib.Path:
    prework = prework_dir_for(evidence_path)
    if prework is not None:
        return prework / "diagnostics"
    return evidence_path.parent


def iter_evidence(root: pathlib.Path) -> list[pathlib.Path]:
    """列出目录下的证据文件：同时支持 ``*.evidence.json`` 与 ``*/evidence.json``。"""

    return sorted([*root.glob("*.evidence.json"), *root.glob("*/evidence.json")])


def load_pair(evidence_path: pathlib.Path) -> tuple[dict, dict | None]:
    """读证据与（若存在）派生视图，供工具复用。"""

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    view_path = view_path_for(evidence_path)
    view = json.loads(view_path.read_text(encoding="utf-8")) if view_path.is_file() else None
    return evidence, view
