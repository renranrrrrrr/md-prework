"""pdf2md —— PaddleOCR-VL 的"人用"命令行前端（兼容旧用法）。

真正的 OCR 逻辑全部在 ``ocr_producer.py``（唯一实现）；本文件只保留历史 CLI 契约：

* 位置参数：一个文件夹，或一个/多个 PDF 文件；目录只取直接子级；
* 产物写在**PDF 同目录**：``<同名>.md`` 与 ``<同名>_media/``（加 ``--keep-images``）；
* 已存在同名 Markdown 时默认跳过，``--overwrite`` 覆盖；
* 退出码：0 全部成功 / 1 有失败 / 2 输入或环境问题 / 130 中断。

新增能力（默认关闭，保持旧行为）：``--evidence`` 落结构化证据，
``--evidence-raw`` 控制原始结构化返回的落盘方式。

被编排调用请用 ``prework_ocr.py``（``--output-dir`` / ``--stdout`` / 状态留痕）。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import ocr_producer as producer


DEFAULT_MODEL = producer.DEFAULT_MODEL


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
    parser.add_argument("inputs", nargs="+", help="一个文件夹，或一个 / 多个 PDF 文件路径")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"PaddleOCR-VL 模型名（默认 {DEFAULT_MODEL}）"
    )
    parser.add_argument("--overwrite", action="store_true", help="已存在同名 .md 时覆盖（默认跳过）")
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="把文档内图片导出到 <同名>_media/（默认只写 markdown 文本）",
    )
    parser.add_argument(
        "--evidence", action="store_true", help="同时写 <同名>.evidence.json 结构化证据"
    )
    parser.add_argument(
        "--evidence-raw",
        choices=("none", "page", "full"),
        default="page",
        help="原始结构化返回的落盘方式（配合 --evidence 使用）",
    )
    parser.add_argument(
        "--poll-timeout", type=float, default=900.0, help="单份 PDF 的总轮询超时秒数（默认 900）"
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="遇到配额/限流错误时的总尝试次数（默认 3）"
    )
    parser.add_argument(
        "--retry-delay", type=float, default=10.0, help="首次重试等待秒数，之后翻倍（默认 10）"
    )
    parser.add_argument("--token", default="", help="直接传入令牌（默认按环境变量解析）")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    pdfs, problems = producer.collect_pdfs(args.inputs)
    for problem in problems:
        print(f"[输入] {problem}", file=sys.stderr)
    if not pdfs:
        print("没有可转换的 PDF。", file=sys.stderr)
        return producer.EXIT_ENVIRONMENT

    try:
        token, token_source = producer.resolve_token(args.token)
    except EnvironmentError as exc:
        print(str(exc), file=sys.stderr)
        return producer.EXIT_ENVIRONMENT

    print(f"共 {len(pdfs)} 个 PDF，模型 {args.model}，API 源 {producer.DEFAULT_PROVIDER}")
    print(f"令牌来源：{token_source}\n")

    ok = 0
    for index, pdf in enumerate(pdfs, start=1):
        print(f"[{index}/{len(pdfs)}] {pdf}")
        try:
            row = await producer.convert_pdf(
                pdf,
                output_dir=None,
                token=token,
                token_source=token_source,
                model=args.model,
                keep_images=args.keep_images,
                overwrite=args.overwrite,
                retries=args.retries,
                retry_delay=args.retry_delay,
                write_evidence_file=args.evidence,
                raw_mode=args.evidence_raw,
                log=lambda message: print(message),
            )
        except KeyboardInterrupt:
            print("\n已中断。", file=sys.stderr)
            return producer.EXIT_INTERRUPTED
        except Exception as exc:  # noqa: BLE001 - 单个失败不应中断整批
            print(f"  [失败] {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        if row.status == producer.STATUS_SKIPPED:
            print(f"  [跳过] 已存在：{row.markdown or row.evidence}（需要覆盖请加 --overwrite）")
            ok += 1
            continue
        if row.status != producer.STATUS_OK:
            print(f"  [失败] {row.error or row.reason}", file=sys.stderr)
            continue

        note = f"{row.pages} 页"
        if row.media_dir:
            note += f"，{row.images_saved}/{row.images_total} 张图 -> {row.media_dir}/"
        elif row.images_total:
            note += f"，{row.images_total} 张图（未落盘，加 --keep-images 可导出）"
        if row.evidence:
            note += f"，证据 {row.evidence}（{row.blocks} 块）"
        for failure in row.image_failures:
            print(f"  [警告] {failure}")
        for missing in row.missing_images:
            print(f"  [警告] 这张图没能保存到本地，markdown 里仍指向远端：{missing}")
        print(f"  [完成] {pdf.name} -> {pathlib.Path(row.markdown).name}（{note}）")
        ok += 1

    print(f"\n完成 {ok}/{len(pdfs)}。")
    return producer.EXIT_OK if ok == len(pdfs) else producer.EXIT_ITEM_FAILED


def main() -> int:
    producer.force_utf8_console()
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return producer.EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
