"""Add ``\\displaystyle`` to inline math that contains large operators.

This stage only decides whether a leading ``\\displaystyle`` must be inserted.
It never reflows, re-spaces or reformats existing math environments: boundary
layout for *newly created* math environments belongs to the wrapping stage.
"""

from __future__ import annotations

from .config import DISPLAYSTYLE_COMMANDS
from .models import SpanKind
from .protector import protect_markdown
from .math_parser import parse_math_spans

DISPLAYSTYLE_PREFIX = r"\displaystyle"


def _has_display_command(content: str) -> bool:
    return any(cmd in content for cmd in DISPLAYSTYLE_COMMANDS)


def apply_displaystyle(text: str) -> str:
    protected = protect_markdown(text)
    spans, _ = parse_math_spans(text, protected)
    inline_spans = [s for s in spans if s.kind == SpanKind.INLINE_MATH]
    if not inline_spans:
        return text

    out: list[str] = []
    cursor = 0
    for span in inline_spans:
        out.append(text[cursor : span.start + 1])
        content = text[span.start + 1 : span.end - 1]
        if content.lstrip().startswith(DISPLAYSTYLE_PREFIX):
            out.append(content)
        elif _has_display_command(content):
            # \displaystyle 加在环境内容开头（规格 §72）。前置标记是唯一的
            # 改动，其余内容（包括尾随空白）逐字保留。
            suffix = content[len(content) - len(content.lstrip()) :]
            out.append(DISPLAYSTYLE_PREFIX + " " + suffix)
        else:
            out.append(content)
        out.append("$")
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)
