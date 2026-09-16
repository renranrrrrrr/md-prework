"""D1: math candidates must keep subscripts, braces and commands intact."""

from md_math_normalizer.math_candidate import (
    find_math_candidate_spans,
    wrap_math_candidates,
)
from md_math_normalizer.math_parser import parse_math_spans
from md_math_normalizer.protector import protect_markdown


def _wrap(text: str) -> str:
    protected = protect_markdown(text)
    spans, diags = parse_math_spans(text, protected)
    assert not diags
    return wrap_math_candidates(text, spans, protected)


def test_simple_subscript_stays_in_one_span():
    assert _wrap("设 x_1 为") == "设 $x_1$ 为"


def test_braced_subscript_stays_in_one_span():
    assert _wrap("设 a_{n+1} 为") == "设 $a_{n+1}$ 为"


def test_latex_command_stays_in_one_span():
    assert _wrap("得 \\frac{a}{b} 的值") == "得 $\\frac{a}{b}$ 的值"


def test_subscript_equation_stays_in_one_span():
    assert _wrap("由 x_1+x_2=1 可知") == "由 $x_1+x_2=1$ 可知"


def test_sum_with_limits_stays_in_one_span():
    assert _wrap("求 sum_{i=1}^n a_i 的值") == "求 $sum_{i=1}^n a_i$ 的值"


def test_report_case_sum_is_wrapped_whole():
    text = "9.（16分）求 sum_{i=1}^n a_i."
    result = _wrap(text)
    assert "$sum_{i=1}^n a_i$" in result
    assert "_" not in result.replace("$sum_{i=1}^n a_i$", "")


def test_candidates_do_not_cross_cjk():
    protected = protect_markdown("a 与 b")
    spans = find_math_candidate_spans("a 与 b", (), protected)
    assert len(spans) >= 1
    for span in spans:
        assert "与" not in "a 与 b"[span.start : span.end]


def test_candidates_do_not_cross_newline():
    text = "a\nb"
    protected = protect_markdown(text)
    spans = find_math_candidate_spans(text, (), protected)
    for span in spans:
        assert "\n" not in text[span.start : span.end]


def test_candidates_do_not_cross_inline_code():
    text = "a `b` c"
    protected = protect_markdown(text)
    spans = find_math_candidate_spans(text, (), protected)
    for span in spans:
        assert "`" not in text[span.start : span.end]


def test_candidate_does_not_swallow_markdown_emphasis():
    result = _wrap("a **b**")
    # 强调标记本身必须留在数学环境之外，只包裹其中的数学内容。
    assert result == "$a$ **$b$**"
    assert "**$b$**" in result


def test_candidate_does_not_swallow_markdown_link():
    result = _wrap("见 [x](https://example.com) 处")
    assert "https://example.com" in result
    assert "$[x]" not in result


def test_protected_url_is_not_wrapped():
    text = "见 https://example.com/a_1.png 图"
    assert _wrap(text) == text
