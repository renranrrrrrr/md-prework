"""图片导出：默认落盘、可显式关闭，以及跨入口的参数传递。

不联网：图片字节全部由本地构造，远端下载用假函数替掉。
"""

from __future__ import annotations

import asyncio
import base64
import pathlib
import sys
from types import SimpleNamespace

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import pdf2md  # noqa: E402
import run_pipeline as pipeline  # noqa: E402
import run_pipeline_parallel as parallel  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff" + b"\x00" * 24

# 三个入口必须给出完全相同的图片开关语义
ENTRY_PARSERS = [
    ("pdf2md", pdf2md.build_parser),
    ("run_pipeline", pipeline.build_parser),
    ("run_pipeline_parallel", parallel.build_parser),
]


# ---------------------------------------------------------------- 命令行默认值


@pytest.mark.parametrize("name,build_parser", ENTRY_PARSERS)
def test_images_are_on_by_default(name, build_parser):
    assert build_parser().parse_args(["a.pdf"]).keep_images is True


@pytest.mark.parametrize("name,build_parser", ENTRY_PARSERS)
def test_keep_images_is_still_accepted(name, build_parser):
    """旧命令仍要能跑：--keep-images 现在只是冗余的显式开启。"""
    assert build_parser().parse_args(["a.pdf", "--keep-images"]).keep_images is True


@pytest.mark.parametrize("name,build_parser", ENTRY_PARSERS)
def test_no_images_turns_export_off(name, build_parser):
    assert build_parser().parse_args(["a.pdf", "--no-images"]).keep_images is False


@pytest.mark.parametrize(
    "flags",
    [
        ("--keep-images", "--no-images"),
        ("--no-images", "--keep-images"),
    ],
)
@pytest.mark.parametrize("name,build_parser", ENTRY_PARSERS)
def test_image_flags_are_mutually_exclusive(name, build_parser, flags):
    """两个开关同时给（任何顺序）都按用法错误退出，而不是「后写的赢」。"""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["a.pdf", *flags])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------- 落盘与改写


def test_write_media_keeps_structure_and_rewrites_img_src(tmp_path, monkeypatch):
    pdf = tmp_path / "第一套.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(pdf2md, "_download_image", lambda url, **kwargs: PNG)
    images = {
        # base64 负载（本地推理路径）：扩展名与内容一致
        "imgs/img_chart_box_1_2.jpg": base64.b64encode(JPEG).decode(),
        # URL 负载（AI Studio 路径）：子目录结构要保留
        "imgs/img_in_image_box_2_1.png": "https://cdn.example.com/a.png",
    }

    media_dir, mapping, failures = pdf2md.write_media(pdf, images)

    assert failures == []
    assert media_dir == tmp_path / "第一套_media"
    assert (media_dir / "imgs" / "img_chart_box_1_2.jpg").read_bytes() == JPEG
    assert (media_dir / "imgs" / "img_in_image_box_2_1.png").read_bytes() == PNG
    assert mapping["imgs/img_chart_box_1_2.jpg"] == "第一套_media/imgs/img_chart_box_1_2.jpg"

    markdown = (
        '<img src="imgs/img_chart_box_1_2.jpg" alt="x">\n'
        "![图](imgs/img_in_image_box_2_1.png)\n"
    )
    rewritten = pdf2md.rewrite_image_sources(markdown, mapping)
    assert 'src="第一套_media/imgs/img_chart_box_1_2.jpg"' in rewritten
    # 只改 img 的 src：Markdown 图片语法与其它内容原样保留
    assert "![图](imgs/img_in_image_box_2_1.png)" in rewritten


def test_write_media_fixes_suffix_from_file_header(tmp_path):
    """键名后缀与真实格式不符时，按文件头纠正，别写出会骗人的扩展名。"""
    pdf = tmp_path / "A.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    media_dir, mapping, failures = pdf2md.write_media(
        pdf, {"imgs/x.jpg": base64.b64encode(PNG).decode()}
    )

    assert failures == []
    assert mapping["imgs/x.jpg"] == "A_media/imgs/x.png"
    assert (media_dir / "imgs" / "x.png").read_bytes() == PNG


def test_write_media_reports_unusable_payload_without_writing(tmp_path):
    pdf = tmp_path / "A.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    media_dir, mapping, failures = pdf2md.write_media(
        pdf, {"imgs/x.png": "not-an-image-payload"}
    )

    assert mapping == {}
    assert failures and "imgs/x.png" in failures[0]
    assert not media_dir.exists()


def test_data_url_payload_is_accepted(tmp_path):
    pdf = tmp_path / "A.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    payload = "data:image/png;base64," + base64.b64encode(PNG).decode()

    _, mapping, failures = pdf2md.write_media(pdf, {"imgs/x.png": payload})

    assert failures == []
    assert mapping == {"imgs/x.png": "A_media/imgs/x.png"}


# ---------------------------------------------------------------- 缺图判定


def test_missing_detection_covers_remote_relative_and_data(tmp_path):
    (tmp_path / "A_media" / "imgs").mkdir(parents=True)
    (tmp_path / "A_media" / "imgs" / "ok.png").write_bytes(PNG)
    (tmp_path / "绝对路径图.png").write_bytes(PNG)
    markdown = "\n".join(
        [
            '<img src="https://cdn.example.com/a.png">',          # 远端 → 缺失
            '<img src="imgs/img_in_image_box_1_2.jpg">',          # 相对且不存在 → 缺失
            '<img src="A_media/imgs/ok.png">',                    # 相对且存在 → 不缺失
            '<img src="A_media/imgs/gone.png">',                  # 相对且不存在 → 缺失
            '<img src="data:image/png;base64,AAAA">',             # 内嵌 → 不缺失
            f'<img src="{tmp_path / "绝对路径图.png"}">',          # 绝对且存在 → 不缺失
            f'<img src="{tmp_path / "没有这张图.png"}">',          # 绝对且不存在 → 缺失
        ]
    )

    assert pdf2md.find_missing_image_sources(markdown, tmp_path) == [
        "https://cdn.example.com/a.png",
        "imgs/img_in_image_box_1_2.jpg",
        "A_media/imgs/gone.png",
        str(tmp_path / "没有这张图.png"),
    ]


def test_missing_detection_uses_markdown_directory_as_base(tmp_path):
    sub = tmp_path / "试卷"
    sub.mkdir()
    markdown = '<img src="A_media/imgs/a.png">'

    # 相对路径按 markdown 所在目录解析，而不是当前工作目录
    assert pdf2md.find_missing_image_sources(markdown, sub) == ["A_media/imgs/a.png"]
    (sub / "A_media" / "imgs").mkdir(parents=True)
    (sub / "A_media" / "imgs" / "a.png").write_bytes(PNG)
    assert pdf2md.find_missing_image_sources(markdown, sub) == []


def test_missing_detection_dedups_and_keeps_order(tmp_path):
    markdown = (
        '<img src="https://a/1.png">\n'
        '<img src="https://a/1.png">\n'
        '<img src="https://a/2.png">\n'
    )
    assert pdf2md.find_missing_image_sources(markdown, tmp_path) == [
        "https://a/1.png",
        "https://a/2.png",
    ]


def test_missing_detection_ignores_non_img_syntax(tmp_path):
    markdown = "![图](https://cdn.example.com/a.png)\n正文\n"
    assert pdf2md.find_missing_image_sources(markdown, tmp_path) == []


# ---------------------------------------------------------------- 跳过时的警告


def test_skip_warning_reports_unlocalized_images(tmp_path, capsys):
    md_path = tmp_path / "A.md"
    md_path.write_text(
        '<img src="https://cdn.example.com/a.png">\n'
        '<img src="imgs/img_in_image_box_1_2.jpg">\n',
        encoding="utf-8",
    )

    pdf2md._warn_unlocalized_images(md_path)

    out = capsys.readouterr().out
    assert "有 2 张图没落盘" in out
    assert "https://cdn.example.com/a.png" in out
    assert "imgs/img_in_image_box_1_2.jpg" in out
    assert "--overwrite" in out


def test_skip_warning_is_quiet_when_images_are_local(tmp_path, capsys):
    md_path = tmp_path / "A.md"
    (tmp_path / "A_media" / "imgs").mkdir(parents=True)
    (tmp_path / "A_media" / "imgs" / "a.png").write_bytes(PNG)
    md_path.write_text(
        '<img src="A_media/imgs/a.png">\n<img src="data:image/png;base64,AAAA">',
        encoding="utf-8",
    )

    pdf2md._warn_unlocalized_images(md_path)
    assert capsys.readouterr().out == ""


def test_convert_one_warns_but_skips_existing_markdown(tmp_path, capsys):
    """已有 markdown 时不重新调 API，但要把缺图说清楚。"""
    pdf = tmp_path / "A.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    pdf.with_suffix(".md").write_text(
        '<img src="imgs/img_in_image_box_1_2.jpg">', encoding="utf-8"
    )

    def create_inference(**kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("跳过分支不应该创建推理客户端")

    ok = asyncio.run(
        pdf2md.convert_one(
            create_inference,
            lambda *, input_data: input_data,
            pdf,
            token="fake",
            model="PaddleOCR-VL-1.6",
            poll_timeout=1.0,
            overwrite=False,
            keep_images=True,
            retries=1,
            retry_delay=0.0,
        )
    )

    assert ok
    out = capsys.readouterr().out
    assert "[跳过]" in out
    assert "没落盘" in out


# ---------------------------------------------------------------- 端到端（假推理）


class _FakeInference:
    def __init__(self, result):
        self._result = result

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def predict(self, request):
        return self._result


def _convert(tmp_path, *, keep_images: bool) -> str:
    """跑一次真正的 convert_one，但用假推理替掉远端调用。不联网。"""
    pdf = tmp_path / "第一套.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = SimpleNamespace(
        markdown='前文\n<img src="imgs/img_box_1_2.jpg" alt="x">\n$$\nx = 1  \n$$\n',
        pages=1,
        images_mapping={"imgs/img_box_1_2.jpg": base64.b64encode(JPEG).decode()},
    )

    def create_inference(**kwargs):
        return _FakeInference(result)

    def request(*, input_data):
        return input_data

    ok = asyncio.run(
        pdf2md.convert_one(
            create_inference,
            request,
            pdf,
            token="fake",
            model="PaddleOCR-VL-1.6",
            poll_timeout=1.0,
            overwrite=False,
            keep_images=keep_images,
            retries=1,
            retry_delay=0.0,
        )
    )
    assert ok
    return (pdf.with_suffix(".md")).read_text(encoding="utf-8")


def test_convert_one_saves_images_by_default(tmp_path):
    markdown = _convert(tmp_path, keep_images=True)

    media = tmp_path / "第一套_media" / "imgs" / "img_box_1_2.jpg"
    assert media.read_bytes() == JPEG
    assert 'src="第一套_media/imgs/img_box_1_2.jpg"' in markdown
    assert "https://" not in markdown
    # 其它规范化照旧：行尾空白仍被清掉
    assert "x = 1\n$$" in markdown


def test_convert_one_without_images_keeps_remote_reference(tmp_path):
    markdown = _convert(tmp_path, keep_images=False)

    assert not (tmp_path / "第一套_media").exists()
    assert 'src="imgs/img_box_1_2.jpg"' in markdown


# ---------------------------------------------------------------- 串行 pipeline


def _pdf2md_command(tmp_path, monkeypatch, *, keep_images: bool) -> list[str]:
    """跑一次 process()，只取它给 pdf2md 的那条命令。"""
    pdf = tmp_path / "A.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    commands: list[list[str]] = []

    def fake_run(command):
        commands.append(list(command))
        # OCR 直接失败，流程到此为止——本用例只关心命令怎么拼的
        return 1, "模拟失败"

    monkeypatch.setattr(pipeline, "run", fake_run)
    result = pipeline.process(
        pdf,
        tool_python=pathlib.Path(sys.executable),
        normalizer=["python", "-m", "md_math_normalizer"],
        overwrite=False,
        keep_images=keep_images,
        verbose=False,
        limit=5,
    )
    assert result["failed"] is True
    return commands[0]


def test_pipeline_passes_keep_images_by_default(tmp_path, monkeypatch):
    command = _pdf2md_command(tmp_path, monkeypatch, keep_images=True)
    assert "--keep-images" in command
    assert "--no-images" not in command


def test_pipeline_can_turn_images_off(tmp_path, monkeypatch):
    command = _pdf2md_command(tmp_path, monkeypatch, keep_images=False)
    assert "--no-images" in command
    assert "--keep-images" not in command
