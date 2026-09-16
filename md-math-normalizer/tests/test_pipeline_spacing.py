"""D2/D3: wrapper spacing rules and inline math merging."""

from md_math_normalizer import normalize_markdown_text as normalize

CASES = [
    # report §109 exact expectation (space inside math kept, comma glued)
    (
        "1. 设 a、b、c 为正实数，且 a+b=10.",
        "1. 设 $a, b, c$ 为正实数, 且 $a+b=10$.",
    ),
    # report D1/D2 exact expectation for the sum case
    (
        "9.（16分）求 sum_{i=1}^n a_i.",
        "9.(16分)求 $sum_{i=1}^n a_i$.",
    ),
    # no phantom space before the following CJK character
    ("设 x=10 时", "设 $x=10$ 时"),
    ("设 a、b、c 为正实数", "设 $a, b, c$ 为正实数"),
    # a sentence period is not absorbed by the new math environment
    ("结果为 10.", "结果为 $10$."),
    ("由 x_1+x_2=1 可知", "由 $x_1+x_2=1$ 可知"),
]


def test_wrapper_outputs_match_spec_cases():
    for source, expected in CASES:
        assert normalize(source) == expected, source


def test_normalize_is_idempotent_on_spec_cases():
    for source, expected in CASES:
        once = normalize(source)
        assert normalize(once) == once
        assert normalize(expected) == expected
