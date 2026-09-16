"""ocr_mark —— 扫描 OCR 产出的 Markdown，只**标记**可疑行，绝不修改文件。

为什么只标记：判断"这一行是页眉/页脚残留还是正文"需要版面上下文，纯本地规则
无法可靠区分，误删正文的代价远大于留下噪音。因此本工具只读文件、输出报告，
由人工确认后自行处理。

检测的规则（全部为纯本地字符串判断，不使用模型、不联网）：

  R1 页眉/页脚形态：整行是 ``___ 1 书名``、``1 书名`` 这类页码 + 书名组合，
     且行内不含 ``$``。
  R2 孤立标题层级异常：标题里出现 CJK 之间的空格，例如 ``## 三 角函数``。
  R3 标题内含正文语气：``##``/``###`` 标题却以"分析/解/证明/评注"等解题词开头。
  R4 疑似页眉图：markdown 引用了名字含 ``header`` 的图片。
  R5 空标题：``#`` 后面没有内容。

用法：
    python ocr_mark.py "D:\\...\\三角函数_1-20.md"
    python ocr_mark.py "D:\\目录"            # 扫描目录下所有 .md（不含子目录）
    python ocr_mark.py "a.md" --json         # 输出 JSON，便于后续处理

退出码：0 = 没有可疑行；1 = 有可疑行（需要人工确认）；2 = 输入有问题。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass

# 解题语气词：出现在标题里通常说明这一行其实是正文。
SOLUTION_WORDS = (
    "分析",
    "解",
    "证明",
    "评注",
    "解答",
    "解析",
    "思路",
    "说明",
)

HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s*(?P<text>.*)$")

# R1：页码 + 书名，前后可能有下划线或空格装饰。
HEADER_LINE_RE = re.compile(
    r"^[_\s\-—]*\d{1,4}[_\s\-—]*[\u4e00-\u9fff][\u4e00-\u9fff\w\s]{0,30}$"
)

# R2：CJK 字符之间夹了空格（正常中文标题不会这样）。
CJK_SPACE_CJK_RE = re.compile(r"[\u4e00-\u9fff]\s+[\u4e00-\u9fff]")

# R4：图片名里含 header。
HEADER_IMAGE_RE = re.compile(r"<img[^>]*?src=\"([^\"]*header[^\"]*)\"", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str
    reason: str
    content: str

    def as_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "rule": self.rule,
            "reason": self.reason,
            "content": self.content,
        }


def scan_text(text: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.split("\n")

    # 书名候选：取最长的纯中文标题行，用于 R1 的比对。
    headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    book_titles = {h for h in headings if h and not CJK_SPACE_CJK_RE.search(h)}

    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        # R4：页眉图。
        for match in HEADER_IMAGE_RE.finditer(raw_line):
            findings.append(
                Finding(
                    number,
                    "R4",
                    f"引用了疑似页眉图：{match.group(1)}",
                    line[:120],
                )
            )

        # R1：页码 + 书名形态（行内不能有公式）。
        if "$" not in line and HEADER_LINE_RE.match(line):
            if any(line.endswith(title) or title in line for title in book_titles):
                findings.append(
                    Finding(number, "R1", "整行像页码 + 书名，疑似页眉/页脚串入正文", line[:120])
                )
                continue

        heading = HEADING_RE.match(raw_line)
        if not heading:
            continue

        title = heading.group("text").strip()
        hashes = heading.group("hashes")

        # R5：空标题。
        if not title:
            findings.append(Finding(number, "R5", f"{hashes} 后面没有内容", raw_line[:120]))
            continue

        # R2：标题里 CJK 之间出现空格。
        if CJK_SPACE_CJK_RE.search(title):
            findings.append(
                Finding(number, "R2", "标题里中文之间夹了空格，疑似页眉残片", title[:120])
            )

        # R3：标题含解题语气词。
        for word in SOLUTION_WORDS:
            if title.startswith(word):
                findings.append(
                    Finding(
                        number,
                        "R3",
                        f"标题以解题语气词「{word}」开头，疑似正文被误当成标题",
                        title[:120],
                    )
                )
                break

    return findings


def collect_markdown(raw_inputs: list[str]) -> tuple[list[pathlib.Path], list[str]]:
    files: list[pathlib.Path] = []
    problems: list[str] = []
    seen: set[str] = set()

    for raw in raw_inputs:
        path = pathlib.Path(raw).expanduser()
        if not path.exists():
            problems.append(f"路径不存在：{path}")
            continue
        if path.is_dir():
            found = sorted(
                item
                for item in path.iterdir()
                if item.is_file() and item.suffix.lower() == ".md"
            )
            if not found:
                problems.append(f"目录下没有 .md（已忽略子目录）：{path}")
                continue
            for item in found:
                key = str(item.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    files.append(item)
            continue
        if path.suffix.lower() != ".md":
            problems.append(f"不是 .md 文件，已跳过：{path}")
            continue
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            files.append(path)

    return files, problems


def report(path: pathlib.Path, findings: list[Finding]) -> None:
    print(f"\n文件：{path}")
    if not findings:
        print("  可疑行：0")
        return
    print(f"  可疑行：{len(findings)}（仅标记，未做任何修改）")
    for finding in findings:
        print(f"  line {finding.line} [{finding.rule}] {finding.reason}")
        print(f"      内容：{finding.content}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="ocr_mark",
        description="只标记 OCR 产出 Markdown 里的可疑行（页眉残片、标题异常等），不修改文件。",
        epilog=(
            "示例：\n"
            '  ocr_mark "D:\\...\\三角函数_1-20.md"\n'
            '  ocr_mark "D:\\math_olympic" --json\n'
        ),
    )
    parser.add_argument("inputs", nargs="+", help="一个 .md 文件，或一个目录（只取该目录下的 .md）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    files, problems = collect_markdown(args.inputs)
    for problem in problems:
        print(f"[输入] {problem}", file=sys.stderr)
    if not files:
        print("没有可扫描的 .md 文件。", file=sys.stderr)
        return 2

    total = 0
    payload: list[dict[str, object]] = []
    for path in files:
        text = path.read_bytes().decode("utf-8-sig")
        findings = scan_text(text)
        total += len(findings)
        if args.json:
            payload.append({"file": str(path), "findings": [f.as_dict() for f in findings]})
        else:
            report(path, findings)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"\n合计可疑行：{total}（工具未修改任何文件）")

    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
