from md_math_normalizer.math_parser import parse_math_spans


def test_normal_inline_math():
    spans, diags = parse_math_spans("a + $b$ + c")
    assert not diags
    assert len(spans) == 1
    assert spans[0].start == 4 and spans[0].end == 7


def test_normal_display_math():
    spans, diags = parse_math_spans("$$a+b$$")
    assert not diags
    assert len(spans) == 1
    assert spans[0].start == 0


def test_escaped_dollar_is_not_math():
    spans, diags = parse_math_spans(r"$a\$,b$")
    assert not any("UNBALANCED" in d.code for d in diags)
    assert len(spans) == 1


def test_unbalanced_inline_math():
    spans, diags = parse_math_spans("a + $b + c")
    assert any(d.code == "ERROR_UNBALANCED_INLINE_MATH" for d in diags)


def test_unbalanced_display_math():
    spans, diags = parse_math_spans("$$a+b")
    assert any(d.code == "ERROR_UNBALANCED_DISPLAY_MATH" for d in diags)


def test_nested_math_is_reported():
    # 环境内部还有未转义的 $ —— 真正的非法嵌套。
    spans, diags = parse_math_spans("$$ a $b$ c $$")
    assert any(d.code == "ERROR_NESTED_MATH_ENVIRONMENT" for d in diags)


def test_space_padded_style_is_not_nested():
    # 宽松写法（$a $ / $ c$）本身是两两配对的合法环境，不算非法嵌套。
    spans, diags = parse_math_spans("$a $b$ c$")
    assert not any(d.code == "ERROR_NESTED_MATH_ENVIRONMENT" for d in diags)
    assert not diags
