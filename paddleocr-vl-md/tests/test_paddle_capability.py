"""PaddleOCR-VL 结构化返回能力的离线回归测试。

这些测试**不访问任何远端服务**：全部断言跑在脱敏 fixture 和纯函数上，
用来锁住 capability probe 的结论，防止后续重构把已确认可用的结构证据丢掉。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR / "tools"))

import paddle_capability as pc  # noqa: E402


FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "paddle_structured_result.fixture.json"
)
MAX_FIXTURE_BYTES = 200_000


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def observed(fixture: dict) -> dict:
    """把 fixture 复原成 probe 内部的观测结构（等同于真实运行时的形态）。"""

    return {
        "job_id_present": True,
        "page_count": fixture["page_count"],
        "data_info": {},
        "pages": fixture["pages"],
    }


@pytest.fixture(scope="module")
def report(observed: dict, fixture: dict) -> dict:
    return pc.build_capability_report(
        observed,
        wrapper_fields=["markdown", "pages", "images_mapping"],
        raw_envelope_accessible=True,
        envelope_keys=fixture["envelope"]["result_keys"],
        envelope=fixture["envelope"],
        source=fixture["source"],
    )


def test_fixture_schema_and_size() -> None:
    assert FIXTURE_PATH.stat().st_size < MAX_FIXTURE_BYTES
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == pc.FIXTURE_SCHEMA_VERSION
    assert fixture["pages"], "fixture 必须至少保留一页"
    assert fixture["pages"][0]["raw"]["prunedResult"]["parsing_res_list"], (
        "fixture 必须保留真实块列表"
    )


def test_fixture_is_desensitized(fixture: dict) -> None:
    """fixture 里不得出现远端 URL、data URL、base64 负载或疑似令牌。"""

    assert pc.scan_for_secrets(fixture) == []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            assert not pc.URL_PATTERN.match(value.strip()), value[:80]
            assert not pc.DATA_URL_PATTERN.match(value.strip()), value[:80]
            assert len(value) <= 400, "长文本必须被截断"

    walk(fixture)


def test_capability_report_flags(report: dict) -> None:
    """验收清单里的每一项都必须为真，这是下一阶段设计 block 证据的前提。"""

    for flag in (
        "markdown",
        "images_mapping",
        "page_results",
        "blocks",
        "block_id",
        "block_order",
        "block_label",
        "block_bbox",
        "block_content",
        "raw_provider_response_accessible",
        "layout_parsing_results",
    ):
        assert report[flag] is True, flag
    assert report["page_count"] >= 2, "多页必须被验证"
    assert report["block_count_total"] > 0


def test_block_keys_and_labels(report: dict) -> None:
    """块级字段名必须与实测一致；标签里必须同时出现正文、公式与版面标签。"""

    assert {
        "block_id",
        "block_order",
        "block_label",
        "block_bbox",
        "block_content",
    } <= set(report["block_keys"])
    labels = report["block_label_histogram"]
    assert labels.get("text", 0) > 0
    assert any("formula" in key for key in labels), "公式标签必须出现"
    assert any(key in labels for key in ("header", "doc_title", "paragraph_title")), (
        "版面/标题标签必须出现"
    )


def test_block_order_is_partially_null(report: dict) -> None:
    """实测 block_order 存在但有 null：阅读顺序必须允许回退到列表顺序。"""

    assert report["block_order"] is True
    assert 0 <= report["block_order_non_null"] <= report["block_count_total"]
    assert report["block_order_null_ratio"] is not None


def test_gap_classification_is_sdk_has_wrapper_drops() -> None:
    """MCP wrapper 只有 markdown/pages/images_mapping，块结构在 wrapper 层被丢掉。"""

    assert pc._classify_gap(
        wrapper_fields=["markdown", "pages", "images_mapping"], has_blocks=True
    ) == "B"
    assert pc._classify_gap(
        wrapper_fields=["markdown", "pages", "images_mapping"], has_blocks=False
    ) == "A"
    assert pc._classify_gap(
        wrapper_fields=["markdown", "pages", "blocks"], has_blocks=True
    ) == "C"


def test_find_block_list_tolerates_aliases() -> None:
    assert pc.find_block_list({"parsing_res_list": [{"block_id": 0}]}) == [{"block_id": 0}]
    assert pc.find_block_list({"outer": {"blocks": [1, 2]}}) == [1, 2]
    assert pc.find_block_list({"nothing": 1}) == []


def test_block_field_report_detects_partial_keys() -> None:
    payload = {
        "parsing_res_list": [
            {"block_label": "text", "block_content": "x", "block_bbox": [0, 0, 1, 1]},
            {"block_label": "image", "block_content": "y"},
        ]
    }
    fields = pc.block_field_report(payload)
    assert fields["blocks"] is True
    assert fields["block_label"] is True
    assert fields["block_bbox"] is True
    assert fields["block_id"] is False
    assert fields["block_order"] is False


def test_structure_map_reports_types_not_content() -> None:
    mapped = pc.structure_map({"a": "secret-token", "b": [{"c": 1}]}, max_depth=3)
    assert mapped["__type__"] == "object"
    assert mapped["a"] == {"__type__": "str", "__chars__": len("secret-token")}
    assert mapped["b"]["__type__"] == "array"
    assert mapped["b"]["__item__"]["c"]["__type__"] == "int"
    assert "secret-token" not in json.dumps(mapped)


def test_trim_layout_boxes_keeps_total() -> None:
    page = {
        "pruned_result": {"layout_det_res": {"boxes": [{"i": index} for index in range(20)]}},
        "raw": {"prunedResult": {"layout_det_res": {"boxes": [{"i": index} for index in range(20)]}}},
    }
    trimmed = pc.trim_layout_boxes(page, limit=5)
    layout = trimmed["pruned_result"]["layout_det_res"]
    assert len(layout["boxes"]) == 5
    assert layout["__boxes_total__"] == 20
    assert trimmed["raw"]["prunedResult"]["layout_det_res"]["__boxes_total__"] == 20


def test_envelope_stats() -> None:
    envelope = [
        {
            "errorCode": 0,
            "result": {
                "dataInfo": {"numPages": 1, "pages": [{"width": 10, "height": 20}], "type": "pdf"},
                "layoutParsingResults": [{"markdown": {}}],
                "preprocessedImages": ["x"],
            },
        }
    ]
    stats = pc.envelope_stats(envelope)
    assert stats["line_count"] == 1
    assert stats["layout_parsing_results_per_line"] == [1]
    assert "preprocessedImages" in stats["result_keys"]
    assert stats["data_info_sample"]["numPages"] == 1


def test_secret_scan_and_redaction() -> None:
    token_like = "AbCdEf0123456789AbCdEf0123456789"
    assert pc.looks_like_secret(token_like)
    assert not pc.looks_like_secret("md-prework/paddle-structured-result-fixture/v1")
    payload = {
        "api_token": token_like,
        "image": "https://cdn.example.com/a.jpg?sign=" + token_like,
        "text": "正文" * 300,
    }
    hits = pc.scan_for_secrets(payload)
    assert any("api_token" in hit for hit in hits)
    redacted = pc.desensitize(payload, text_limit=50)
    assert redacted["api_token"] == "<<redacted:sensitive-key>>"
    assert redacted["image"].startswith("<<redacted-resource:")
    assert "truncated sha256:" in redacted["text"]
    assert pc.scan_for_secrets(redacted) == []
