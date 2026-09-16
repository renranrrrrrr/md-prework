"""pdf2md —— 用 PaddleOCR-VL 的 AI Studio API 把 PDF 转成 Markdown。

完全独立的命令行工具：不依赖 DSH、不依赖任何智能体、不跑 MCP 服务、
不做本地推理（不安装 paddlepaddle）。只作为 HTTP API 的调用方。

两种输入模式
    # 模式 1：给一个文件夹，转换该目录下的全部 PDF（不含子目录）
    python pdf2md.py "D:\\Users\\lenovo\\Documents"

    # 模式 2：给一个或多个 PDF 文件
    python pdf2md.py "D:\\a\\1.pdf" "D:\\b\\2.pdf"

输出：每个 PDF 同目录下生成同名 .md；图片（如有）写入同目录的 <同名>_media/
令牌：从用户级环境变量 PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN 读取
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ENV_TOKEN = "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"

# 远端服务压力的分类只保留一份实现，见 remote_pressure.py。
# 它覆盖 HTTP 429 / 503 / 504、业务码 10010 / 12002 与服务端中英短语，
# 并把「当日额度耗尽」排除在可重试之外（重试无意义）。
from remote_pressure import is_retryable_remote_error

# 图片在 markdown 里可能是 data URL 或裸 base64，统一去掉前缀。
_DATA_URL_PREFIXES = ("data:image/", "data:application/")


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文与标记符号会乱码或抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _read_user_env(name: str) -> str | None:
    """读用户级环境变量，作为当前进程环境缺失时的兜底。

    刚设置的用户变量不会进入已经在运行的进程（终端、工具都可能受影响），
    这里直接查注册表，避免"明明设置了却说没设置"。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value or None


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    from_process = os.environ.get(ENV_TOKEN)
    if from_process:
        return from_process
    from_user_scope = _read_user_env(ENV_TOKEN)
    if from_user_scope:
        return from_user_scope
    raise SystemExit(f"未找到令牌：请设置环境变量 {ENV_TOKEN}，或用 --token 传入。")


def collect_pdfs(raw_inputs: list[str]) -> tuple[list[pathlib.Path], list[str]]:
    """把用户输入展开成 PDF 路径列表。

    文件夹参数只取该目录**直接包含**的 .pdf，不递归子目录。
    文件参数照原样收下。返回 (pdf 列表, 问题说明列表)。
    """
    pdfs: list[pathlib.Path] = []
    problems: list[str] = []
    seen: set[str] = set()

    def _add(candidate: pathlib.Path) -> None:
        key = str(candidate.resolve()).lower()
        if key not in seen:
            seen.add(key)
            pdfs.append(candidate)

    for raw in raw_inputs:
        path = pathlib.Path(raw).expanduser()
        if not path.exists():
            problems.append(f"路径不存在：{path}")
            continue

        if path.is_dir():
            found = sorted(
                item
                for item in path.iterdir()
                if item.is_file() and item.suffix.lower() == ".pdf"
            )
            if not found:
                problems.append(f"目录下没有 PDF（已忽略子目录）：{path}")
                continue
            for item in found:
                _add(item)
            continue

        if path.suffix.lower() != ".pdf":
            problems.append(f"不是 PDF 文件，已跳过：{path}")
            continue
        _add(path)

    return pdfs, problems


_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
)


def detect_image_format(data: bytes) -> str | None:
    """按文件头判断图片格式，识别不出来返回 None。"""
    for signature, name in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return name
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def decode_image_payload(payload: str) -> bytes:
    """把图片负载还原成字节。

    负载有两种形态：AI Studio 返回的是**图片 URL**，本地推理返回的是 base64。
    两者都支持。
    """
    data = str(payload).strip()
    if data.lower().startswith(("http://", "https://")):
        return _download_image(data)
    if data.lower().startswith(_DATA_URL_PREFIXES) and "," in data:
        data = data.split(",", 1)[1]
    data = "".join(data.split())
    padded = data + "=" * (-len(data) % 4)
    return base64.b64decode(padded)


def _download_image(
    url: str,
    *,
    timeout: float = 120.0,
    attempts: int = 3,
    delay: float = 2.0,
) -> bytes:
    """下载图片 URL，失败按退避重试。

    图片托管在 CDN 上，偶发 SSL/连接抖动很常见；不重试会在 markdown 里留下
    指向远端的引用。只用标准库，避免为一个 GET 增加依赖。
    """
    last_error: Exception | None = None
    wait = delay
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": "pdf2md/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(f"  [重试] 图片下载失败（第 {attempt}/{attempts - 1} 次）：{exc}；{wait:.0f}s 后重试")
            time.sleep(wait)
            wait *= 2
    raise OSError(f"下载失败（已重试 {attempts} 次）：{last_error}")


def write_media(
    pdf: pathlib.Path,
    images: dict[str, str],
) -> tuple[pathlib.Path, dict[str, str], list[str]]:
    """把解析出的图片写到 <同名>_media/，返回 (目录, 路径映射, 失败说明)。

    映射是 ``{markdown 里的原始 src: 新的相对路径}``。原始 src 可能带子目录
    （例如 ``imgs/img_in_chart_box_1_2.jpg``），这里按原样保留目录结构，
    保证 markdown 里的相对路径依然能对上文件。
    """
    media_dir = pdf.with_name(f"{pdf.stem}_media")
    mapping: dict[str, str] = {}
    failures: list[str] = []

    for name, payload in images.items():
        relative = pathlib.PurePosixPath(str(name).replace("\\", "/").lstrip("/"))
        relative = pathlib.PurePosixPath(*[part for part in relative.parts if part not in ("", "..")])
        if not relative.name:
            failures.append(f"图片键名无效，已跳过：{name!r}")
            continue
        target = media_dir.joinpath(*relative.parts)
        try:
            blob = decode_image_payload(payload)
        except (binascii.Error, ValueError, OSError) as exc:
            failures.append(f"图片 {name} 获取失败：{exc}")
            continue
        image_format = detect_image_format(blob)
        if image_format is None:
            failures.append(f"图片 {name} 内容不是可识别图片（{len(blob)} 字节），已跳过")
            continue
        expected_suffix = "." + image_format.lower().replace("jpeg", "jpg")
        if relative.suffix.lower() != expected_suffix:
            relative = relative.with_suffix(expected_suffix)
        target = media_dir.joinpath(*relative.parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        except OSError as exc:
            failures.append(f"图片 {name} 写入失败：{exc}")
            continue
        mapping[str(name)] = str(pathlib.PurePosixPath(media_dir.name) / relative)

    return media_dir, mapping, failures


def normalize_block_math(markdown: str) -> str:
    """去掉块级公式行的行尾空白。

    OCR 会输出 ``  $$ ... $$ ``（前后都有空格）。行尾空格会让标准 Markdown
    渲染器认不出 ``$$`` 块，所以这里清掉每一行的行尾空白。
    行内公式与块级公式的内容一律不动。
    """
    return "\n".join(line.rstrip() for line in markdown.split("\n"))


def find_broken_image_sources(markdown: str, mapping: dict[str, str]) -> list[str]:
    """找出仍然没有本地文件的 img src。

    图片下载失败时不能把引用改成不存在的本地路径，但也不能悄悄留着远端 URL：
    调用方拿到这份清单后必须明确告知用户。
    """
    broken = []
    for match in re.finditer(r'<img[^>]*?src="([^"]+)"', markdown):
        source = match.group(1)
        if source in mapping:
            continue
        if source.lower().startswith(("http://", "https://")):
            broken.append(source)
    return broken


def rewrite_image_sources(markdown: str, mapping: dict[str, str]) -> str:
    """把 markdown 里的图片 src 重写成实际落盘位置。

    只替换 img 标签的 src 属性取值，其余内容不动。
    """
    if not mapping:
        return markdown

    def _replace(match: re.Match[str]) -> str:
        original = match.group("src")
        replacement = mapping.get(original)
        if replacement is None:
            return match.group(0)
        return f'{match.group("prefix")}{replacement}{match.group("suffix")}'

    return re.sub(
        r'(?P<prefix><img[^>]*?src=")(?P<src>[^"]+)(?P<suffix>")',
        _replace,
        markdown,
    )


def _is_rate_limited(exc: Exception) -> bool:
    """是否为**临时**远端服务压力（可重试）。

    额度耗尽、鉴权失败、本地错误都不算——重试它们没有意义。
    """
    return is_retryable_remote_error(exc)


async def _predict_with_retry(infer, request, *, attempts: int, delay: float):
    """调用一次推理；遇到临时远端压力时按退避重试。

    这是 **PDF 内部** 的重试（处理单次临时远端失败），与调度器的 cooldown 职责不同：
    调度器只控制「何时提交下一个 PDF」，绝不重跑已失败的完整任务。
    """
    last_error: Exception | None = None
    wait = delay
    for attempt in range(1, attempts + 1):
        try:
            return await infer.predict(request)
        except Exception as exc:  # noqa: BLE001 - 需要按错误内容判断是否重试
            last_error = exc
            if not _is_rate_limited(exc) or attempt == attempts:
                raise
            print(
                f"  [重试] 第 {attempt}/{attempts - 1} 次遇到服务端限流：{exc}"
                f"；{wait:.0f}s 后重试"
            )
            await asyncio.sleep(wait)
            wait *= 2
    raise last_error if last_error else RuntimeError("unreachable")


async def convert_one(
    create_inference,
    inference_request,
    pdf: pathlib.Path,
    *,
    token: str,
    model: str,
    poll_timeout: float,
    overwrite: bool,
    keep_images: bool,
    retries: int,
    retry_delay: float,
) -> bool:
    md_path = pdf.with_suffix(".md")
    if md_path.exists() and not overwrite:
        print(f"  [跳过] 已存在：{md_path.name}（需要覆盖请加 --overwrite）")
        return True

    infer = create_inference(
        model=model,
        provider="aistudio",
        token=token,
        poll_timeout=poll_timeout,
    )
    await infer.start()
    try:
        # 输入契约要求绝对路径。
        result = await _predict_with_retry(
            infer,
            inference_request(input_data=str(pdf.resolve())),
            attempts=retries,
            delay=retry_delay,
        )
    finally:
        await infer.stop()

    markdown = normalize_block_math(result.markdown)
    if not markdown.strip():
        print(f"  [失败] 未识别到内容：{pdf.name}")
        return False

    note = f"{result.pages} 页"
    if result.images_mapping and keep_images:
        media_dir, mapping, failures = write_media(pdf, result.images_mapping)
        markdown = rewrite_image_sources(markdown, mapping)
        note += f"，{len(mapping)}/{len(result.images_mapping)} 张图 -> {media_dir.name}/"
        for failure in failures:
            print(f"  [警告] {failure}")
        for broken in find_broken_image_sources(markdown, mapping):
            print(f"  [警告] 这张图没能保存到本地，markdown 里仍指向远端：{broken}")
    elif result.images_mapping:
        note += f"，{len(result.images_mapping)} 张图（未落盘，加 --keep-images 可导出）"

    md_path.write_text(markdown, encoding="utf-8")
    print(f"  [完成] {pdf.name} -> {md_path.name}（{note}）")
    return True


async def main_async(args: argparse.Namespace) -> int:
    token = resolve_token(args.token)
    pdfs, problems = collect_pdfs(args.inputs)

    for problem in problems:
        print(f"[输入] {problem}", file=sys.stderr)
    if not pdfs:
        print("没有可转换的 PDF。", file=sys.stderr)
        return 2

    try:
        from paddleocr_mcp.inference import create_inference
        from paddleocr_mcp.inference.types import InferenceRequest
    except ImportError as exc:
        print(f"缺少依赖 {exc.name}，请先安装：paddleocr-mcp", file=sys.stderr)
        return 2

    print(f"共 {len(pdfs)} 个 PDF，模型 {args.model}，API 源 aistudio\n")
    ok = 0
    for index, pdf in enumerate(pdfs, start=1):
        print(f"[{index}/{len(pdfs)}] {pdf}")
        try:
            if await convert_one(
                create_inference,
                InferenceRequest,
                pdf,
                token=token,
                model=args.model,
                poll_timeout=args.poll_timeout,
                overwrite=args.overwrite,
                keep_images=args.keep_images,
                retries=args.retries,
                retry_delay=args.retry_delay,
            ):
                ok += 1
        except KeyboardInterrupt:
            print("\n已中断。", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 - 单个失败不应中断整批
            print(f"  [失败] {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"\n完成 {ok}/{len(pdfs)}。")
    return 0 if ok == len(pdfs) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2md",
        description=(
            "用 PaddleOCR-VL API 把 PDF 转成 Markdown，输出到 PDF 同目录。"
            "参数为文件夹时只转换该目录内的 PDF（不含子目录）；参数为文件时逐个转换。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  pdf2md "D:\\Users\\lenovo\\Documents"\n'
            '  pdf2md "D:\\a\\1.pdf" "D:\\b\\2.pdf"\n'
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="一个文件夹，或一个 / 多个 PDF 文件路径",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"PaddleOCR-VL 模型名（默认 {DEFAULT_MODEL}）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="已存在同名 .md 时覆盖（默认跳过）",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="把文档内图片导出到 <同名>_media/（默认只写 markdown 文本）",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=900.0,
        help="单份 PDF 的总轮询超时秒数（默认 900）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="遇到配额/限流错误时的总尝试次数（默认 3）",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=10.0,
        help="首次重试等待秒数，之后翻倍（默认 10）",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=f"直接传入令牌（默认读环境变量 {ENV_TOKEN}）",
    )
    return parser


def main() -> int:
    _force_utf8_console()
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
