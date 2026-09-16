"""run_pipeline —— 一次跑完全流程：PDF/Markdown → 规范化 → 可疑行标记。

支持把文件或文件夹**直接拖到** `运行全流程.cmd` 上运行。会对每个输入：

    ① PDF  →（pdf2md）→ 同目录同名 .md
    ② .md  → 跳过 OCR
    ③ 规范化 → <同名>.规范化.md（原文件不动）
    ④ 复检规范化结果，确认是固定点
    ⑤ 可疑行标记（只报告，绝不改文件）

输入可以是任意多个，文件与文件夹可混用：

    run_pipeline.py "D:\\a\\1.pdf" "D:\\b\\2.md" "D:\\试卷"
    参数是文件夹时只取该目录下的 .pdf/.md，**不进子目录**。

退出码：0 全部成功；1 有失败项；2 输入或环境有问题；130 手动中断。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

# ---------------------------------------------------------------- 环境探测

TOOL_DIR = pathlib.Path(__file__).resolve().parent
NORMALIZER_ENV = "MD_MATH_NORMALIZER"

# 两个项目是 Tools/ 下的同级目录，默认按相对位置解析：
#     <Tools>/paddleocr-vl-md/   ← 本工具
#     <Tools>/md-math-normalizer/ ← 规范化器
# 因此不再硬编码绝对路径；仍可用 MD_MATH_NORMALIZER 环境变量覆盖。
TOOLS_DIR = TOOL_DIR.parent
NORMALIZER_SIBLING = TOOLS_DIR / "md-math-normalizer"


def _force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def find_tool_python() -> pathlib.Path:
    """pdf2md / ocr_mark 跑在工具自己的虚拟环境里。"""
    candidate = TOOL_DIR / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return candidate
    candidate = TOOL_DIR / ".venv" / "bin" / "python"
    if candidate.exists():
        return candidate
    # 退回当前解释器（已经装好依赖时可用）
    return pathlib.Path(sys.executable)


def find_normalizer() -> list[str]:
    """返回调用规范化器的**命令前缀**（可执行文件 + 前置参数）。

    解析顺序：
      1. 环境变量 MD_MATH_NORMALIZER 指向的项目目录
      2. Tools/ 下的同级目录（默认部署形态）
      3. PATH 中的 md-math-normalizer

    优先使用 `<venv>/python -m md_math_normalizer`：pip 生成的 .exe 启动器在
    被 subprocess 以管道方式调用时可能静默失败（rc=1、无输出），而 `-m` 形式
    稳定且能正常回传诊断。.exe 仅作为最后手段。
    """
    override = os.environ.get(NORMALIZER_ENV)
    roots = []
    if override:
        roots.append(pathlib.Path(override))
    roots.append(NORMALIZER_SIBLING)

    for root in roots:
        for sub in (".venv/Scripts", ".venv/bin"):
            folder = root / sub
            python = folder / ("python.exe" if os.name == "nt" else "python")
            if python.exists() and (root / "src" / "md_math_normalizer").is_dir():
                return [str(python), "-m", "md_math_normalizer"]
            for name in ("md-math-normalizer.exe", "md-math-normalizer"):
                candidate = folder / name
                if candidate.exists():
                    return [str(candidate)]

    found = shutil.which("md-math-normalizer")
    if found:
        return [found]
    raise SystemExit(
        "找不到 md-math-normalizer。请把它放在与本工具同级的目录下"
        f"（当前期望：{NORMALIZER_SIBLING}），"
        f"或设置环境变量 {NORMALIZER_ENV}=<规范化器项目目录>。"
    )


# ---------------------------------------------------------------- 输入展开

TARGET_SUFFIXES = {".pdf", ".md"}


def collect_inputs(raw_inputs: list[str]) -> tuple[list[pathlib.Path], list[str]]:
    """展开成待处理的 PDF / Markdown 列表；目录只取直接子级。"""
    items: list[pathlib.Path] = []
    problems: list[str] = []
    seen: set[str] = set()

    def _add(path: pathlib.Path) -> None:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            items.append(path)

    for raw in raw_inputs:
        path = pathlib.Path(raw).expanduser()
        if not path.exists():
            problems.append(f"路径不存在：{path}")
            continue
        if path.is_dir():
            found = sorted(
                entry
                for entry in path.iterdir()
                if entry.is_file()
                and entry.suffix.lower() in TARGET_SUFFIXES
                # 规范化产物不再作为输入，避免自我循环
                and not entry.stem.endswith(".规范化")
            )
            if not found:
                problems.append(f"目录下没有 .pdf/.md（已忽略子目录）：{path}")
                continue
            for entry in found:
                _add(entry)
            continue
        if path.suffix.lower() not in TARGET_SUFFIXES:
            problems.append(f"后缀不支持（只处理 .pdf/.md），已跳过：{path}")
            continue
        _add(path)

    return items, problems


# ---------------------------------------------------------------- 执行封装

def run(command: list[str]) -> tuple[int, str]:
    """执行子进程，返回 (退出码, 合并输出)。"""
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.rstrip()


def indent(text: str, prefix: str = "      ", limit: int = 0) -> int:
    """缩进打印输出；limit>0 时只打印前 limit 行，返回被省略的行数。"""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    shown = lines if limit <= 0 else lines[:limit]
    # 诊断行的续行（以空格开头）跟着上一条，不重复加前缀。
    for line in shown:
        stripped = line.strip()
        if line.startswith(" ") and not stripped.startswith(("line ", "文件", "可疑行", "合计")):
            print(prefix + "  " + stripped)
        else:
            print(prefix + stripped)
    omitted = len(lines) - len(shown)
    if omitted > 0:
        print(f"{prefix}…（另有 {omitted} 行，加 --verbose 查看）")
    return omitted


def kind_label(returncode: int) -> str:
    return {0: "已规范", 1: "可修复", 2: "需人工"}.get(returncode, f"退出码 {returncode}")


# ---------------------------------------------------------------- 主流程

def process(
    source: pathlib.Path,
    *,
    tool_python: pathlib.Path,
    normalizer: list[str],
    overwrite: bool,
    keep_images: bool,
    verbose: bool,
    limit: int,
) -> dict[str, object]:
    """处理单个输入，返回结果摘要。"""
    result: dict[str, object] = {
        "source": source,
        "markdown": None,
        "normalized": None,
        "suspicious": None,
        "diagnostics": 0,
        "failed": False,
    }
    show = 0 if verbose else limit

    # ① OCR（仅 PDF）
    if source.suffix.lower() == ".pdf":
        command = [
            str(tool_python),
            str(TOOL_DIR / "pdf2md.py"),
            str(source),
            "--retries",
            "4",
            "--retry-delay",
            "20",
        ]
        if overwrite:
            command.append("--overwrite")
        if keep_images:
            command.append("--keep-images")
        print("[1/4] OCR")
        code, output = run(command)
        indent(output, limit=show)
        md_path = source.with_suffix(".md")
        if code != 0 or not md_path.exists():
            result["failed"] = True
            return result
        markdown = md_path
    else:
        markdown = source
        print("[1/4] OCR —— 跳过（输入已是 Markdown）")

    result["markdown"] = markdown

    # ② 体检
    print("[2/4] 规范化器体检")
    code, output = run([*normalizer, str(markdown), "--check"])
    omitted = indent(output, limit=show)
    result["diagnostics"] = len(
        [line for line in output.splitlines() if line.strip()]
    )
    print(f"      -> {kind_label(code)}")
    if code == 2:
        result["failed"] = True
        return result

    # ③ 规范化（输出到新文件，原文件不动）
    target = markdown.with_name(f"{markdown.stem}.规范化{markdown.suffix}")
    print("[3/4] 规范化")
    code, output = run([*normalizer, str(markdown), "-o", str(target)])
    indent(output, limit=show)
    if code != 0 or not target.exists():
        result["failed"] = True
        return result
    result["normalized"] = target

    # ④ 复检规范化结果（应为 0，确认是固定点）
    print("[4/4] 复检规范化结果")
    code, output = run([*normalizer, str(target), "--check"])
    indent(output, limit=show)
    print(f"      -> 复检 {kind_label(code)}")
    if code != 0:
        result["failed"] = True

    # ⑤ 可疑行标记（只报告）
    print("[标记] 可疑行扫描（不修改文件）")
    code, output = run(
        [str(tool_python), str(TOOL_DIR / "ocr_mark.py"), str(markdown)]
    )
    indent(output, limit=show)
    result["suspicious"] = _extract_suspicious(output)

    return result


def _extract_suspicious(output: str) -> int | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("可疑行："):
            digits = "".join(char for char in stripped if char.isdigit())
            if digits:
                return int(digits)
    return None


def main() -> int:
    _force_utf8_console()

    parser = argparse.ArgumentParser(
        prog="run_pipeline",
        description="一次性跑完 OCR → 规范化 → 可疑行标记。可把文件/文件夹拖到 运行全流程.cmd 上。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  run_pipeline.py "D:\\试卷\\1.pdf" "D:\\试卷\\2.md"\n'
            '  run_pipeline.py "D:\\试卷"\n'
        ),
    )
    parser.add_argument("inputs", nargs="+", help="任意多个 .pdf / .md 文件，或文件夹")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的 .md / .规范化.md")
    parser.add_argument("--keep-images", action="store_true", help="OCR 时导出文档内图片")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印全部诊断（默认每个阶段只打印前几行）",
    )
    parser.add_argument(
        "--diagnostics-limit",
        type=int,
        default=5,
        help="每个阶段最多打印几行诊断（默认 5）",
    )
    parser.add_argument("--no-pause", action="store_true", help="结束后不等待按键（脚本化调用用）")
    args = parser.parse_args()

    tool_python = find_tool_python()
    normalizer = find_normalizer()
    print(f"解释器  ：{tool_python}")
    print(f"规范化器：{' '.join(normalizer)}")

    items, problems = collect_inputs(args.inputs)
    for problem in problems:
        print(f"[输入] {problem}", file=sys.stderr)
    if not items:
        print("没有可处理的输入。", file=sys.stderr)
        return 2

    print(f"\n共 {len(items)} 个输入\n" + "=" * 60)

    results: list[dict[str, object]] = []
    try:
        for index, source in enumerate(items, start=1):
            print(f"\n[{index}/{len(items)}] {source}")
            print("-" * 60)
            results.append(
                process(
                    source,
                    tool_python=tool_python,
                    normalizer=normalizer,
                    overwrite=args.overwrite,
                    keep_images=args.keep_images,
                    verbose=args.verbose,
                    limit=args.diagnostics_limit,
                )
            )
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130

    print("\n" + "=" * 60)
    print("汇总")
    ok = 0
    for item in results:
        source = item["source"]
        if item["failed"]:
            print(f"  [失败] {source.name}")
            continue
        ok += 1
        suspicious = item["suspicious"]
        note = f"，可疑行 {suspicious}" if suspicious else "，无可疑行"
        print(f"  [完成] {source.name} -> {pathlib.Path(str(item['normalized'])).name}{note}")

    print(f"\n成功 {ok}/{len(results)}；工具未修改任何原始文件。")

    if not args.no_pause and sys.stdout.isatty():
        try:
            input("\n按回车键关闭……")
        except EOFError:
            pass

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
