from md_math_normalizer.math_merger import merge_inline_math


def test_addition_merge():
    assert merge_inline_math("$a$+$b$") == "$a+b$"


def test_space_merge():
    assert merge_inline_math("$a$ = $b$") == "$a = b$"


def test_comma_merge():
    assert merge_inline_math("$a$,$b$,$c$") == "$a,b,c$"


def test_chinese_boundary_blocks_merge():
    assert merge_inline_math("$a$ 与 $b$") == "$a$ 与 $b$"


def test_newline_boundary_blocks_merge():
    assert merge_inline_math("$a$\n$b$") == "$a$\n$b$"
