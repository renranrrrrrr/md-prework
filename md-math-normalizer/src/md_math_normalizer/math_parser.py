from __future__ import annotations

from .models import Diagnostic, ProtectedSpan, Span, SpanKind, Severity
from .scanner import is_escaped, is_in_spans, line_col


def _has_embedded_dollar(content: str) -> bool:
    for index, char in enumerate(content):
        if char == "$" and not is_escaped(content, index):
            return True
    return False


def parse_math_spans(
    text: str,
    protected_spans: tuple[ProtectedSpan, ...] = (),
) -> tuple[tuple[Span, ...], tuple[Diagnostic, ...]]:
    spans: list[Span] = []
    diagnostics: list[Diagnostic] = []
    n = len(text)
    i = 0

    def add_error(code: str, message: str, offset: int) -> None:
        line, col = line_col(text, offset)
        context_start = max(0, offset - 20)
        context_end = min(n, offset + 20)
        diagnostics.append(
            Diagnostic(
                code=code,
                severity=Severity.ERROR,
                message=message,
                line=line,
                column=col,
                context=text[context_start:context_end],
            )
        )

    while i < n:
        if text[i] != "$" or is_escaped(text, i):
            i += 1
            continue
        if is_in_spans(i, protected_spans):
            i += 1
            continue

        if i + 1 < n and text[i + 1] == "$":
            j = i + 2
            while j + 1 < n:
                if text[j] == "$" and text[j + 1] == "$" and not is_escaped(text, j):
                    content = text[i + 2 : j]
                    if _has_embedded_dollar(content):
                        add_error(
                            "ERROR_NESTED_MATH_ENVIRONMENT",
                            "nested math in display math",
                            i,
                        )
                        return (), tuple(diagnostics)
                    spans.append(Span(i, j + 2, SpanKind.DISPLAY_MATH))
                    i = j + 2
                    break
                j += 1
            else:
                add_error("ERROR_UNBALANCED_DISPLAY_MATH", "unclosed display math", i)
                i += 1
        else:
            j = i + 1
            while j < n:
                if text[j] == "$" and not is_escaped(text, j):
                    if is_in_spans(j, protected_spans):
                        j += 1
                        continue
                    content = text[i + 1 : j]
                    if _has_embedded_dollar(content):
                        add_error(
                            "ERROR_NESTED_MATH_ENVIRONMENT",
                            "nested math in inline math",
                            i,
                        )
                        return (), tuple(diagnostics)
                    spans.append(Span(i, j + 1, SpanKind.INLINE_MATH))
                    i = j + 1
                    break
                j += 1
            else:
                add_error("ERROR_UNBALANCED_INLINE_MATH", "unclosed inline math", i)
                i += 1

    return tuple(spans), tuple(diagnostics)
