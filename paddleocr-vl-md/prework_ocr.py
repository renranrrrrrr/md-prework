"""prework-ocr —— md-prework 唯一 OCR producer 的编排入口。

它把两套历史 CLI 的能力合成一个：

* 新版契约：``--output-dir`` / ``--stdout`` / ``convert`` / ``batch`` /
  ``ocr_summary.json`` / ``ocr_status.jsonl`` / 退出码 0/1/2/130；
* 旧版成熟能力：远端压力重试（``remote_pressure``）、调度冷却（复用
  ``run_pipeline_parallel`` 的 ``Backoff`` / ``ScheduleStats``）、CDN 图片退避下载与
  文件头严格校验、失败隔离；
* 新增能力：结构化 Paddle Evidence 原样落盘（``<stem>.evidence.json`` +
  ``<stem>_raw/page-NNNN.json``）。

所有 OCR 逻辑都在 ``ocr_producer.py`` 里，本文件只做参数解析与编排。

用法：

    python prework_ocr.py convert --input paper.pdf --output-dir out --keep-images
    python prework_ocr.py batch   --pdf-dir pdfs  --output-dir out --keep-images
    python prework_ocr.py convert --input paper.pdf --output-dir out --stdout
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time

import ocr_producer as producer
from remote_pressure import classify_failure, should_cool_down


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PaddleOCR-VL producer：原始 PDF → 结构化证据 + Markdown + 图片。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--output-dir", required=True, help="产物目录（证据、Markdown、图片都写这里）")
        sub.add_argument("--model", default=producer.DEFAULT_MODEL, help="模型名")
        sub.add_argument("--provider", default=producer.DEFAULT_PROVIDER, help="inference provider")
        sub.add_argument(
            "--token-env",
            action="append",
            default=[],
            help="令牌环境变量名（可重复，按顺序解析；默认 PADDLEOCR_API_KEY → 旧名兜底）",
        )
        sub.add_argument("--token", default="", help="直接传入令牌（不推荐，仅临时诊断）")
        sub.add_argument("--base-url", default="", help="可选：覆盖 AI Studio base URL")
        sub.add_argument("--poll-timeout", type=float, default=900.0, help="单份 PDF 轮询超时秒数")
        sub.add_argument("--request-timeout", type=float, default=120.0, help="单次 HTTP 请求超时秒数")
        sub.add_argument("--retries", type=int, default=3, help="远端压力类错误的总尝试次数")
        sub.add_argument("--retry-delay", type=float, default=10.0, help="首次重试等待秒数（之后翻倍）")
        sub.add_argument("--page-ranges", default="", help="只处理指定页范围，例如 3-5")
        sub.add_argument("--overwrite", action="store_true", help="覆盖已有产物与证据")
        sub.add_argument(
            "--keep-images",
            dest="keep_images",
            action="store_true",
            help="导出文档内图片到 <stem>_media/（默认开启，与旧 CLI 同名）",
        )
        sub.add_argument(
            "--no-images",
            dest="keep_images",
            action="store_false",
            help="不导出文档内图片",
        )
        sub.add_argument(
            "--evidence",
            dest="evidence",
            action="store_true",
            help="写结构化证据 <stem>.evidence.json（默认开启）",
        )
        sub.add_argument(
            "--no-evidence",
            dest="evidence",
            action="store_false",
            help="不写结构化证据",
        )
        sub.add_argument(
            "--evidence-raw",
            choices=("none", "page", "full"),
            default="page",
            help="原始结构化返回的落盘方式：none 只写摘要，page 逐页原样，full 额外内嵌进证据",
        )
        sub.add_argument(
            "--stdout",
            action="store_true",
            help="把 Markdown 写到 stdout（只允许单个输入；进度与警告走 stderr）",
        )
        sub.set_defaults(keep_images=True, evidence=True)

    convert = subparsers.add_parser("convert", help="转换一个或多个 PDF")
    convert.add_argument("--input", action="append", required=True, help="PDF 路径，可重复")
    add_common(convert)

    batch = subparsers.add_parser("batch", help="批量转换目录下的 PDF")
    batch.add_argument("--pdf-dir", required=True, help="PDF 所在目录（不递归）")
    batch.add_argument("--pattern", default="*.pdf", help="文件匹配模式（默认 *.pdf）")
    batch.add_argument("--limit", type=int, default=0, help="只处理前 N 份（0 表示全部）")
    batch.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="并发 worker 数（默认 1；>4 会提示远端配额吃紧）",
    )
    batch.add_argument(
        "--launch-interval",
        type=float,
        default=0.5,
        help="连续启动两个 worker 的最小间隔秒数（默认 0.5）",
    )
    add_common(batch)
    return parser.parse_args(argv)


def collect_targets(args: argparse.Namespace) -> list[pathlib.Path]:
    if args.command == "batch":
        directory = pathlib.Path(args.pdf_dir)
        if not directory.is_dir():
            raise SystemExit(f"PDF 目录不存在：{directory}")
        pdfs = sorted(item for item in directory.glob(args.pattern) if item.is_file())
        if args.limit > 0:
            pdfs = pdfs[: args.limit]
        return pdfs
    pdfs, problems = producer.collect_pdfs(args.input)
    for problem in problems:
        print(f"[输入] {problem}", file=sys.stderr)
    return pdfs


async def run_serial(
    pdfs: list[pathlib.Path],
    args: argparse.Namespace,
    *,
    token: str,
    token_source: str,
    log,
    stdout_markdown: bool,
) -> list[producer.ConvertOutcome]:
    rows: list[producer.ConvertOutcome] = []
    for index, pdf in enumerate(pdfs, start=1):
        log(f"[{index}/{len(pdfs)}] {pdf.name}")
        try:
            row = await producer.convert_pdf(
                pdf,
                output_dir=pathlib.Path(args.output_dir),
                token=token,
                token_source=token_source,
                model=args.model,
                provider=args.provider,
                keep_images=args.keep_images,
                overwrite=args.overwrite,
                page_ranges=args.page_ranges,
                retries=args.retries,
                retry_delay=args.retry_delay,
                write_evidence_file=args.evidence,
                raw_mode=args.evidence_raw,
                log=log,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个失败不中断整批
            row = producer.ConvertOutcome(
                pdf=pdf.name,
                status=producer.STATUS_FAILED,
                error=f"{type(exc).__name__}: {exc}",
                model=args.model,
                token_source=token_source,
            )
            log(f"  [失败] {row.error}")
        if stdout_markdown and row.status == producer.STATUS_OK:
            row.stdout_markdown = True
        _report_row(row, log)
        rows.append(row)
    return rows


async def run_concurrent(
    pdfs: list[pathlib.Path],
    args: argparse.Namespace,
    *,
    token: str,
    token_source: str,
    log,
) -> list[producer.ConvertOutcome]:
    """受限并发 + 远端压力冷却。

    冷却策略直接复用调度器（``run_pipeline_parallel``）的 ``Backoff`` /
    ``ScheduleStats`` 与 ``remote_pressure`` 分类，不另建第二套限流规则；
    只是把等待换成 asyncio，避免阻塞事件循环。
    """

    from run_pipeline_parallel import Backoff, DEFAULT_LAUNCH_INTERVAL, ScheduleStats

    jobs = max(1, args.jobs)
    if jobs > 4:
        log(f"[提示] jobs={jobs} 偏高：远端 OCR 是共享配额服务，建议先用 2 跑通")
    stats = ScheduleStats()
    backoff = Backoff()
    launch_interval = args.launch_interval if args.launch_interval >= 0 else DEFAULT_LAUNCH_INTERVAL
    semaphore = asyncio.Semaphore(jobs)
    rows: dict[str, producer.ConvertOutcome] = {}
    order = [pdf.name for pdf in pdfs]

    async def _wait_admission(index: int) -> None:
        if index > 0 and launch_interval > 0:
            await asyncio.sleep(launch_interval)
        while backoff.in_cooldown():
            await asyncio.sleep(min(1.0, backoff.cooldown_remaining()))

    async def _worker(index: int, pdf: pathlib.Path) -> None:
        async with semaphore:
            await _wait_admission(index)
            log(f"[{index + 1}/{len(pdfs)}] 开始 {pdf.name}")
            try:
                row = await producer.convert_pdf(
                    pdf,
                    output_dir=pathlib.Path(args.output_dir),
                    token=token,
                    token_source=token_source,
                    model=args.model,
                    provider=args.provider,
                    keep_images=args.keep_images,
                    overwrite=args.overwrite,
                    page_ranges=args.page_ranges,
                    retries=args.retries,
                    retry_delay=args.retry_delay,
                    write_evidence_file=args.evidence,
                    raw_mode=args.evidence_raw,
                    log=log,
                )
            except Exception as exc:  # noqa: BLE001 - 失败隔离
                row = producer.ConvertOutcome(
                    pdf=pdf.name,
                    status=producer.STATUS_FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    model=args.model,
                    token_source=token_source,
                )
            verdict = classify_failure(row.error or row.reason)
            if row.status == producer.STATUS_OK:
                backoff.note_success()
            elif should_cool_down(verdict):
                cooldown = backoff.note_backpressure()
                log(f"  [冷却] 检测到远端压力（{verdict.reason}），暂停启动新任务 {cooldown:.0f}s")
            stats.record(verdict, backoff.cooldown_remaining())
            _report_row(row, log)
            rows[pdf.name] = row

    await asyncio.gather(*[_worker(index, pdf) for index, pdf in enumerate(pdfs)])
    if stats.backpressure_events or stats.quota_events:
        log(
            f"[调度] 远端压力 {stats.backpressure_events} 次，额度耗尽 {stats.quota_events} 次，"
            f"峰值冷却 {stats.peak_cooldown:.0f}s"
        )
    return [rows[name] for name in order if name in rows]


def _report_row(row: producer.ConvertOutcome, log) -> None:
    if row.status == producer.STATUS_OK:
        detail = (
            f"{row.pages} 页，{row.chars} 字符，块 {row.blocks}，"
            f"图片 {row.images_saved}/{row.images_total}"
        )
        log(f"  [完成] {row.markdown}（{detail}）")
        for failure in row.image_failures:
            log(f"  [警告] {failure}")
        for missing in row.missing_images:
            log(f"  [警告] 图片引用没有本地文件：{missing}")
    elif row.status == producer.STATUS_SKIPPED:
        log(f"  [跳过] {row.evidence or row.markdown}（{row.reason}）")
    else:
        log(f"  [失败] {row.error or row.reason or 'unknown'}")


def main(argv: list[str] | None = None) -> int:
    producer.force_utf8_console()
    args = parse_args(argv)

    def log(*values: object) -> None:
        # --stdout 模式下 stdout 只承载 Markdown，所有进度/警告都走 stderr。
        print(*values, file=sys.stderr)

    pdfs = collect_targets(args)
    if not pdfs:
        print("没有可处理的 PDF。", file=sys.stderr)
        return producer.EXIT_ENVIRONMENT
    if args.stdout and len(pdfs) != 1:
        print("--stdout 只支持单个输入（编排方需要可解析的 stdout）。", file=sys.stderr)
        return producer.EXIT_ENVIRONMENT

    try:
        token, token_source = producer.resolve_token(
            args.token, tuple(args.token_env) or producer.DEFAULT_TOKEN_ENVS
        )
    except EnvironmentError as exc:
        print(str(exc), file=sys.stderr)
        return producer.EXIT_ENVIRONMENT

    log(
        f"[prework-ocr] model={args.model} provider={args.provider} "
        f"token={token_source} pdfs={len(pdfs)} -> {args.output_dir}"
    )

    interrupted = False
    try:
        if args.command == "batch" and args.jobs > 1:
            rows = asyncio.run(
                run_concurrent(pdfs, args, token=token, token_source=token_source, log=log)
            )
        else:
            rows = asyncio.run(
                run_serial(
                    pdfs,
                    args,
                    token=token,
                    token_source=token_source,
                    log=log,
                    stdout_markdown=args.stdout,
                )
            )
    except KeyboardInterrupt:
        interrupted = True
        rows = []

    output_dir = pathlib.Path(args.output_dir)
    if rows:
        if args.command == "batch":
            producer.write_status_log(output_dir, rows)
        producer.write_summary(
            output_dir,
            rows,
            model=args.model,
            provider=args.provider,
            token_source=token_source,
            command=" ".join(sys.argv[1:]) if argv is None else " ".join(argv),
        )

    if args.stdout and rows and rows[0].status == producer.STATUS_OK:
        md_path = output_dir / rows[0].markdown
        if md_path.is_file():
            print(md_path.read_text(encoding="utf-8"))
    elif rows:
        print(str(output_dir / "ocr_summary.json"))

    if interrupted:
        return producer.EXIT_INTERRUPTED
    failed = sum(1 for row in rows if row.status == producer.STATUS_FAILED)
    return producer.EXIT_OK if failed == 0 and rows else producer.EXIT_ITEM_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
