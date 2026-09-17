"""唯一 producer（``ocr_producer.py``）的离线回归测试。

全部使用真实 capability probe 落下来的脱敏 fixture 与假客户端：
不访问远端、不消耗配额，用来锁住"证据保真 + 不可变 + 图片校验 + 压力重试"这几条不变量。
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


# ---------------------------------------------------------------- 证据模型


def test_document_result_keeps_block_evidence(fixture: dict) -> None:
    document = _document(fixture)
    assert document.page_count >= 1
    first = document.pages[0]
    assert first.blocks, "块证据不能为空"
    labels = {block.label for block in first.blocks}
    assert "text" in labels
    assert first.width and first.height
    assert first.layout_boxes_total > 0
    assert document.markdown.strip(), "markdown 视图必须保留"


def test_reading_order_falls_back_to_list_order(fixture: dict) -> None:
    """实测 block_order 会为 null：阅读顺序必须以列表顺序兜底。"""

    document = _document(fixture)
    blocks = document.pages[0].blocks
    assert [block.reading_order for block in blocks] == list(range(len(blocks)))
    assert any(block.block_order is None for block in blocks), (
        "fixture 里应当包含 block_order 为 null 的块（header/doc_title）"
    )


def test_evidence_schema_hash_and_redaction(fixture: dict, tmp_path: pathlib.Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    document = _document(fixture)
    evidence = producer.build_evidence(
        document,
        source_path=source,
        model="PaddleOCR-VL-1.6",
        provider="aistudio",
        token_source="进程环境变量 TEST",
    )

    assert evidence["schema_version"] == producer.EVIDENCE_SCHEMA_VERSION
    assert evidence["page_count"] == document.page_count
    assert evidence["block_count"] == sum(len(page.blocks) for page in document.pages)
    assert len(evidence["content_hash"]) == 64
    assert evidence["source"]["sha256"]
    assert "raw_pruned_result" not in evidence["pages"][0], "默认不内嵌原始结构"

    text = json.dumps(evidence, ensure_ascii=False)
    assert "http://" not in text and "https://" not in text, "远端链接必须脱敏"

    # 同一份内容 → 同一哈希；改动块内容 → 哈希变化
    again = producer.build_evidence(
        document,
        source_path=source,
        model="PaddleOCR-VL-1.6",
        provider="aistudio",
        token_source="进程环境变量 TEST",
    )
    assert again["content_hash"] == evidence["content_hash"]
    mutated = producer.DocumentResult(
        markdown=document.markdown,
        pages=(
            producer.PageEvidence(
                page_index=document.pages[0].page_index,
                width=document.pages[0].width,
                height=document.pages[0].height,
                markdown_text=document.pages[0].markdown_text,
                blocks=(
                    producer.BlockEvidence(
                        block_id=0,
                        block_order=None,
                        reading_order=0,
                        label="text",
                        content="被改过的正文",
                    ),
                ),
            ),
        ),
        envelope_keys=document.envelope_keys,
    )
    mutated_evidence = producer.build_evidence(
        mutated,
        source_path=source,
        model="PaddleOCR-VL-1.6",
        provider="aistudio",
        token_source="进程环境变量 TEST",
    )
    assert mutated_evidence["content_hash"] != evidence["content_hash"]


def test_evidence_is_immutable(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "x.evidence.json"
    producer.write_evidence(path, {"a": 1})
    with pytest.raises(producer.EvidenceExistsError):
        producer.write_evidence(path, {"a": 2})
    producer.write_evidence(path, {"a": 2}, overwrite=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}


def test_write_raw_pages_modes(fixture: dict, tmp_path: pathlib.Path) -> None:
    document = _document(fixture)
    assert producer.write_raw_pages(document, tmp_path / "none", mode="none") == []
    page_files = producer.write_raw_pages(document, tmp_path / "page", mode="page")
    assert len(page_files) == document.page_count
    payload = json.loads(page_files[0].read_text(encoding="utf-8"))
    assert "parsing_res_list" in payload, "page 模式写的是原始 prunedResult"
    full_files = producer.write_raw_pages(document, tmp_path / "full", mode="full")
    full_payload = json.loads(full_files[0].read_text(encoding="utf-8"))
    assert {"pruned_result", "markdown", "output_images"} <= set(full_payload)


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
    assert evidence["block_count"] > 0
    assert (out / "mock_raw" / "page-0001.json").is_file()
    assert row.blocks == evidence["block_count"]
    assert _FakeClient.calls[0]["pdf"].endswith("mock.pdf")

    # 第二次运行：证据已存在 → 跳过，绝不覆盖原始证据
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
                page_index=0,
                width=10,
                height=20,
                markdown_text='<img src="https://cdn.invalid/a.jpg" />',
                images={"imgs/a.jpg": "https://cdn.invalid/a.jpg"},
                blocks=(),
            ),
        ),
    )
    _FakeClient.document = document

    def downloader(url: str) -> bytes:
        raise OSError("boom")

    row = asyncio.run(
        producer.convert_pdf(
            pdf,
            output_dir=out,
            client_factory=_FakeClient,
            token="dummy",
            keep_images=True,
            download=False,
            downloader=downloader,
        )
    )
    # download=False → 图片不落盘，但必须如实登记"仍未本地化"
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
    assert args.evidence_raw == "page"
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
