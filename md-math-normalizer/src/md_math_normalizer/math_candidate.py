from __future__ import annotations

import re

from .config import (
    ALLOWED_TEXT_PUNCTUATION,
    MARKDOWN_BOUNDARY_CHARS,
    NUMBERING_MARKER_RES,
    QUESTION_NUMBER_RE,
    SCORE_RE,
)
from .models import ProtectedSpan, Span, SpanKind
from .scanner import is_in_spans

# 数值类编号字符：掩码只盖这些字符，不盖圈码/罗马数字之外的标签文字。
_NUMBERING_CHARS = set("0123456789.\u2460-\u2473\u2160-\u217f")

# 正文中允许原样保留、且不参与数学候选的 Markdown 结构字符。
# 注意：`_`、`^`、`{`、`}`、`\` 不在此集合中——它们属于数学表达式本身
# （下标、上标、花括号组、LaTeX 命令），必须留在同一个数学 span 内。
_STRUCTURE_CHARS = set(MARKDOWN_BOUNDARY_CHARS) - {"_"}

# 不作为数学候选内容的标点：保留逗号/冒号/分号（a,b 属于同一数学式），
# 感叹号、问号按原规格属于正文允许标点。
_SKIPPED_PUNCTUATION = set("?!") | (ALLOWED_TEXT_PUNCTUATION - {",", ":", ";", "."})

# 数学候选首尾需要剥离的标点。
_EDGE_PUNCTUATION = set(",.:;!?")


def is_cjk_ideograph(char: str) -> bool:
    if not char:
        return False
    point = ord(char)
    return (
        0x3400 <= point <= 0x4DBF
        or 0x4E00 <= point <= 0x9FFF
        or 0x20000 <= point <= 0x2A6DF
        or 0x2A700 <= point <= 0x2B73F
        or 0x2B740 <= point <= 0x2B81F
        or 0x2B820 <= point <= 0x2CEAF
        or 0x2CEB0 <= point <= 0x2EBEF
        or 0x30000 <= point <= 0x3134F
    )


def _build_no_math_mask(text: str) -> list[bool]:
    """Mark offsets that must never become math candidates.

    Covers the numbering and score exceptions: leading question numbers (``9.``),
    score values (``16分`` / ``（16分）``), and numbering labels such as ``例1``,
    ``（10）``, ``### 1.`` — those numbers are labels, not math. Everything else,
    including numbers that merely look similar (``结果为 10.``), is left for the
    math wrapper.
    """
    mask = [False] * len(text)

    for m in QUESTION_NUMBER_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if text[i].isdigit() or text[i] == ".":
                mask[i] = True

    for m in SCORE_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if text[i].isdigit():
                mask[i] = True

    for pattern in NUMBERING_MARKER_RES:
        for match in pattern.finditer(text):
            for name in ("num", "paren_a", "paren_b", "option"):
                value = match.groupdict().get(name)
                if not value:
                    continue
                for index in range(match.start(name), match.end(name)):
                    if text[index] in _NUMBERING_CHARS or text[index].isascii():
                        mask[index] = True

    return mask


def _is_math_candidate_char(text: str, index: int, mask: list[bool]) -> bool:
    ch = text[index]
    if mask[index]:
        return False
    if ch in "\r\n":
        return False
    if is_cjk_ideograph(ch):
        return False
    if ch == "$":
        # 定界符是强边界：已有数学环境由 math_parser 负责，候选不得吞入。
        return False
    if ch in _STRUCTURE_CHARS:
        return False
    if ch in _SKIPPED_PUNCTUATION:
        return False
    if ch == ".":
        # 只有数字之间的小数点属于数学内容。
        return (
            index > 0
            and index + 1 < len(text)
            and text[index - 1].isdigit()
            and text[index + 1].isdigit()
        )
    if ch.isspace():
        return ch in {" ", "\t", "\u00a0"}
    if not ch.isprintable():
        return False
    return True


def _is_content_candidate(text: str, start: int, end: int) -> bool:
    return any(text[i].isalnum() or text[i] == "\\" for i in range(start, end))


#: 孤立数字字面量：可选正负号 + 数字/千分位 + 可选小数 + 可选百分号。
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?[\d,]+(?:\.\d+)?%?$")


def is_standalone_numeric_literal(segment: str) -> bool:
    """只有数字、没有任何数学结构证据的片段（例如 ``10``、``3.14``、``50%``）。

    这类片段**不再自动**进入数学环境：数学意义上的数字不等于需要数学排版。
    只有与变量、运算符、上下标、LaTeX 命令等明确数学结构共同出现时（例如 ``x=2``、
    ``2^n``、``a_2``、``3m+4``），整个表达式才作为数学候选。
    """

    return bool(_NUMERIC_LITERAL_RE.match(segment.strip()))


def _trim_edges(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    while start < end and text[start] in _EDGE_PUNCTUATION:
        start += 1
    while end > start and text[end - 1] in _EDGE_PUNCTUATION:
        if text[end - 1] == "." and end - 2 >= start and text[end - 2].isdigit():
            break
        end -= 1
    return start, end


def _underscore_blank_run_end(text: str, index: int) -> int:
    if text.startswith(r"\_", index):
        end = index
        while text.startswith(r"\_", end):
            end += 2
        return end if end - index >= 6 else index
    if index < len(text) and text[index] == "_":
        end = index
        while end < len(text) and text[end] == "_":
            end += 1
        return end if end - index >= 3 else index
    return index


def replace_underscore_blanks(
    text: str,
    math_spans: tuple[Span, ...],
    protected_spans: tuple[ProtectedSpan, ...],
) -> str:
    """Replace escaped or literal runs of three or more underscores."""

    protected = tuple(sorted(protected_spans, key=lambda s: (s.start, s.end)))
    math = tuple(sorted(math_spans, key=lambda s: (s.start, s.end)))
    replacement = r"$\underline{\quad\quad\quad}$"
    replacement_content = r"\underline{\quad\quad\quad}"
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        in_math = is_in_spans(cursor, math)
        if is_in_spans(cursor, protected) and not in_math:
            output.append(text[cursor])
            cursor += 1
            continue

        escaped_run_end = cursor
        if text.startswith(r"\_", cursor):
            while (
                text.startswith(r"\_", escaped_run_end)
                and (
                    is_in_spans(escaped_run_end, math)
                    or not is_in_spans(escaped_run_end, protected)
                )
            ):
                escaped_run_end += 2
            if escaped_run_end - cursor >= 6:
                output.append(replacement_content if in_math else replacement)
                cursor = escaped_run_end
                continue

        if text[cursor] == "_":
            literal_run_end = cursor
            while literal_run_end < len(text) and text[literal_run_end] == "_":
                literal_run_end += 1
            if literal_run_end - cursor >= 3:
                output.append(replacement_content if in_math else replacement)
                cursor = literal_run_end
                continue

        output.append(text[cursor])
        cursor += 1

    return "".join(output)


def find_math_candidate_spans(
    text: str,
    math_spans: tuple[Span, ...],
    protected_spans: tuple[ProtectedSpan, ...],
) -> tuple[Span, ...]:
    blocked = sorted((*math_spans, *protected_spans), key=lambda s: (s.start, s.end))

    blocked_spans: list[tuple[int, int]] = []
    for span in blocked:
        blocked_spans.append((span.start, span.end))

    mask = _build_no_math_mask(text)
    ranges: list[tuple[int, int]] = []

    cursor = 0
    for start, end in blocked_spans:
        if cursor < start:
            ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        ranges.append((cursor, len(text)))

    candidate_spans: list[Span] = []
    for start, end in ranges:
        i = start
        while i < end:
            blank_end = _underscore_blank_run_end(text, i)
            if blank_end > i:
                i = blank_end
                continue
            if _is_math_candidate_char(text, i, mask):
                run_start = i
                i += 1
                while i < end and _is_math_candidate_char(text, i, mask):
                    blank_end = _underscore_blank_run_end(text, i)
                    if blank_end > i:
                        break
                    i += 1
                span_start, span_end = _trim_edges(text, run_start, i)
                if (
                    span_end > span_start
                    and _is_content_candidate(text, span_start, span_end)
                    and not is_standalone_numeric_literal(text[span_start:span_end])
                ):
                    candidate_spans.append(Span(span_start, span_end, SpanKind.TEXT))
            else:
                i += 1

    return tuple(candidate_spans)


def wrap_math_candidates(
    text: str,
    math_spans: tuple[Span, ...],
    protected_spans: tuple[ProtectedSpan, ...],
) -> str:
    candidates = find_math_candidate_spans(text, math_spans, protected_spans)
    if not candidates:
        return text

    out: list[str] = []
    cursor = 0
    for span in candidates:
        out.append(text[cursor : span.start])
        candidate_content = text[span.start : span.end]
        blank_index = span.end
        while blank_index < len(text) and text[blank_index] in {" ", "\t"}:
            blank_index += 1
        blank_after = _underscore_blank_run_end(text, blank_index) > blank_index
        if candidate_content.rstrip().endswith("=") and blank_after:
            stripped_candidate = candidate_content.rstrip()
            equals_index = stripped_candidate.rfind("=")
            left_content = stripped_candidate[:equals_index].rstrip()
            operator = stripped_candidate[len(left_content) :]
            if left_content:
                out.append("$" + left_content + "$" + operator)
            else:
                out.append(candidate_content)
        else:
            out.append("$")
            out.append(candidate_content)
            out.append("$")
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)
