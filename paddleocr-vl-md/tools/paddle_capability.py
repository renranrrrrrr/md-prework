"""PaddleOCR-VL 结构化返回的 capability probe（纯函数层）。

本模块只回答一个问题：**当前 PaddleOCR-VL / AI Studio 链路到底返回了哪些结构证据。**

职责边界（这条链路的红线）：

- 不联网、不发请求（真实调用只在 ``probe_paddle_capabilities.py`` 里发生）；
- 不改变任何生产逻辑，不实现任何 layout detector 的替代品；
- 只做三件事：描述对象结构、判定字段可用性、脱敏成可提交的 fixture；
- 输出里不得出现令牌、请求头、签名 URL 或 base64 图片负载。

判定分层（对应验收里的 A / B / C 三选一）：

- ``A`` 服务端没有返回；
- ``B`` SDK 返回但 MCP wrapper 丢掉；
- ``C`` 已经存在、只是当前代码没有读取。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CAPABILITY_SCHEMA_VERSION = "md-prework/paddle-capability-report/v1"
FIXTURE_SCHEMA_VERSION = "md-prework/paddle-structured-result-fixture/v1"

#: PaddleX / AI Studio 文档解析结果里承载版面块的键名（按优先级探测，不写死结论）。
BLOCK_LIST_KEYS = ("parsing_res_list", "parsingResList", "blocks")

#: 块级字段的候选键名：探测到任意一个即判定该能力可用。
BLOCK_FIELD_KEYS: Mapping[str, tuple[str, ...]] = {
    "block_id": ("block_id", "blockId", "id"),
    "block_order": ("block_order", "blockOrder", "order", "reading_order"),
    "block_label": ("block_label", "blockLabel", "label"),
    "block_bbox": ("block_bbox", "blockBbox", "bbox"),
    "block_content": ("block_content", "blockContent", "content"),
}

#: 可能泄漏敏感信息的键名（命中即脱敏，无论值形态）。
SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|signature|sign|"
    r"access[_-]?key|session)",
    re.IGNORECASE,
)

#: 形似令牌/签名的长随机串（用于最后的自检扫描）。
SECRET_VALUE_PATTERN = re.compile(r"(?<![A-Za-z0-9_\-/])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-/])")

#: 这些键本身允许出现长标识（schema 名、版本号、路径），不参与自检。
SCAN_EXEMPT_KEYS = frozenset(
    {
        "schema_version",
        "version",
        "note",
        "tool",
        "model",
        "page_ranges",
        "input_name",
        "token_source",
        "page_index_source",
        "gap_classification",
        "input_sha256_hint",
    }
)

URL_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
DATA_URL_PATTERN = re.compile(r"^data:", re.IGNORECASE)


def type_name(value: Any) -> str:
    """返回稳定的类型描述（用于结构树，不依赖具体类对象）。"""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _object_to_mapping(value: Any) -> Any:
    """把 dataclass / 普通对象转换成可序列化结构；未知对象原样返回。"""

    if isinstance(value, Mapping):
        return {str(key): _object_to_mapping(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_object_to_mapping(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _object_to_mapping(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    return value


def structure_map(value: Any, *, max_depth: int = 5, max_items: int = 3) -> dict[str, Any]:
    """把任意返回对象压成 ``{key: type}`` 结构树，供人工审阅。"""

    if max_depth < 0:
        return {"__type__": type_name(value), "__truncated__": "depth"}

    kind = type_name(value)
    if kind == "object":
        mapping = value if isinstance(value, Mapping) else vars(value)
        result: dict[str, Any] = {"__type__": "object", "__keys__": list(mapping.keys())}
        for key in list(mapping.keys())[: max(1, max_items * 4)]:
            result[str(key)] = structure_map(
                mapping[key], max_depth=max_depth - 1, max_items=max_items
            )
        return result
    if kind == "array":
        items = list(value)
        result = {"__type__": "array", "__len__": len(items)}
        if items:
            result["__item__"] = structure_map(
                items[0], max_depth=max_depth - 1, max_items=max_items
            )
        return result
    if kind == "str":
        return {"__type__": "str", "__chars__": len(value)}
    return {"__type__": kind}


def iter_key_paths(value: Any, *, max_depth: int = 6, prefix: str = "") -> list[str]:
    """列出结构中出现过的点分键路径（用于回答"到底有哪些字段"）。"""

    paths: list[str] = []
    if max_depth < 0:
        return paths
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(iter_key_paths(item, max_depth=max_depth - 1, prefix=path))
    elif isinstance(value, (list, tuple)):
        for item in value[:1]:
            paths.extend(iter_key_paths(item, max_depth=max_depth - 1, prefix=f"{prefix}[]"))
    return paths


def find_block_list(payload: Any) -> list[Any]:
    """在任意层级里找到第一个块列表（``parsing_res_list`` 等），找不到返回空列表。"""

    if isinstance(payload, Mapping):
        for key in BLOCK_LIST_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return list(candidate)
        for item in payload.values():
            found = find_block_list(item)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found = find_block_list(item)
            if found:
                return found
    return []


def _has_any_key(payload: Any, keys: Sequence[str]) -> bool:
    if isinstance(payload, Mapping):
        if any(key in payload for key in keys):
            return True
        return any(_has_any_key(item, keys) for item in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_has_any_key(item, keys) for item in payload)
    return False


def block_field_report(payload: Any) -> dict[str, bool]:
    """在块列表里逐个字段判定可用性（容忍不同版本的键名）。"""

    blocks = find_block_list(payload)
    report = {
        "blocks": bool(blocks),
        "block_count": len(blocks),
    }
    for field, keys in BLOCK_FIELD_KEYS.items():
        report[field] = any(
            isinstance(block, Mapping) and any(key in block for key in keys)
            for block in blocks
        )
    return report


def wrapper_result_fields(cls: Any) -> list[str]:
    """读取 MCP wrapper 的返回类型字段（不调用任何远端能力）。"""

    if dataclasses.is_dataclass(cls):
        return [field.name for field in dataclasses.fields(cls)]
    annotations = getattr(cls, "__annotations__", None)
    if isinstance(annotations, Mapping):
        return list(annotations.keys())
    return []


def sdk_result_to_observed(result: Any) -> dict[str, Any]:
    """把底层 SDK 的 ``DocParsingResult`` 转成只含纯数据的观测结构。"""

    pages: list[dict[str, Any]] = []
    for index, page in enumerate(list(getattr(result, "pages", []) or [])):
        pages.append(
            {
                "position": index,
                "markdown_text": str(getattr(page, "markdown_text", "") or ""),
                "markdown_images": _object_to_mapping(getattr(page, "markdown_images", {}) or {}),
                "output_images": _object_to_mapping(getattr(page, "output_images", {}) or {}),
                "pruned_result": _object_to_mapping(getattr(page, "pruned_result", None)),
                "exports": _object_to_mapping(getattr(page, "exports", {}) or {}),
                "input_image_url": str(getattr(page, "input_image_url", "") or ""),
                "raw": _object_to_mapping(getattr(page, "raw", {}) or {}),
            }
        )
    return {
        "job_id_present": bool(getattr(result, "job_id", "")),
        "page_count": len(pages),
        "data_info": _object_to_mapping(getattr(result, "data_info", {}) or {}),
        "pages": pages,
    }


def build_capability_report(
    observed: Mapping[str, Any],
    *,
    wrapper_fields: Sequence[str] = (),
    raw_envelope_accessible: bool = False,
    envelope_keys: Sequence[str] = (),
    envelope: Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按验收口径产出 capability_report.json 的内容。"""

    pages = list(observed.get("pages") or [])
    first_page = pages[0] if pages else {}
    pruned = first_page.get("pruned_result")
    raw = first_page.get("raw") or {}
    markdown = str(first_page.get("markdown_text") or "")

    block_fields = block_field_report(pruned if pruned is not None else raw)
    has_blocks = bool(block_fields.get("blocks"))
    stats = _structure_stats(pages)
    image_pages = _image_pages(pages)
    envelope_info = dict(envelope or {})

    report: dict[str, Any] = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "source": dict(source or {}),
        # —— 验收清单里的核心字段 ——
        "markdown": bool(markdown.strip()),
        "images_mapping": bool(image_pages),
        "page_results": bool(pages),
        "blocks": has_blocks,
        "block_id": bool(block_fields.get("block_id")),
        "block_order": bool(block_fields.get("block_order")),
        "block_label": bool(block_fields.get("block_label")),
        "block_bbox": bool(block_fields.get("block_bbox")),
        "block_content": bool(block_fields.get("block_content")),
        "raw_provider_response_accessible": bool(raw_envelope_accessible),
        # —— 附加上下文 ——
        "page_count": int(observed.get("page_count") or 0),
        "page_index": bool(pages),
        "page_index_source": "page_position" if pages else "",
        "layout_parsing_results": bool(
            _has_any_key(raw, ("layoutParsingResults",))
            or any("layoutParsingResults" == key for key in envelope_keys)
        ),
        "job_envelope_keys": sorted(str(key) for key in envelope_keys),
        "job_envelope": envelope_info,
        "pruned_result": pruned is not None,
        "output_images": bool(first_page.get("output_images")),
        "exports": bool(first_page.get("exports")),
        "input_image_url": bool(first_page.get("input_image_url")),
        "block_count_page1": int(block_fields.get("block_count") or 0),
        "block_count_total": int(stats["block_count_total"]),
        "block_keys": stats["block_keys"],
        "block_order_non_null": int(stats["block_order_non_null"]),
        "block_order_null_ratio": stats["block_order_null_ratio"],
        "block_label_histogram": stats["label_histogram"],
        "block_group_id_present": stats["group_id_present"],
        "block_polygon_points_present": stats["polygon_points_present"],
        "layout_det_boxes_total": int(stats["layout_boxes_total"]),
        "images_pages": image_pages,
        "images_kind": stats["images_kind"],
        "wrapper_result_fields": list(wrapper_fields),
        "wrapper_exposes_blocks": any(
            field in {"blocks", "parsing_res_list", "pages_structured"} for field in wrapper_fields
        ),
        "gap_classification": _classify_gap(
            wrapper_fields=wrapper_fields, has_blocks=has_blocks
        ),
        "observed_page_keys": sorted({str(key) for key in first_page.keys()}),
        "observed_raw_keys": sorted({str(key) for key in raw.keys()})
        if isinstance(raw, Mapping)
        else [],
        "observed_pruned_keys": sorted({str(key) for key in pruned.keys()})
        if isinstance(pruned, Mapping)
        else [],
        "page_index_note": (
            "每行 JSONL 对应一个 layoutParsingResults 条目（dataInfo.numPages=1），"
            "页码只能由行序/页序推导，响应里没有显式 pageIndex 字段。"
        ),
    }
    return report


def envelope_stats(jsonl_data: Any) -> dict[str, Any]:
    """统计作业信封（每行 JSONL）：行数、每行条目数、dataInfo 与额外产物。"""

    if not isinstance(jsonl_data, list):
        return {}
    line_keys: set[str] = set()
    result_keys: set[str] = set()
    data_info_keys: set[str] = set()
    items_per_line: list[int] = []
    data_info_sample: dict[str, Any] = {}
    for line in jsonl_data:
        if not isinstance(line, Mapping):
            continue
        line_keys.update(str(key) for key in line.keys())
        result = line.get("result")
        if isinstance(result, Mapping):
            result_keys.update(str(key) for key in result.keys())
            items = result.get("layoutParsingResults")
            items_per_line.append(len(items) if isinstance(items, list) else 0)
            data_info = result.get("dataInfo")
            if isinstance(data_info, Mapping):
                data_info_keys.update(str(key) for key in data_info.keys())
                if not data_info_sample:
                    data_info_sample = {
                        "numPages": data_info.get("numPages"),
                        "page_size_keys": _first_page_size_keys(data_info.get("pages")),
                        "type": data_info.get("type"),
                    }
    return {
        "line_count": len(jsonl_data),
        "line_keys": sorted(line_keys),
        "result_keys": sorted(result_keys),
        "data_info_keys": sorted(data_info_keys),
        "data_info_sample": data_info_sample,
        "layout_parsing_results_per_line": items_per_line,
    }


def _first_page_size_keys(pages_value: Any) -> list[str]:
    if isinstance(pages_value, list) and pages_value and isinstance(pages_value[0], Mapping):
        return sorted(str(key) for key in pages_value[0].keys())
    return []


def _image_pages(pages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """逐页登记图片资源（只记数量与形态，不记 URL）。"""

    result: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        images = page.get("markdown_images") or {}
        if not isinstance(images, Mapping) or not images:
            continue
        kinds = sorted({_resource_kind(str(value)) for value in images.values()})
        result.append({"position": int(page.get("position", index)), "count": len(images), "kinds": kinds})
    return result


def _resource_kind(value: str) -> str:
    stripped = value.strip()
    if DATA_URL_PATTERN.match(stripped):
        return "data_url"
    if URL_PATTERN.match(stripped):
        return "url"
    return "opaque"


def _structure_stats(pages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """统计块级证据的完备度（键名、阅读顺序、标签分布、版面框）。"""

    block_keys: set[str] = set()
    label_histogram: dict[str, int] = {}
    order_total = 0
    order_non_null = 0
    layout_boxes = 0
    group_present = False
    polygon_present = False
    image_kinds: set[str] = set()

    for page in pages:
        for image in (page.get("markdown_images") or {}).values():
            image_kinds.add(_resource_kind(str(image)))
        payload = page.get("pruned_result")
        if not isinstance(payload, Mapping):
            continue
        boxes = ((payload.get("layout_det_res") or {}) or {}).get("boxes")
        if isinstance(boxes, list):
            layout_boxes += len(boxes)
        blocks = find_block_list(payload)
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            block_keys.update(str(key) for key in block.keys())
            label = str(block.get("block_label") or block.get("label") or "")
            if label:
                label_histogram[label] = label_histogram.get(label, 0) + 1
            if any(key in block for key in ("block_order", "blockOrder", "order")):
                order_total += 1
                value = block.get("block_order", block.get("blockOrder", block.get("order")))
                if value is not None:
                    order_non_null += 1
            group_present = group_present or any(
                key in block for key in ("group_id", "groupId")
            )
            polygon_present = polygon_present or any(
                key in block for key in ("block_polygon_points", "polygon_points")
            )

    return {
        "block_count_total": sum(len(find_block_list(page.get("pruned_result"))) for page in pages),
        "block_keys": sorted(block_keys),
        "block_order_non_null": order_non_null,
        "block_order_null_ratio": (
            round(1 - order_non_null / order_total, 4) if order_total else None
        ),
        "label_histogram": dict(sorted(label_histogram.items(), key=lambda item: (-item[1], item[0]))),
        "group_id_present": group_present,
        "polygon_points_present": polygon_present,
        "layout_boxes_total": layout_boxes,
        "images_kind": sorted(image_kinds),
    }


def _classify_gap(*, wrapper_fields: Sequence[str], has_blocks: bool) -> str:
    if any(field in {"blocks", "parsing_res_list", "pages_structured"} for field in wrapper_fields):
        return "C"  # wrapper 已经暴露块结构
    if has_blocks:
        return "B"  # SDK 有，wrapper 丢掉
    return "A"  # 服务端没给


def _redact_string(value: str, *, text_limit: int) -> str:
    """字符串脱敏：URL / data URL 整体替换，长文本截断。"""

    stripped = value.strip()
    if URL_PATTERN.match(stripped) or DATA_URL_PATTERN.match(stripped):
        digest = hashlib.sha256(stripped.encode("utf-8", "replace")).hexdigest()[:16]
        return f"<<redacted-resource:{digest}>>"
    if len(value) > text_limit:
        digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
        return (
            value[:text_limit]
            + f"...<<truncated sha256:{digest} chars:{len(value)}>>"
        )
    return value


def desensitize(value: Any, *, text_limit: int = 200, depth: int = 0, max_depth: int = 12) -> Any:
    """递归脱敏：敏感键置空、URL 替换、长文本截断、base64 负载丢弃。"""

    if depth > max_depth:
        return "<<depth-limit>>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SENSITIVE_KEY_PATTERN.search(key_text):
                result[key_text] = "<<redacted:sensitive-key>>"
                continue
            result[key_text] = desensitize(
                item, text_limit=text_limit, depth=depth + 1, max_depth=max_depth
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            desensitize(item, text_limit=text_limit, depth=depth + 1, max_depth=max_depth)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_string(value, text_limit=text_limit)
    return value


def looks_like_secret(value: str) -> bool:
    """长随机串启发式：足够长、无空白、大小写与数字混杂。"""

    if len(value) < 32 or any(char.isspace() for char in value):
        return False
    if not SECRET_VALUE_PATTERN.search(value):
        return False
    has_upper = any(char.isupper() for char in value)
    has_lower = any(char.islower() for char in value)
    has_digit = any(char.isdigit() for char in value)
    return has_upper and has_lower and has_digit


def scan_for_secrets(payload: Any) -> list[str]:
    """自检：在待提交产物里扫描疑似令牌/长随机串。"""

    hits: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key)
                if key_text in SCAN_EXEMPT_KEYS:
                    continue
                if SENSITIVE_KEY_PATTERN.search(key_text) and not str(item).startswith("<<redacted"):
                    hits.append(f"{path}.{key} (sensitive key)")
                walk(item, f"{path}.{key_text}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, str) and looks_like_secret(value):
            if not value.startswith("<<redacted") and "<<truncated sha256:" not in value:
                hits.append(f"{path} (long opaque value)")

    walk(payload, "$")
    return hits


def build_fixture(
    observed: Mapping[str, Any],
    *,
    max_pages: int = 2,
    text_limit: int = 200,
    box_limit: int = 8,
    source: Mapping[str, Any] | None = None,
    envelope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """产出可提交的脱敏 fixture（结构保真、内容最小）。"""

    pages = list(observed.get("pages") or [])[: max(1, max_pages)]
    pages = [trim_layout_boxes(page, limit=box_limit) for page in pages]
    fixture = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "source": dict(source or {}),
        "note": (
            "脱敏后的 PaddleOCR-VL structured result：键名/类型/版面字段保真，"
            "长文本截断、URL 与 base64 负载替换为占位符。"
        ),
        "page_count": int(observed.get("page_count") or 0),
        "envelope": desensitize(dict(envelope or {}), text_limit=text_limit),
        "pages": [
            desensitize(page, text_limit=text_limit, max_depth=14) for page in pages
        ],
    }
    return fixture


def trim_layout_boxes(page: Mapping[str, Any], *, limit: int = 8) -> dict[str, Any]:
    """裁掉版面框列表的长尾（保留结构与前 N 条，并记录原始条数）。"""

    clone = {key: value for key, value in page.items()}
    payload = clone.get("pruned_result")
    if not isinstance(payload, Mapping):
        return clone
    layout = payload.get("layout_det_res")
    if not isinstance(layout, Mapping):
        return clone
    boxes = layout.get("boxes")
    if not isinstance(boxes, list) or len(boxes) <= limit:
        return clone
    trimmed_layout = {key: value for key, value in layout.items()}
    trimmed_layout["boxes"] = boxes[:limit]
    trimmed_layout["__boxes_total__"] = len(boxes)
    trimmed_payload = {key: value for key, value in payload.items()}
    trimmed_payload["layout_det_res"] = trimmed_layout
    clone["pruned_result"] = trimmed_payload
    raw = clone.get("raw")
    if isinstance(raw, Mapping) and isinstance(raw.get("prunedResult"), Mapping):
        raw_clone = {key: value for key, value in raw.items()}
        raw_clone["prunedResult"] = trimmed_payload
        clone["raw"] = raw_clone
    return clone


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def resolve_token(explicit: str, token_env: str, fallback_env: str) -> tuple[str, str]:
    """按环境变量名解析令牌；返回值说明只写变量名，绝不写值。"""

    import os

    if explicit:
        return explicit, "参数 --token"
    for name in (token_env, fallback_env):
        if not name:
            continue
        value = os.environ.get(name)
        if value:
            return value, f"进程环境变量 {name}"
        value = _read_user_env(name)
        if value:
            return value, f"用户级环境变量 {name}"
    raise SystemExit(
        f"未找到令牌：请设置环境变量 {token_env}（或 {fallback_env}），不要写进配置文件。"
    )


def _read_user_env(name: str) -> str:
    import os

    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value or "")
