"""D3: mergeable inline math, including whitespace-only gaps."""

from md_math_normalizer.math_merger import merge_inline_math

MERGE_CASES = [
    ("$a$+$b$", "$a+b$"),
    ("$a$ = $b$", "$a = b$"),
    ("$a$,$b$", "$a,b$"),
    ("$a$+$b$+$c$", "$a+b+c$"),
    ("$a$ $b$", "$a b$"),
    ("$a$ + $b$ = $10$", "$a + b = 10$"),
    ("$a$;$b$", "$a;b$"),
    ("$a$:$b$", "$a:b$"),
    ("$a$-$b$", "$a-b$"),
]

KEEP_CASES = [
    ("$a$ 与 $b$", "$a$ 与 $b$"),
    ("$a$\n$b$", "$a$\n$b$"),
    ("$a$ **$b$**", "$a$ **$b$**"),
    ("$$a$$ $b$", "$$a$$ $b$"),
    ("$a$ $$b$$", "$a$ $$b$$"),
    ("$$a$$ $$b$$", "$$a$$ $$b$$"),
    ("$$a$$", "$$a$$"),
    ("$a$ \\, $b$", "$a$ \\, $b$"),
    ("$a$ `c` $b$", "$a$ `c` $b$"),
]


def test_mergeable_gaps_are_merged():
    for source, expected in MERGE_CASES:
        assert merge_inline_math(source) == expected, source


def test_blocking_gaps_are_preserved():
    for source, expected in KEEP_CASES:
        assert merge_inline_math(source) == expected, source


def test_merge_reaches_fixed_point_in_one_call():
    assert merge_inline_math("$a$ + $b$ + $c$ + $d$") == "$a + b + c + d$"


def test_merge_never_touches_display_math():
    assert merge_inline_math("$$a$$") == "$$a$$"
