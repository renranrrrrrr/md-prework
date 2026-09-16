from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .api import check_markdown_file, normalize_markdown_file
from .errors import InvalidFileTypeError, NormalizationError


def _print_diagnostics(diags):
    for d in diags:
        print(f"{d.code} [{d.severity.value}] {d.message} ({d.line}:{d.column})")


def _revision_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}改版{source.suffix}")


def _split_file_names(raw: str, folder: Path) -> list[str]:
    cleaned = raw.strip().strip('"')
    direct = Path(cleaned)
    if direct.is_file() or (folder / direct).is_file():
        return [cleaned]

    normalized = cleaned.replace("；", ";")
    if ";" in normalized:
        parts = normalized.split(";")
    else:
        parts = normalized.replace("，", ",").split(",")
    return [part.strip().strip('"') for part in parts if part.strip()]


def _resolve_interactive_files(folder: Path, raw_names: str) -> list[Path]:
    names = _split_file_names(raw_names, folder)
    if len(names) == 1 and names[0].lower() in {"all", "全部", "*"}:
        return sorted(
            path
            for path in folder.glob("*.md")
            if not path.stem.endswith("改版")
        )

    files: list[Path] = []
    for name in names:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = folder / candidate
        files.append(candidate)
    return files


def _interactive() -> int:
    print("中文数学 Markdown 规范化工具")
    print()
    folder_text = input("请输入文件地址：").strip().strip('"')
    folder = Path(folder_text)
    if not folder.is_dir():
        print(f"错误：文件夹不存在：{folder}", file=sys.stderr)
        return 2

    print("请选择操作:")
    print("1. 规范化并生成改版文件")
    print("2. 仅检查文件")
    operation = input("> ").strip().lower()
    if operation in {"1", "规范化", "转换"}:
        normalize = True
    elif operation in {"2", "检查"}:
        normalize = False
    else:
        print("错误：请输入 1 或 2。", file=sys.stderr)
        return 2

    raw_names = input(
        "请输入待转录文件名称（多个文件用分号分隔，输入 all 处理全部 .md）："
    ).strip()
    files = _resolve_interactive_files(folder, raw_names)
    if not files:
        print("错误：没有找到待处理的 .md 文件。", file=sys.stderr)
        return 2

    exit_code = 0
    for source in files:
        if not source.is_file():
            print(f"文件不存在，已跳过：{source}", file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue
        try:
            if normalize:
                output = _revision_path(source)
                normalize_markdown_file(source, output)
                print(f"已生成：{output}")
                continue

            result = check_markdown_file(source)
            print(f"\n文件：{source}")
            _print_diagnostics(result.diagnostics)
            if result.has_fatal_error:
                print("结果：检查未通过")
                exit_code = max(exit_code, 2)
            elif result.conforming:
                print("结果：检查通过")
            else:
                print("结果：需要规范化")
                exit_code = max(exit_code, 1)
        except (InvalidFileTypeError, NormalizationError, OSError) as exc:
            print(f"处理失败：{source}\n{exc}", file=sys.stderr)
            exit_code = max(exit_code, 2)

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize Chinese math markdown deterministically.")
    parser.add_argument("input_path", nargs="?")
    parser.add_argument("-o", "--output", dest="output")
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.input_path is None:
        if args.output or args.in_place or args.check:
            parser.error("使用 --check、--output 或 --in-place 时必须提供 input_path")
        return _interactive()

    if args.check and (args.output or args.in_place):
        print(
            "error: --check cannot be used with --output or --in-place",
            file=sys.stderr,
        )
        return 2
    if args.output and args.in_place:
        print(
            "error: --output and --in-place cannot be used together",
            file=sys.stderr,
        )
        return 2
    if not args.check and not args.in_place and not args.output:
        print("error: either --output or --in-place must be specified", file=sys.stderr)
        return 2

    try:
        if args.check:
            result = check_markdown_file(args.input_path)
            _print_diagnostics(result.diagnostics)
            if result.has_fatal_error:
                return 2
            if result.conforming:
                return 0
            return 1

        if args.in_place:
            normalize_markdown_file(args.input_path, args.input_path)
            return 0
        if args.output:
            normalize_markdown_file(args.input_path, args.output)
            return 0
    except (InvalidFileTypeError, NormalizationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
