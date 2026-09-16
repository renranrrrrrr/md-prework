from md_math_normalizer.pipeline import normalize_pipeline


def test_core_example():
    text = "1. 设 a、b、c 为正实数，且 a+b=10."
    result = normalize_pipeline(text)
    assert result.text == "1. 设 $a, b, c$ 为正实数, 且 $a+b=10$."


def test_pipeline_wraps_subscripts_without_splitting():
    text = "9.（16分）求 sum_{i=1}^n a_i."
    result = normalize_pipeline(text)
    assert result.text == "9.(16分)求 $sum_{i=1}^n a_i$."


def test_pipeline_keeps_protected_html_byte_identical():
    text = '<div style="text-align: center;"><img src="https://abc.com/a_123.jpg" width="50%" /></div>'
    assert normalize_pipeline(text).text == text


def test_pipeline_merges_and_adds_displaystyle():
    text = "$a$ + $b$ = $10$ 与 $\\frac{1}{2}$"
    result = normalize_pipeline(text).text
    assert "$a + b = 10$" in result
    assert "$\\displaystyle \\frac{1}{2}$" in result


def test_idempotence_guaranteed_by_pipeline():
    text = "a+b"
    first = normalize_pipeline(text).text
    second = normalize_pipeline(first).text
    assert first == second
