from __future__ import annotations

import re

from .config import DISPLAYSTYLE_COMMANDS, QUESTION_NUMBER_RE, SCORE_RE
from .errors import NormalizationError
from .math_candidate import find_math_candidate_spans
from .math_merger import merge_inline_math
from .math_parser import parse_math_spans
from .models import CheckResult, Diagnostic, Severity
from .protector import protect_markdown
from .scanner import line_col


_DISPLAYSTYLE_DUP_RE = re.compile(r"^\s*(?:\\displaystyle)(?:\s+\\displaystyle)+")


def _add_diagnostic(
    diagnostics: list[Diagnostic],
    *,
    code: str,
    severity: Severity,
    message: str,
    line: int,
    column: int,
    context: str,
) -> None:
    diagnostics.append(
        Diagnostic(
            code=code,
            severity=severity,
            message=message,
            line=line,
            column=column,
            context=context,
        )
    )


def _has_display_command(content: str) -> bool:
    for cmd in DISPLAYSTYLE_COMMANDS:
        if cmd in content:
            return True
    return False


def _ensure_protected_contents(
    text: str,
    protected_snippets: tuple[str, ...],
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    cursor = 0
    for snippet in protected_snippets:
        if not snippet:
            continue
        idx = text.find(snippet, cursor)
        if idx < 0:
            line, column = line_col(text, min(cursor, len(text)))
            _add_diagnostic(
                diagnostics,
                code="ERROR_PROTECTED_CHANGED",
                severity=Severity.ERROR,
                message="protected content changed or lost",
                line=line,
                column=column,
                context=text[max(0, cursor - 20) : cursor + 20],
            )
            continue
        cursor = idx + len(snippet)

    return tuple(diagnostics)


def _is_in_any_math_span(offset: int, math_spans: tuple) -> bool:
    for span in math_spans:
        if span.start <= offset < span.end:
            return True
    return False


def validate_markdown_text(
    text: str,
    *,
    original_protected_snippets: tuple[str, ...] | None = None,
) -> CheckResult:
    diagnostics: list[Diagnostic] = []
    blocked = protect_markdown(text)
    math_spans, parse_diags = parse_math_spans(text, blocked)

    has_error = False
    needs_normalization = False

    if parse_diags:
        for diag in parse_diags:
            _add_diagnostic(
                diagnostics,
                code=diag.code,
                severity=diag.severity,
                message=diag.message,
                line=diag.line,
                column=diag.column,
                context=diag.context,
            )
            has_error = True
        return CheckResult(
            False,
            False,
            tuple(diagnostics),
            conforming=False,
            has_fatal_error=True,
        )

    candidate_spans = find_math_candidate_spans(text, tuple(math_spans), blocked)
    if candidate_spans:
        first = candidate_spans[0]
        line, column = line_col(text, first.start)
        _add_diagnostic(
            diagnostics,
            code="ERROR_UNWRAPPED_MATH",
            severity=Severity.WARNING,
            message="raw math-like text remains outside math delimiters",
            line=line,
            column=column,
            context=text[max(0, first.start - 20) : first.end + 20],
        )
        # 规范违规：文本尚未符合规范，但可以由管线安全修复。
        needs_normalization = True

    merged = merge_inline_math(text)
    if merged != text:
        line, column = line_col(text, 0)
        _add_diagnostic(
            diagnostics,
            code="ERROR_MERGEABLE_INLINE_MATH",
            severity=Severity.WARNING,
            message="inline math can still be merged",
            line=line,
            column=column,
            context=text[:40],
        )
        needs_normalization = True

    for span in (s for s in math_spans if s.kind.name == "INLINE_MATH"):
        content = text[span.start + 1 : span.end - 1]
        if content.lstrip().startswith(r"\displaystyle"):
            if _DISPLAYSTYLE_DUP_RE.match(content):
                has_error = True
                line, column = line_col(text, span.start + 1)
                _add_diagnostic(
                    diagnostics,
                    code="ERROR_DUPLICATE_DISPLAYSTYLE",
                    severity=Severity.ERROR,
                    message="duplicate \\displaystyle in inline math",
                    line=line,
                    column=column,
                    context=text[max(0, span.start - 20) : span.end + 20],
                )
            continue

        if _has_display_command(content):
            line, column = line_col(text, span.start + 1)
            _add_diagnostic(
                diagnostics,
                code="ERROR_MISSING_DISPLAYSTYLE",
                severity=Severity.WARNING,
                message="inline math should include \\displaystyle",
                line=line,
                column=column,
                context=text[max(0, span.start - 20) : span.end + 20],
            )
            needs_normalization = True

    for match in QUESTION_NUMBER_RE.finditer(text):
        start = match.start()
        if _is_in_any_math_span(start, math_spans):
            line, column = line_col(text, start)
            _add_diagnostic(
                diagnostics,
                code="ERROR_QUESTION_NUMBER_MATHIFIED",
                severity=Severity.ERROR,
                message="question number should stay outside math",
                line=line,
                column=column,
                context=text[start : match.end()],
            )
            has_error = True

    for match in SCORE_RE.finditer(text):
        start = match.start()
        if _is_in_any_math_span(start, math_spans):
            line, column = line_col(text, start)
            _add_diagnostic(
                diagnostics,
                code="ERROR_SCORE_MATHIFIED",
                severity=Severity.ERROR,
                message="score value should stay outside math",
                line=line,
                column=column,
                context=text[match.start() : match.end()],
            )
            has_error = True

    if original_protected_snippets:
        diagnostics.extend(_ensure_protected_contents(text, original_protected_snippets))
        if any(d.code == "ERROR_PROTECTED_CHANGED" for d in diagnostics):
            has_error = True

    if has_error:
        return CheckResult(
            False,
            needs_normalization,
            tuple(diagnostics),
            conforming=False,
            has_fatal_error=True,
        )

    # 用管线自身的变换判断“这份文本是否已经是规范形式”，这样 validator、
    # pipeline 与 --check 共用同一套规则。直接调用 ``_normalize_once``（关闭
    # 其中的 validator 调用）避免两个模块相互递归。
    from .pipeline import _normalize_once

    try:
        first = _normalize_once(text, validate=False).text
        second = _normalize_once(first, validate=False).text
        is_fixed_point = first == second == text
    except NormalizationError:
        is_fixed_point = False
    needs_normalization = needs_normalization or not is_fixed_point

    if not is_fixed_point and not diagnostics:
        line, column = line_col(text, 0)
        _add_diagnostic(
            diagnostics,
            code="ERROR_NOT_NORMALIZED",
            severity=Severity.WARNING,
            message="text is not in normalized form",
            line=line,
            column=column,
            context=text[:40],
        )

    return CheckResult(
        is_valid=is_fixed_point,
        needs_normalization=needs_normalization,
        diagnostics=tuple(diagnostics),
        conforming=is_fixed_point,
        has_fatal_error=False,
    )
