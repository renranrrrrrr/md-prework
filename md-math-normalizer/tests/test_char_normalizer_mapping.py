"""D4: only explicit, mappable ASCII equivalents are replaced."""

from md_math_normalizer.char_normalizer import normalize_text
from md_math_normalizer.config import ASCII_COMPAT_MAP, ASCII_PUNCTUATION_MAP
from md_math_normalizer.protector import protect_markdown

ALLOWED = [
    ("a，b。c：d；e！f？g（h）", "a, b. c: d; e! f? g(h)"),
    ("a、b、c", "a, b, c"),
    ("ＡＢＣ１２３", "ABC123"),
    ("ａｂ＝ｃ", "ab=c"),
    ("［］｛｝＋－＜＞％＃＆＊／＼", "[]{}+-<>%#&*/\\"),
    ("“引号”", '"引号"'),
    ("【i】《k》", "[i]<k>"),
    ("……", "..."),
]

PRESERVED = [
    "π α β ≤ ≥ √ ∈ × ÷ ± → ∞ ∑ ∫",
    "x² + y³",
    "x₁ + y₂",
    "①②③",
    "Ⅰ Ⅱ Ⅲ",
    "中文汉字",
    "",
]


def test_mappable_characters_are_replaced():
    for source, expected in ALLOWED:
        assert normalize_text(source) == expected, source


def test_unmapped_characters_are_preserved():
    for source in PRESERVED:
        assert normalize_text(source) == source, source


def test_no_character_is_replaced_without_a_map_entry():
    for char in "π≤√∈×²₁①Ⅰ":
        assert char not in ASCII_COMPAT_MAP
        assert char not in ASCII_PUNCTUATION_MAP


def test_protected_spans_are_never_mapped():
    text = "x `ＡＢＣ１２３，` y"
    protected = protect_markdown(text)
    result = normalize_text(text, protected)
    assert "`ＡＢＣ１２３，`" in result
    assert result.startswith("x ")


def test_math_span_content_is_never_mapped():
    text = "$ＡＢＣ$"
    protected = protect_markdown(text)
    from md_math_normalizer.math_parser import parse_math_spans

    spans, _ = parse_math_spans(text, protected)
    result = normalize_text(text, tuple(protected) + tuple(spans))
    assert result == text
