"""图片导出与缺图检测：默认落盘、可显式关闭、跨入口参数一致、缺图判定完备。

由 temp 分支的原始用例改编到当前唯一 producer（``ocr_producer``）与两个 CLI 契约上；
不联网：图片字节全部本地构造，远端下载用假函数替掉。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import ocr_producer as producer  # noqa: E402
import pdf2md  # noqa: E402
import prework_ocr  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff" + b"\x00" * 24


def _downloader(payload: bytes):
    def _get(url: str) -> bytes:
        return payload

    return _get


# ---------------------------------------------------------------- media 落盘


def test_materialize_keeps_structure_and_rewrites_src(tmp_path: pathlib.Path) -> None:
    images = {"imgs/sub/a.jpg": "https://cdn.invalid/a.jpg"}
    mapping, records, failures = producer.materialize_images(
        images, tmp_path / "media", downloader=_downloader(JPEG)
    )
    assert failures == []
    assert mapping["imgs/sub/a.jpg"] == "media/imgs/sub/a.jpg"
    assert (tmp_path / "media" / "imgs" / "sub" / "a.jpg").is_file()
    markdown = '<img src="imgs/sub/a.jpg" />'
    assert producer.rewrite_image_sources(markdown, mapping) == '<img src="media/imgs/sub/a.jpg" />'


def test_materialize_fixes_suffix_from_file_header(tmp_path: pathlib.Path) -> None:
    mapping, records, _ = producer.materialize_images(
        {"imgs/a.png": "https://cdn.invalid/a.png"}, tmp_path / "media", downloader=_downloader(JPEG)
    )
    assert mapping["imgs/a.png"].endswith(".jpg"), "按文件头纠正扩展名"
    assert records[0]["format"] == "JPEG"


def test_materialize_reports_unusable_payload_without_writing(tmp_path: pathlib.Path) -> None:
    mapping, records, failures = producer.materialize_images(
        {"imgs/bad.jpg": "https://cdn.invalid/bad.jpg"},
        tmp_path / "media",
        downloader=_downloader(b"<html>not an image</html>"),
    )
    assert mapping == {} and failures
    assert records[0]["state"] == "missing"
    assert not (tmp_path / "media" / "imgs").exists()


def test_data_url_payload_is_accepted(tmp_path: pathlib.Path) -> None:
    import base64

    payload = "data:image/png;base64," + base64.b64encode(PNG).decode()
    mapping, _, failures = producer.materialize_images({"imgs/a.png": payload}, tmp_path / "media")
    assert failures == []
    assert (tmp_path / "media" / "imgs" / "a.png").read_bytes() == PNG


# ---------------------------------------------------------------- 缺图判定


def test_missing_detection_covers_remote_relative_and_data(tmp_path: pathlib.Path) -> None:
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "ok.png").write_bytes(PNG)
    markdown = "\n".join(
        [
            '<img src="media/ok.png" />',
            '<img src="media/gone.png" />',
            '<img src="https://cdn.invalid/x.jpg" />',
            '<img src="data:image/png;base64,AAAA" />',
        ]
    )
    missing = producer.find_missing_image_sources(markdown, tmp_path)
    assert missing == ["media/gone.png", "https://cdn.invalid/x.jpg"], "data URL 不算缺失"


def test_missing_detection_uses_markdown_directory_as_base(tmp_path: pathlib.Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "media").mkdir()
    (docs / "media" / "ok.png").write_bytes(PNG)
    markdown = '<img src="media/ok.png" />'
    assert producer.find_missing_image_sources(markdown, docs) == []
    assert producer.find_missing_image_sources(markdown, tmp_path) == ["media/ok.png"]


def test_missing_detection_dedups_and_keeps_order(tmp_path: pathlib.Path) -> None:
    markdown = "\n".join(
        [
            '<img src="b.png" />',
            '<img src="a.png" />',
            '<img src="b.png" />',
        ]
    )
    assert producer.find_missing_image_sources(markdown, tmp_path) == ["b.png", "a.png"]


def test_missing_detection_ignores_non_img_syntax(tmp_path: pathlib.Path) -> None:
    markdown = "![alt](a.png)\n普通文本提到 a.png"
    assert producer.find_missing_image_sources(markdown, tmp_path) == []


# ---------------------------------------------------------------- 跳过时的提示


class _SkipClient:
    document = None

    def __init__(self, **_kwargs) -> None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def ocr_document(self, pdf, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("已有证据时不应重新 OCR")


def _skip_convert(pdf: pathlib.Path, out: pathlib.Path, markdown: str):
    import asyncio

    (out / "paper_prework").mkdir(parents=True, exist_ok=True)
    (out / "paper_prework" / "evidence.json").write_text("{}", encoding="utf-8")
    (out / "paper.md").write_text(markdown, encoding="utf-8")
    return asyncio.run(
        producer.convert_pdf(
            pdf, output_dir=out, client_factory=_SkipClient, token="dummy"
        )
    )


def test_skip_warning_reports_unlocalized_images(tmp_path: pathlib.Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    out = tmp_path / "out"
    row = _skip_convert(pdf, out, '<img src="https://cdn.invalid/x.jpg" />')
    assert row.status == producer.STATUS_SKIPPED
    assert row.missing_images == ["https://cdn.invalid/x.jpg"], "跳过时也要提示未本地化图片"


def test_skip_warning_is_quiet_when_images_are_local(tmp_path: pathlib.Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    out = tmp_path / "out"
    row = _skip_convert(pdf, out, "纯文本，没有图片引用")
    assert row.status == producer.STATUS_SKIPPED
    assert row.missing_images == []


# ---------------------------------------------------------------- 参数契约


def test_pdf2md_images_default_on_with_compat_flag() -> None:
    parser = pdf2md.build_parser()
    default = parser.parse_args(["paper.pdf"])
    assert default.keep_images is True, "图片默认落盘"
    assert parser.parse_args(["paper.pdf", "--keep-images"]).keep_images is True
    assert parser.parse_args(["paper.pdf", "--no-images"]).keep_images is False
    with pytest.raises(SystemExit):
        parser.parse_args(["paper.pdf", "--keep-images", "--no-images"])


def test_prework_ocr_images_default_on() -> None:
    args = prework_ocr.parse_args(["convert", "--input", "paper.pdf", "--output-dir", "out"])
    assert args.keep_images is True
    assert prework_ocr.parse_args(
        ["convert", "--input", "paper.pdf", "--output-dir", "out", "--no-images"]
    ).keep_images is False


def test_run_pipeline_passes_no_images_through() -> None:
    import run_pipeline as pipeline

    parser = pipeline.build_parser()
    default = parser.parse_args(["paper.pdf"])
    assert default.keep_images is True
    assert parser.parse_args(["paper.pdf", "--no-images"]).keep_images is False
