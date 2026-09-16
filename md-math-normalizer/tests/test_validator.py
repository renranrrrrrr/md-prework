from md_math_normalizer.validator import validate_markdown_text


def test_bare_number_is_error():
    result = validate_markdown_text("第1题 x=1")
    assert not result.is_valid


def test_unbalanced_math_is_error():
    result = validate_markdown_text("设 $x+1 为正数")
    assert not result.is_valid


def test_mergeable_inline_math_is_error():
    result = validate_markdown_text("$a$ + $b$")
    assert not result.is_valid


def test_missing_displaystyle_is_error():
    result = validate_markdown_text("设 $\\frac{1}{2}$")
    assert not result.is_valid


def test_duplicate_displaystyle_is_error():
    result = validate_markdown_text("$\\displaystyle \\displaystyle x$")
    assert not result.is_valid
