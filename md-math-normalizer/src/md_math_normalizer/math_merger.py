"""Merge adjacent inline math environments.

A gap between two inline math environments may be merged when it is empty, or
holds only whitespace, operators, and in-expression punctuation such as ``,`` or
``;`` — ``$a$ = $b$`` becomes ``$a = b$`` and ``$a$,$b$`` becomes ``$a,b$``. The
gap content is kept verbatim, so this stage never reformats the expression.

Sentence punctuation blocks merging. Once characters are normalized a period is
followed by a space (``$ (k \\in Z) $.`` becomes ``$ (k \\in Z) . ``), so merging
across it would pull the period into the environment and then swallow the prose
that follows. Runs separated only by spaces were already handled by the wrapping
stage, which owns boundary layout for new environments.
"""

from __future__ import annotations

import unicodedata

from .config import MARKDOWN_BOUNDARY_CHARS
from .models import Span, SpanKind
from .protector import protect_markdown
from .math_parser import parse_math_spans
from .scanner import is_in_spans
from .math_candidate import is_cjk_ideograph

# 这些标点同时是 Markdown 结构字符，出现在间隙中时阻止合并。
_BLOCKING_STRUCTURE = set(MARKDOWN_BOUNDARY_CHARS)

# 数学表达式中常见的运算符/分隔符（Unicode 类别为 Sm/Sk，不属于标点）。
_MERGEABLE_OPERATORS = set("+-=<>^/|")

# 句末标点：出现即说明两侧是两个句子，不能当作数学分隔符合并。
_SENTENCE_PUNCTUATION = set(".!?")


def _is_merge_blocking_char(char: str) -> bool:
    if char in "\r\n":
        # 换行是硬边界，任何情况都不合并。
        return True
    if char in _SENTENCE_PUNCTUATION:
        return True
    if is_cjk_ideograph(char):
        return True
    if char in _BLOCKING_STRUCTURE:
        return True
    if char.isspace() or char in _MERGEABLE_OPERATORS:
        return False
    # 反斜杠可能开启转义序列：是否 protected 由 _gap_is_mergeable 判定，
    # 但裸反斜杠本身不允许被吞进数学环境。
    if char == "\\":
        return True
    # 其余情况只允许 Unicode 标点（逗号、分号、冒号等）。
    return not unicodedata.category(char).startswith("P")


def _gap_is_mergeable(text: str, protected, left: Span, right: Span) -> bool:
    gap = text[left.end : right.start]
    for offset, char in enumerate(gap):
        if _is_merge_blocking_char(char):
            return False
        if is_in_spans(left.end + offset, protected):
            # 转义序列、链接目标等 protected 内容必须保持独立。
            return False
    return True


def _merge_once(text: str) -> tuple[str, bool]:
    protected = protect_markdown(text)
    spans, parse_diags = parse_math_spans(text, protected)
    if parse_diags:
        return text, False
    inline = [s for s in spans if s.kind == SpanKind.INLINE_MATH]
    if len(inline) < 2:
        return text, False

    changed = False
    out: list[str] = []
    cursor = 0
    i = 0
    while i < len(inline):
        group = [inline[i]]
        j = i + 1
        while j < len(inline):
            if _gap_is_mergeable(text, protected, group[-1], inline[j]):
                group.append(inline[j])
                j += 1
            else:
                break

        if len(group) > 1:
            changed = True
            out.append(text[cursor : group[0].start])
            merged_content: list[str] = [text[group[0].start + 1 : group[0].end - 1]]
            for idx in range(1, len(group)):
                merged_content.append(text[group[idx - 1].end : group[idx].start])
                merged_content.append(text[group[idx].start + 1 : group[idx].end - 1])
            out.append("$" + "".join(merged_content) + "$")
            cursor = group[-1].end
        i = j

    out.append(text[cursor:])
    return "".join(out), changed


def merge_inline_math(text: str) -> str:
    current = text
    while True:
        merged, changed = _merge_once(current)
        if not changed:
            return current
        current = merged
