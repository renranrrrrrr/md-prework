"""对一次真实 OCR 调用做 capability probe，产出报告、结构树与脱敏 fixture。

红线：

- 只探测返回对象结构，**不改变生产逻辑**，不实现任何 layout detector 的替代品；
- 不打印令牌，不把请求头写盘，产物里不出现 base64 图片负载与签名 URL；
- 完整原始响应只在显式传入 ``--raw-dump`` 时写到仓库外路径。

用法：

    python tools/probe_paddle_capabilities.py `
      --input <pdf> --output-dir <repo>/capability `
      --fixture-out <repo>/tests/fixtures/paddle_structured_result.fixture.json `
      --page-ranges 3-5 --max-fixture-pages 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paddle_capability import (  # noqa: E402
    build_capability_report,
    build_fixture,
    envelope_stats,
    resolve_token,
    scan_for_secrets,
    sdk_result_to_observed,
    structure_map,
    wrapper_result_fields,
    write_json,
)


DEFAULT_MODEL = "PaddleOCR-VL-1.6"
DEFAULT_TOKEN_ENV = "PADDLEOCR_API_KEY"
FALLBACK_TOKEN_ENV = "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="探测 PaddleOCR-VL / AI Studio 返回对象的结构化能力（只探测，不改生产逻辑）。"
    )
    parser.add_argument("--input", required=True, help="原始 PDF 路径（不预先渲染页面）")
    parser.add_argument("--output-dir", required=True, help="报告与结构树的输出目录")
    parser.add_argument(
        "--replay",
        default="",
        help="离线复算：读取之前 --raw-dump 保存的作业信封，不发起任何网络调用",
    )
    parser.add_argument(
        "--fixture-out", default="", help="脱敏 fixture 的输出路径（留空则不写）"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"默认 {DEFAULT_MODEL}")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="令牌环境变量名")
    parser.add_argument("--token", default="", help="直接传令牌（仅临时诊断用）")
    parser.add_argument("--page-ranges", default="", help="只探测指定页范围，例如 3-5")
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--poll-timeout", type=float, default=900.0)
    parser.add_argument("--retries", type=int, default=5, help="远端压力类错误的总尝试次数")
    parser.add_argument("--retry-delay", type=float, default=15.0, help="首次重试等待秒数（之后翻倍）")
    parser.add_argument("--max-fixture-pages", type=int, default=2)
    parser.add_argument("--fixture-text-limit", type=int, default=200)
    parser.add_argument(
        "--fixture-box-limit", type=int, default=8, help="fixture 里每页保留的版面框条数"
    )
    parser.add_argument("--run-note", default="", help="写入报告的补充说明（例如本次实跑说明）")
    parser.add_argument(
        "--raw-dump",
        default="",
        help="可选：把完整原始响应写到该路径（必须位于仓库外）",
    )
    return parser.parse_args(argv)


RETRYABLE_TOKENS = ("429", "503", "504", "10010", "12002", "队列已满", "rate", "busy", "限流", "超时")


def _is_retryable(exc: BaseException) -> bool:
    message = str(exc)
    return any(token_text in message for token_text in RETRYABLE_TOKENS)


async def _submit_with_retry(client, model, pdf_path, options, page_ranges, args):
    """远端压力类错误退避重试；配额耗尽这类不可恢复错误直接抛出。"""

    wait = args.retry_delay
    attempts = max(1, args.retries)
    for attempt in range(1, attempts + 1):
        try:
            return await client._submit(model, None, str(pdf_path), options, page_ranges, None)
        except Exception as exc:  # noqa: BLE001 - 需要按错误文本判断是否重试
            if attempt >= attempts or not _is_retryable(exc):
                raise
            print(
                f"[probe] 第 {attempt} 次遇到远端压力：{exc}；{wait:.0f}s 后重试",
                file=sys.stderr,
            )
            await asyncio.sleep(wait)
            wait *= 2
    raise RuntimeError("unreachable")


async def run_probe(
    args: argparse.Namespace, token: str
) -> tuple[dict, bool, object, list[str]]:
    """执行一次真实调用；返回 (观测结构, 原始响应是否可取, 原始响应, 信封键名)。"""

    from paddleocr_mcp.inference.shared.paddleocr_api_sdk import (
        AsyncPaddleOCRClient,
        PaddleOCRVLOptions,
        resolve_document_model,
    )
    from paddleocr._api_client._poller import parse_doc_parsing_result

    if args.replay:
        replay_path = Path(args.replay)
        if not replay_path.is_file():
            raise SystemExit(f"replay 文件不存在：{replay_path}")
        jsonl_data = json.loads(replay_path.read_text(encoding="utf-8"))
        sdk_result = parse_doc_parsing_result("replay", jsonl_data)
        return (
            sdk_result_to_observed(sdk_result),
            True,
            jsonl_data,
            _envelope_keys(jsonl_data),
        )

    pdf_path = Path(args.input).resolve()
    if not pdf_path.is_file():
        raise SystemExit(f"PDF 不存在：{pdf_path}")

    client = AsyncPaddleOCRClient(
        token=token,
        request_timeout=args.request_timeout,
        poll_timeout=args.poll_timeout,
    )
    model = resolve_document_model(args.model)
    options = PaddleOCRVLOptions()
    page_ranges = args.page_ranges or None
    try:
        # 走与生产路径完全相同的提交方式，只是把轮询到的原始 JSONL 留在手里。
        job_id = await _submit_with_retry(client, model, pdf_path, options, page_ranges, args)
        jsonl_data, _ = await client._poller.poll_until_done(job_id)
        sdk_result = parse_doc_parsing_result(job_id, jsonl_data)
        return (
            sdk_result_to_observed(sdk_result),
            True,
            jsonl_data,
            _envelope_keys(jsonl_data),
        )
    except AttributeError:
        # 私有成员不可用时退回公开 API：结构报告依然成立，只是拿不到作业信封。
        print(
            "[probe] 私有成员不可用，退回 client.parse_document()（原始信封不可取）",
            file=sys.stderr,
        )
        sdk_result = await client.parse_document(
            model=model, file_path=str(pdf_path), options=options, page_ranges=page_ranges
        )
        return sdk_result_to_observed(sdk_result), False, None, []
    finally:
        await client.close()


def _envelope_keys(jsonl_data: object) -> list[str]:
    """列出作业信封（每行 JSONL 的 result）里出现过的键名。"""

    keys: set[str] = set()
    if not isinstance(jsonl_data, list):
        return []
    for line in jsonl_data:
        if not isinstance(line, dict):
            continue
        result = line.get("result")
        if isinstance(result, dict):
            keys.update(str(key) for key in result.keys())
        data_info = line.get("dataInfo") or (result or {}).get("dataInfo")
        if isinstance(data_info, dict):
            keys.update(f"dataInfo.{key}" for key in data_info.keys())
    return sorted(keys)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parse_args(argv)
    token = ""
    if args.replay:
        token_source = "replay（无网络调用）"
    else:
        token, token_source = resolve_token(args.token, args.token_env, FALLBACK_TOKEN_ENV)

    from paddleocr_mcp.inference.types import DocParsingResult

    observed, raw_accessible, raw_payload, envelope_keys = asyncio.run(
        run_probe(args, token)
    )

    source = {
        "tool": "paddleocr-vl-md/tools/probe_paddle_capabilities.py",
        "model": args.model,
        "page_ranges": args.page_ranges or "all",
        "input_name": Path(args.input).name,
        "token_source": token_source,
        "mode": "replay" if args.replay else "live",
    }
    if args.run_note:
        source["note"] = args.run_note
    envelope = envelope_stats(raw_payload)
    report = build_capability_report(
        observed,
        wrapper_fields=wrapper_result_fields(DocParsingResult),
        raw_envelope_accessible=raw_accessible,
        envelope_keys=envelope_keys,
        envelope=envelope,
        source=source,
    )
    report["input_sha256_hint"] = _sha256_hint(Path(args.input))

    output_dir = Path(args.output_dir)
    write_json(output_dir / "capability_report.json", report)
    write_json(
        output_dir / "structure_map.json",
        {
            "schema_version": report["schema_version"],
            "sdk_result": structure_map(observed, max_depth=4, max_items=2),
            "page_1_pruned_result": structure_map(
                (observed.get("pages") or [{}])[0].get("pruned_result"),
                max_depth=4,
                max_items=2,
            ),
            "page_1_raw": structure_map(
                (observed.get("pages") or [{}])[0].get("raw"),
                max_depth=3,
                max_items=2,
            ),
            "job_envelope_keys": envelope_keys,
        },
    )

    if args.raw_dump:
        raw_path = Path(args.raw_dump)
        if not raw_path.is_absolute():
            raise SystemExit("--raw-dump 必须是绝对路径（完整原始响应不入仓库）")
        write_json(raw_path, raw_payload if raw_payload is not None else [])
        print(f"[probe] 原始响应写入 {raw_path}", file=sys.stderr)

    if args.fixture_out:
        fixture = build_fixture(
            observed,
            max_pages=args.max_fixture_pages,
            text_limit=args.fixture_text_limit,
            box_limit=args.fixture_box_limit,
            source=source,
            envelope=envelope,
        )
        hits = scan_for_secrets(fixture)
        if hits:
            print("[probe] fixture 自检发现疑似敏感内容，已拒绝写入：", file=sys.stderr)
            for hit in hits[:10]:
                print(f"  - {hit}", file=sys.stderr)
            return 2
        write_json(Path(args.fixture_out), fixture)
        print(f"[probe] fixture 写入 {args.fixture_out}", file=sys.stderr)

    summary = {
        key: report[key]
        for key in (
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
            "gap_classification",
            "block_count_page1",
            "page_count",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _sha256_hint(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


if __name__ == "__main__":
    raise SystemExit(main())
