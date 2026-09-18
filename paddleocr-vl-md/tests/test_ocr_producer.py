"""唯一 producer（``ocr_producer.py``）的离线回归测试。

全部使用真实 capability probe 落下来的脱敏 fixture 与假客户端：
不访问远端、不消耗配额。

其中两个测试是**调查任务的锁定测试**（capability probe 收口用）：

* ``test_wrapper_loss_regression``：provider raw 有块结构，MCP wrapper 的公共返回类型没有；
* ``test_schema_extraction_regression``：fixture → 证据模型必须稳定产出固定的块数量与字段。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import types

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import ocr_producer as producer  # noqa: E402


FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "paddle_structured_result.fixture.json"
)

#: fixture 实测：page 0 有 27 块、page 1 有 13 块（共 40 块）。
#: 一旦 Paddle provider 结构变化，这个断言会第一时间失败。
EXPECTED_BLOCKS_BY_PAGE = {0: 27, 1: 13}


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _fake_sdk_result(fixture: dict) -> object:
    pages = []
    for page in fixture["pages"]:
        raw = page.get("raw") or {}
        pages.append(
            types.SimpleNamespace(
                markdown_text=raw.get("markdown", {}).get("text", ""),
                markdown_images=dict(raw.get("markdown", {}).get("images") or {}),
                output_images=dict(raw.get("outputImages") or {}),
                input_image_url=raw.get("inputImage", ""),
                pruned_result=raw.get("prunedResult") or {},
                raw=raw,
            )
        )
    return types.SimpleNamespace(job_id="job-fixture", pages=pages, data_info={})


def _document(fixture: dict) -> producer.DocumentResult:
    return producer.document_result_from_sdk(_fake_sdk_result(fixture))


class _FakeClient:
    """假 client：只返回预先准备好的 DocumentResult。"""

    document: producer.DocumentResult | None = None
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def ocr_document(self, pdf: pathlib.Path, **kwargs) -> producer.DocumentResult:
        self.calls.append({"pdf": str(pdf), **kwargs})
        assert self.document is not None
        return self.document


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ---------------------------------------------------------------- 锁定测试


def test_wrapper_loss_regression(fixture: dict) -> None:
    """锁定事实：provider raw page 有块结构，而 MCP wrapper 的公共返回类型没有。

    这个测试不是为了测 Paddle，而是为了把"为什么必须保存 raw page result"写进回归：
    将来若有人把证据来源换回 wrapper，这里会立刻失败。
    """

    from paddleocr_mcp.inference.types import DocParsingResult as McpDocParsingResult

    wrapper_fields = set(getattr(McpDocParsingResult, "__dataclass_fields__", {}))
    assert wrapper_fields == {"markdown", "pages", "images_mapping"}, wrapper_fields
    assert "parsing_res_list" not in wrapper_fields

    raw_pruned = fixture["pages"][0]["raw"]["prunedResult"]
    assert raw_pruned.get("parsing_res_list"), "provider raw page 必须带块列表"
    assert "layout_det_res" in raw_pruned


def test_schema_extraction_regression(fixture: dict) -> None:
    """fixture → 证据模型：块数量与字段必须稳定（provider 升级的第一道警报）。"""

    document = _document(fixture)
    counts = {page.page_seq: len(page.blocks) for page in document.pages}
    assert counts == EXPECTED_BLOCKS_BY_PAGE
    assert document.pages[0].layout_boxes_count > 0

    first = document.pages[0].blocks[0].to_dict(page_seq=0)
    assert {
        "block_ref",
        "provider_block_id",
        "provider_block_order",
        "sequence_index",
        "label",
        "content",
        "bbox",
        "polygon",
        "group_id",
        "content_sha256",
    } <= set(first)


# ---------------------------------------------------------------- 证据模型


def test_document_result_keeps_block_evidence(fixture: dict) -> None:
    document = _document(fixture)
    first = document.pages[0]
    labels = {block.label for block in first.blocks}
    assert "text" in labels
    assert first.width and first.height
    assert document.markdown.strip(), "markdown 视图必须保留"
    assert document.model_settings, "producer settings 必须来自 provider 的 model_settings"


def test_sequence_index_is_array_position(fixture: dict) -> None:
    """数组位置是事实，block_order 是 provider 字段（可为 null），不写 reading_order。"""

    document = _document(fixture)
    blocks = document.pages[0].blocks
    assert [block.sequence_index for block in blocks] == list(range(len(blocks)))
    assert any(block.provider_block_order is None for block in blocks)

    payload = document.pages[0].to_dict()
    assert payload["blocks"][0]["block_ref"] == "p0000:b0000"
    assert "reading_order" not in payload["blocks"][0], "解释性字段不进证据层"


def test_order_consistency_diagnostics(fixture: dict) -> None:
    document = _document(fixture)
    payload = document.pages[0].to_dict()
    stats = payload["order_consistency"]
    assert stats["ordered_blocks"] == len(document.pages[0].blocks)
    assert stats["block_order_present"] < stats["ordered_blocks"], "fixture 里存在 null order"
    assert isinstance(stats["block_order_monotonic"], bool)
    assert stats["block_order_conflicts"] >= 0

    # 人为构造回退的 block_order → 必须被记为不单调
    blocks = (
        producer.BlockEvidence(1, 5, 0, "text", "a"),
        producer.BlockEvidence(2, 3, 1, "text", "b"),
    )
    bad = producer.order_consistency(blocks)
    assert bad["block_order_monotonic"] is False
    assert bad["block_order_conflicts"] == 1


def test_evidence_schema_identity_and_redaction(fixture: dict, tmp_path: pathlib.Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    document = _document(fixture)
    evidence = producer.build_evidence(
        document,
        source_path=source,
        model="PaddleOCR-VL-1.6",
        provider="aistudio",
        token_source="进程环境变量 TEST",
        page_ranges="1-4",
        raw_mode="page",
        mcp_version="0.8.5",
    )

    assert evidence["schema_version"] == producer.EVIDENCE_SCHEMA_VERSION
    assert evidence["document_id"].startswith("paper-")
    assert evidence["identity"]["source_sha256"] == evidence["source"]["sha256"]
    assert len(evidence["identity"]["producer_config_hash"]) == 64
    assert evidence["producer"]["paddleocr_mcp_version"] == "0.8.5"
    assert evidence["producer"]["settings"] == dict(document.model_settings)
    assert evidence["page_count"] == document.page_count
    assert evidence["block_count"] == sum(len(page.blocks) for page in document.pages)
    assert len(evidence["content_hash"]) == 64

    page = evidence["pages"][0]
    assert page["page_seq"] == 0
    assert page["page_index_source"] == "response_sequence"
    assert page["coordinate_space"]["kind"] in {"provider_page", "preprocessed_page"}
    assert page["layout_detection"]["boxes_count"] == document.pages[0].layout_boxes_count
    assert page["raw_ref"].endswith("paper_raw/page-0001.json")

    text = json.dumps(evidence, ensure_ascii=False)
    assert "raw_pruned_result" not in text, "Stable Core 不内嵌 provider raw"
    assert "http://" not in text and "https://" not in text, "远端链接必须脱敏"

    # 内容哈希稳定；producer 配置变化 → 配置指纹变化
    same = producer.build_evidence(
        document,
        source_path=source,
        model="PaddleOCR-VL-1.6",
        provider="aistudio",
        token_source="进程环境变量 TEST",
        page_ranges="1-4",
        raw_mode="page",
        mcp_version="0.8.5",
    )
    assert same["content_hash"] == evidence["content_hash"]
    other_model = producer.build_evidence(
        document,
        source_path=source,
        model="PaddleOCR-VL-1.7",
        provider="aistudio",
        token_source="进程环境变量 TEST",
        page_ranges="1-4",
        raw_mode="page",
        mcp_version="0.8.5",
    )
    assert other_model["identity"]["producer_config_hash"] != evidence["identity"]["producer_config_hash"]


def test_evidence_is_immutable(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "x.evidence.json"
    producer.write_evidence(path, {"a": 1})
    with pytest.raises(producer.EvidenceExistsError):
        producer.write_evidence(path, {"a": 2})
    producer.write_evidence(path, {"a": 2}, overwrite=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}


def test_raw_artifact_modes(fixture: dict, tmp_path: pathlib.Path) -> None:
    document = _document(fixture)
    assert producer.write_raw_pages(document, tmp_path / "none", mode="none") == []

    page_files = producer.write_raw_pages(document, tmp_path / "page", mode="page")
    assert len(page_files) == document.page_count
    payload = json.loads(page_files[0].read_text(encoding="utf-8"))
    assert "parsing_res_list" in payload, "page 模式写的是完整 prunedResult"


def test_full_mode_writes_scrubbed_envelope(fixture: dict, tmp_path: pathlib.Path) -> None:
    """full 模式额外写作业信封，但必须剔除 token / 凭证 / 长随机串。"""

    document = _document(fixture)
    envelope = [
        {
            "errorCode": 0,
            "result": {"layoutParsingResults": [], "dataInfo": {"numPages": 1}},
            "accessToken": "AbCdEf0123456789AbCdEf0123456789",
        }
    ]
    document_with_envelope = producer.DocumentResult(
        markdown=document.markdown,
        pages=document.pages,
        envelope_keys=document.envelope_keys,
        model_settings=document.model_settings,
        raw_pages=document.raw_pages,
        raw_envelope=envelope,
    )
    written = producer.write_raw_pages(document_with_envelope, tmp_path / "full", mode="full")
    names = sorted(path.name for path in written)
    assert "envelope.json" in names
    envelope_payload = json.loads((tmp_path / "full" / "envelope.json").read_text(encoding="utf-8"))
    assert envelope_payload[0]["accessToken"] == "<<redacted>>"
    assert "AbCdEf0123456789" not in json.dumps(envelope_payload)


# ---------------------------------------------------------------- 图片


def test_materialize_images_validates_and_records(tmp_path: pathlib.Path) -> None:
    images = {
        "imgs/ok.jpg": "https://cdn.invalid/ok.jpg",
        "imgs/bad.jpg": "https://cdn.invalid/bad.jpg",
        "imgs/inline.png": "data:image/png;base64," + "A" * 8,
    }

    def downloader(url: str) -> bytes:
        if url.endswith("bad.jpg"):
            return b"<html>not an image</html>"
        return _png_bytes()

    mapping, records, failures = producer.materialize_images(
        images, tmp_path / "paper_media", downloader=downloader
    )

    states = {record["src"]: record["state"] for record in records}
    assert states["imgs/ok.jpg"] == "saved"
    assert states["imgs/bad.jpg"] == "missing"
    assert states["imgs/inline.png"] == "missing"  # base64 负载不是合法 PNG
    assert any("bad.jpg" in failure for failure in failures)
    assert mapping["imgs/ok.jpg"].endswith("imgs/ok.png"), "按文件头纠正扩展名"
    assert (tmp_path / "paper_media" / "imgs" / "ok.png").is_file()


def test_rewrite_and_broken_image_sources() -> None:
    markdown = '<img src="imgs/a.jpg" />\n<img src="https://cdn.invalid/b.jpg" />'
    mapping = {"imgs/a.jpg": "paper_media/imgs/a.png"}
    rewritten = producer.rewrite_image_sources(markdown, mapping)
    assert 'src="paper_media/imgs/a.png"' in rewritten
    assert 'src="https://cdn.invalid/b.jpg"' in rewritten, "拿不到本地文件的引用保持原样"
    assert producer.find_broken_image_sources(rewritten, mapping) == ["https://cdn.invalid/b.jpg"]


def test_materialize_images_without_keep_records_remote(tmp_path: pathlib.Path) -> None:
    mapping, records, failures = producer.materialize_images(
        {"imgs/a.jpg": "https://cdn.invalid/a.jpg"}, tmp_path / "media", keep=False
    )
    assert mapping == {}
    assert failures == []
    assert records[0]["state"] == "remote"


# ---------------------------------------------------------------- 重试与压力


def test_retry_only_on_temporary_backpressure() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Bad request: 任务提交队列已满，请稍后重试")
        return "ok"

    async def run() -> str:
        return await producer.predict_with_retry(
            flaky, attempts=4, delay=0, sleep=lambda _: asyncio.sleep(0)
        )

    assert asyncio.run(run()) == "ok"
    assert calls["n"] == 3


def test_quota_exhausted_is_not_retried() -> None:
    calls = {"n": 0}

    async def exhausted() -> str:
        calls["n"] += 1
        raise RuntimeError("Bad request: 今日提交任务已达上限")

    async def run() -> str:
        return await producer.predict_with_retry(
            exhausted, attempts=4, delay=0, sleep=lambda _: asyncio.sleep(0)
        )

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert calls["n"] == 1, "额度耗尽重试无意义"


# ---------------------------------------------------------------- 端到端（假客户端）


def test_convert_pdf_writes_evidence_markdown_and_raw(fixture: dict, tmp_path: pathlib.Path) -> None:
    pdf = tmp_path / "mock.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = tmp_path / "out"
    _FakeClient.document = _document(fixture)
    _FakeClient.calls = []

    row = asyncio.run(
        producer.convert_pdf(
            pdf,
            output_dir=out,
            client_factory=_FakeClient,
            token="dummy",
            token_source="测试",
            keep_images=False,
            download=False,
        )
    )

    assert row.status == producer.STATUS_OK
    assert (out / "mock.md").is_file()
    evidence = json.loads((out / "mock.evidence.json").read_text(encoding="utf-8"))
    assert evidence["schema_version"] == producer.EVIDENCE_SCHEMA_VERSION
    assert evidence["block_count"] == sum(EXPECTED_BLOCKS_BY_PAGE.values())
    assert (out / "mock_raw" / "page-0000.json").is_file()
    assert row.blocks == evidence["block_count"]

    second = asyncio.run(
        producer.convert_pdf(
            pdf,
            output_dir=out,
            client_factory=_FakeClient,
            token="dummy",
            token_source="测试",
        )
    )
    assert second.status == producer.STATUS_SKIPPED
    assert second.reason == "evidence_exists"


def test_convert_pdf_records_missing_images(fixture: dict, tmp_path: pathlib.Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = tmp_path / "out"
    document = producer.DocumentResult(
        markdown='<img src="https://cdn.invalid/a.jpg" />',
        pages=(
            producer.PageEvidence(
                page_seq=0,
                width=10,
                height=20,
                markdown_text='<img src="https://cdn.invalid/a.jpg" />',
                images={"imgs/a.jpg": "https://cdn.invalid/a.jpg"},
                blocks=(),
            ),
        ),
        raw_pages=({"parsing_res_list": []},),
    )
    _FakeClient.document = document

    row = asyncio.run(
        producer.convert_pdf(
            pdf,
            output_dir=out,
            client_factory=_FakeClient,
            token="dummy",
            keep_images=True,
            download=False,
        )
    )
    assert row.status == producer.STATUS_OK
    assert row.images_total == 1
    assert row.images_saved == 0


# ---------------------------------------------------------------- 汇总与输入


def test_status_and_summary_files(tmp_path: pathlib.Path) -> None:
    rows = [
        producer.ConvertOutcome(pdf="a.pdf", status=producer.STATUS_OK, pages=2, blocks=9),
        producer.ConvertOutcome(pdf="b.pdf", status=producer.STATUS_FAILED, error="boom"),
    ]
    status_path = producer.write_status_log(tmp_path, rows)
    lines = status_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == producer.STATUS_OK

    summary_path = producer.write_summary(
        tmp_path,
        rows,
        model="PaddleOCR-VL-1.6",
        provider="aistudio",
        token_source="进程环境变量 TEST",
        command="prework_ocr.py batch",
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["ok"] == 1 and summary["failed"] == 1
    assert summary["rows"][1]["error"] == "boom"


def test_collect_pdfs_dedupes_and_reports_problems(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    pdfs, problems = producer.collect_pdfs([str(tmp_path), str(tmp_path / "a.pdf")])
    assert [pdf.name for pdf in pdfs] == ["a.pdf", "b.pdf"]
    assert problems == [], "目录扫描只挑 .pdf，不该为其它文件报问题"
    _, wrong_type = producer.collect_pdfs([str(tmp_path / "note.txt")])
    assert wrong_type and "不是 PDF" in wrong_type[0]
    _, missing = producer.collect_pdfs([str(tmp_path / "nope.pdf")])
    assert missing and "路径不存在" in missing[0]


def test_prework_cli_defaults() -> None:
    import prework_ocr

    args = prework_ocr.parse_args(
        ["convert", "--input", "paper.pdf", "--output-dir", "out"]
    )
    assert args.evidence is True, "证据默认落盘"
    assert args.keep_images is True
    assert args.evidence_raw == "page", "生产默认保存 provider raw page"
    batch = prework_ocr.parse_args(["batch", "--pdf-dir", "pdfs", "--output-dir", "out"])
    assert batch.jobs == 1


def test_run_concurrent_reuses_scheduler_and_isolates_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """并发批处理复用调度器的冷却策略，且单个失败不影响其余任务。"""

    import prework_ocr

    pdfs = []
    for index in range(3):
        pdf = tmp_path / f"p{index}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        pdfs.append(pdf)

    async def fake_convert(pdf: pathlib.Path, **kwargs):
        if pdf.name == "p1.pdf":
            return producer.ConvertOutcome(
                pdf=pdf.name,
                status=producer.STATUS_FAILED,
                error="Bad request: 任务提交队列已满，请稍后重试",
            )
        return producer.ConvertOutcome(
            pdf=pdf.name, status=producer.STATUS_OK, pages=1, blocks=2
        )

    monkeypatch.setattr(producer, "convert_pdf", fake_convert)
    args = prework_ocr.parse_args(
        [
            "batch",
            "--pdf-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--jobs",
            "2",
            "--launch-interval",
            "0",
        ]
    )
    rows = asyncio.run(
        prework_ocr.run_concurrent(
            pdfs, args, token="dummy", token_source="测试", log=lambda *_: None
        )
    )
    assert [row.pdf for row in rows] == ["p0.pdf", "p1.pdf", "p2.pdf"]
    assert sum(1 for row in rows if row.status == producer.STATUS_OK) == 2
    assert sum(1 for row in rows if row.status == producer.STATUS_FAILED) == 1
