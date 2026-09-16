"""D5: is_valid must express spec conformance, without losing the safe-to-fix signal."""

import pytest

from md_math_normalizer.validator import validate_markdown_text

VIOLATIONS = [
    ("裸变量", "设 x 为实数"),
    ("裸数字", "第1题 x=1"),
    ("裸等式", "a+b=10"),
    ("可合并的数学环境", "$a$ + $b$"),
    ("缺少 displaystyle", "设 $\\frac{1}{2}$"),
    ("未闭合定界符", "设 $x+1 为正数"),
    ("非法嵌套", "$a $b$ c$"),
    ("重复 displaystyle", "$\\displaystyle \\displaystyle x$"),
]

CONFORMING = [
    "1. 设 $a, b, c$ 为正实数, 且 $a+b=10$.",
    "9.(16分)求 $sum_{i=1}^n a_i$.",
    "本题 $16$ 分, 得 $x=1$.",
    "1. 本题共16分, 设 $x=1$.",
    "$\\displaystyle \\frac{1}{2}$",
    "$$\\frac{a}{b}$$",
    "纯中文正文, 没有数学.",
    "见 https://example.com/a_1.png 图",
    "设 `x = 10` 时 $a$ 不变",
]


@pytest.mark.parametrize(("label", "text"), VIOLATIONS, ids=[v[0] for v in VIOLATIONS])
def test_violations_are_not_valid(label, text):
    result = validate_markdown_text(text)
    assert result.is_valid is False, label


def test_fixable_violations_still_report_needs_normalization():
    for _, text in VIOLATIONS:
        result = validate_markdown_text(text)
        if text not in {"$\\displaystyle \\displaystyle x$", "$a $b$ c$", "设 $x+1 为正数"}:
            assert result.needs_normalization is True, text


@pytest.mark.parametrize("text", CONFORMING)
def test_conforming_text_is_valid(text):
    result = validate_markdown_text(text)
    assert result.is_valid is True, (text, [d.code for d in result.diagnostics])
    assert result.needs_normalization is False, text


def test_question_number_and_score_digits_are_not_violations():
    result = validate_markdown_text("1. 本题共16分.")
    assert result.is_valid is True
    assert [d.code for d in result.diagnostics] == []
    assert not [d for d in result.diagnostics if d.code == "ERROR_SCORE_MATHIFIED"]


def test_parse_errors_carry_error_severity():
    result = validate_markdown_text("设 $x+1 为正数")
    assert any(d.code == "ERROR_UNBALANCED_INLINE_MATH" for d in result.diagnostics)
    assert all(
        d.severity.name == "ERROR"
        for d in result.diagnostics
        if d.code.startswith("ERROR_") and "UNBALANCED" in d.code
    )
