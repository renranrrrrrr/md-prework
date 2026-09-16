from md_math_normalizer.math_candidate import wrap_math_candidates
from md_math_normalizer.math_parser import parse_math_spans
from md_math_normalizer.protector import protect_markdown


def test_variable_and_number_candidates_are_wrapped():
    text = "设 x=10 且 y>0"
    protected = protect_markdown(text)
    spans, diags = parse_math_spans(text, protected)
    assert not diags
    out = wrap_math_candidates(text, spans, protected)
    assert "$x=10" in out


def test_question_number_is_not_wrapped():
    text = "10. 设 a = 1"
    protected = protect_markdown(text)
    spans, diags = parse_math_spans(text, protected)
    out = wrap_math_candidates(text, spans, protected)
    assert out.startswith("10.")
    assert "$10" not in out


def test_score_pattern_is_not_wrapped():
    text = "本题 16分，x=1"
    protected = protect_markdown(text)
    spans, diags = parse_math_spans(text, protected)
    out = wrap_math_candidates(text, spans, protected)
    assert "$16" not in out


def test_xOy_kept_as_one_math_span():
    text = "xOy"
    protected = protect_markdown(text)
    spans, _ = parse_math_spans(text, protected)
    out = wrap_math_candidates(text, spans, protected)
    assert out == "$xOy$"
