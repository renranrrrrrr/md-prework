from __future__ import annotations

from .char_normalizer import normalize_text
from .errors import ValidationError
from .models import PipelineResult
from .protector import protect_markdown
from .math_parser import parse_math_spans
from .math_candidate import replace_underscore_blanks, wrap_math_candidates
from .math_merger import merge_inline_math
from .displaystyle import apply_displaystyle
from .validator import validate_markdown_text


def _format_diagnostics(code: str, diagnostics) -> str:
    if diagnostics:
        return "\n".join(
            f"{d.code}: {d.message} at line {d.line}:{d.column}"
            for d in diagnostics
        )
    return f"{code}: pipeline invariant failed"


def _top_level_protected_spans(spans):
    """Drop protected spans already contained in a wider one.

    Nested spans (a URL inside a Markdown link, a URL inside an HTML tag) would
    otherwise be searched for sequentially and reported as lost. Verifying only
    the outermost regions covers the same text exactly once.
    """
    ordered = sorted(spans, key=lambda s: (s.start, -s.end))
    kept = []
    for span in ordered:
        if kept and span.start >= kept[-1].start and span.end <= kept[-1].end:
            continue
        kept.append(span)
    return tuple(kept)


def _prefer_unbalanced(diagnostics):
    """Report unclosed delimiters ahead of the nesting errors they can trigger.

    An unclosed ``$$`` block makes a later ``$`` look like a misplaced closing
    delimiter, so the nesting diagnostic is a side effect. The root cause is
    more useful to the user.
    """
    unbalanced = tuple(d for d in diagnostics if d.code.startswith("ERROR_UNBALANCED_"))
    return unbalanced or tuple(diagnostics)


def _normalize_once(text: str, *, validate: bool = True) -> PipelineResult:
    first_protected = protect_markdown(text)
    original_protected_snippets = tuple(
        text[span.start : span.end] for span in _top_level_protected_spans(first_protected)
    )

    existing_math_spans, initial_parse_diags = parse_math_spans(text, first_protected)
    if initial_parse_diags:
        raise ValidationError(
            _format_diagnostics("parse", _prefer_unbalanced(initial_parse_diags))
        )

    normalization_protected = tuple(
        sorted(
            (*first_protected, *existing_math_spans),
            key=lambda span: (span.start, span.end),
        )
    )

    normalized = normalize_text(text, normalization_protected)

    final_protected = protect_markdown(normalized)
    math_spans, parse_diags = parse_math_spans(normalized, final_protected)
    if parse_diags:
        raise ValidationError(_format_diagnostics("parse", parse_diags))

    with_candidates = wrap_math_candidates(normalized, math_spans, final_protected)
    candidate_protected = protect_markdown(with_candidates)
    candidate_math_spans, parse_diags = parse_math_spans(
        with_candidates,
        candidate_protected,
    )
    if parse_diags:
        raise ValidationError(_format_diagnostics("parse", parse_diags))

    with_blanks = replace_underscore_blanks(
        with_candidates,
        candidate_math_spans,
        candidate_protected,
    )
    merged = merge_inline_math(with_blanks)
    with_displaystyle = apply_displaystyle(merged)

    if not validate:
        return PipelineResult(with_displaystyle, ())

    validation = validate_markdown_text(
        with_displaystyle,
        original_protected_snippets=original_protected_snippets,
    )
    if not validation.is_valid:
        raise ValidationError(_format_diagnostics("validate", validation.diagnostics))

    return PipelineResult(with_displaystyle, validation.diagnostics)


def normalize_pipeline(
    text: str,
    *,
    strict: bool = True,
) -> PipelineResult:
    """Run the full normalization pipeline.

    ``strict=True`` (the default) requires the *result* to pass validation; the
    API uses it to guarantee that what it returns is conforming.

    ``strict=False`` only transforms the text. ``check_markdown_file`` uses it
    to answer "is this text fixable, and does it still need fixing?" without
    requiring the input to already be conforming. Either way the result must be
    a fixed point, so a second pass cannot change it further.

    The validator reaches its own conclusions by calling ``_normalize_once``
    with ``validate=False`` directly, so the two modules never recurse.
    """
    first = _normalize_once(text, validate=strict)
    second = _normalize_once(first.text, validate=strict)
    if first.text != second.text:
        raise ValidationError(_format_diagnostics("idempotence", second.diagnostics))
    return first
