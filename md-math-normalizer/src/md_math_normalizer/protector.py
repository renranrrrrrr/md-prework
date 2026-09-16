from __future__ import annotations

import re
from typing import Iterable

from .models import ProtectedKind, ProtectedSpan
from .scanner import is_escaped, is_in_spans


# 更具体的 protected 类型优先保留：HTML 标签内部的 URL、行内代码等
# 不应被外层的通用 RAW_HTML span 吞掉。
_SPECIFIC_KINDS = {
    ProtectedKind.URL,
    ProtectedKind.INLINE_CODE,
    ProtectedKind.MARKDOWN_LINK_DESTINATION,
    ProtectedKind.MARKDOWN_IMAGE,
    ProtectedKind.AUTOLINK,
}

# URL 本体：方括号/尖括号/圆括号/引号都可能包裹或分隔 URL，不能并入 URL。
_URL_RE = re.compile(r"\b(?:https?|ftp)://[^\s\]\[<>()\"']+")


def _sorted_unique_spans(
    text: str,
    spans: Iterable[ProtectedSpan],
) -> tuple[ProtectedSpan, ...]:
    """Sort protected spans and drop those already covered by a wider one.

    Spans keep their own kind and are never relabelled, so a URL inside an HTML
    attribute stays visible as ``URL`` next to the enclosing ``RAW_HTML`` span.
    A span that merely continues a wider one (a URL right after ``<div>``) is
    dropped, because the wider span already covers exactly the same region.
    """
    ordered = sorted(spans, key=lambda s: (s.start, -s.end))
    kept: list[ProtectedSpan] = []
    for span in ordered:
        if kept:
            last = kept[-1]
            covered = last.start <= span.start and span.end <= last.end
            continues = (
                span.kind in _SPECIFIC_KINDS
                and span.start > last.start
                and span.start - 1 < len(text)
                and not text[span.start - 1].isspace()
                and text[span.start - 1] not in "<>\"'"
            )
            if covered and not continues:
                continue
        kept.append(span)
    return tuple(sorted(kept, key=lambda s: (s.start, s.end)))


def _protect_fenced_code(text: str) -> tuple[ProtectedSpan, ...]:
    spans: list[ProtectedSpan] = []
    lines = text.splitlines(True)
    offset = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    start_offset = 0

    for line in lines:
        stripped = line.rstrip("\r\n")
        if not in_fence:
            m = re.match(r"^[ \t]{0,3}([`~]+)", stripped)
            if m and len(m.group(1)) >= 3:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                in_fence = True
                start_offset = offset
        else:
            pattern = rf"^[ \t]{{0,3}}({re.escape(fence_char)}{{{fence_len},}})(?:[ \t]*)$"
            m = re.match(pattern, stripped)
            if m:
                spans.append(ProtectedSpan(start_offset, offset + len(line), ProtectedKind.FENCED_CODE))
                in_fence = False
                fence_char = ""
                fence_len = 0
        offset += len(line)

    if in_fence:
        spans.append(ProtectedSpan(start_offset, len(text), ProtectedKind.FENCED_CODE))

    return tuple(spans)


def _protect_inline_code(text: str, existing: tuple[ProtectedSpan, ...]) -> tuple[ProtectedSpan, ...]:
    spans: list[ProtectedSpan] = []
    i = 0
    n = len(text)
    while i < n:
        if (
            not is_escaped(text, i)
            and text[i] == "`"
            and not is_in_spans(i, existing)
            and not is_in_spans(i, tuple(spans))
        ):
            j = i + 1
            while j < n:
                if text[j] == "`" and not is_escaped(text, j):
                    spans.append(ProtectedSpan(i, j + 1, ProtectedKind.INLINE_CODE))
                    i = j + 1
                    break
                j += 1
            else:
                break
        else:
            i += 1
    return tuple(spans)


def _protect_html(text: str, existing: tuple[ProtectedSpan, ...]) -> tuple[ProtectedSpan, ...]:
    spans: list[ProtectedSpan] = []
    html_pattern = re.compile(
        r"<!--.*?-->|"
        r"</?[A-Za-z][A-Za-z0-9-]*"
        r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
        r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*"
        r"\s*/?>",
        flags=re.DOTALL,
    )
    for match in html_pattern.finditer(text):
        if is_in_spans(match.start(), existing):
            continue
        # 整段标签登记为 RAW_HTML；若其中含 URL，URL span 会另行登记，
        # 合并时按 URL 报告（见 ``_sorted_unique_spans``）。
        spans.append(ProtectedSpan(match.start(), match.end(), ProtectedKind.RAW_HTML))
    return tuple(spans)


def _protect_urls(text: str, existing: tuple[ProtectedSpan, ...]) -> tuple[ProtectedSpan, ...]:
    spans: list[ProtectedSpan] = []
    for match in _URL_RE.finditer(text):
        # URL 是最具体的 kind：即使它位于 HTML 标签内部或属性值中，也要单独登记，
        # 这样 URL 内容既受保护，又能在 protected span 列表中被识别出来。
        spans.append(ProtectedSpan(match.start(), match.end(), ProtectedKind.URL))
    return tuple(spans)


def _protect_markdown_links(text: str, existing: tuple[ProtectedSpan, ...]) -> tuple[ProtectedSpan, ...]:
    spans: list[ProtectedSpan] = []
    link_pattern = re.compile(r"\[[^\]]*?\]\(([^)\s]+)(?:\s+\"[^\"]+\")?\)")
    img_pattern = re.compile(r"!\[[^\]]*?\]\(([^)\s]+)(?:\s+\"[^\"]+\")?\)")
    for match in link_pattern.finditer(text):
        if is_in_spans(match.start(), existing):
            continue
        spans.append(ProtectedSpan(match.start(), match.end(), ProtectedKind.MARKDOWN_LINK_DESTINATION))
    for match in img_pattern.finditer(text):
        if is_in_spans(match.start(), existing):
            continue
        spans.append(ProtectedSpan(match.start(), match.end(), ProtectedKind.MARKDOWN_IMAGE))
    return tuple(spans)


def _protect_autolinks(text: str, existing: tuple[ProtectedSpan, ...]) -> tuple[ProtectedSpan, ...]:
    spans: list[ProtectedSpan] = []
    pattern = re.compile(r"<(?:https?|ftp)://[^>\s]+>")
    for match in pattern.finditer(text):
        if is_in_spans(match.start(), existing):
            continue
        spans.append(ProtectedSpan(match.start(), match.end(), ProtectedKind.AUTOLINK))
    return tuple(spans)


def _protect_escaped_sequences(text: str, existing: tuple[ProtectedSpan, ...]) -> tuple[ProtectedSpan, ...]:
    spans: list[ProtectedSpan] = []
    # CommonMark only treats a backslash followed by ASCII punctuation as an
    # escape. LaTeX commands such as \\odot must remain available to the math
    # candidate scanner and must not be split into \\o + dot.
    for match in re.finditer(r"\\[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]", text):
        if is_in_spans(match.start(), existing):
            continue
        if match.group() == r"\_":
            left = match.start()
            while left >= 2 and text[left - 2 : left] == r"\_":
                left -= 2
            right = match.start()
            while text.startswith(r"\_", right):
                right += 2
            if (right - left) // 2 >= 3:
                continue
        spans.append(ProtectedSpan(match.start(), match.end(), ProtectedKind.ESCAPED_SEQUENCE))
    return tuple(spans)


def protect_markdown(text: str) -> tuple[ProtectedSpan, ...]:
    spans: tuple[ProtectedSpan, ...] = ()
    spans += _protect_fenced_code(text)
    spans += _protect_inline_code(text, spans)
    spans += _protect_html(text, spans)
    spans += _protect_urls(text, spans)
    spans += _protect_markdown_links(text, spans)
    spans += _protect_autolinks(text, spans)
    spans += _protect_escaped_sequences(text, spans)
    return _sorted_unique_spans(text, spans)


def protect(text: str) -> tuple[ProtectedSpan, ...]:
    return protect_markdown(text)
