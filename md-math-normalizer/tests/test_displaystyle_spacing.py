"""D2: displaystyle only inserts \\displaystyle; it must not restyle spacing."""

from md_math_normalizer.displaystyle import apply_displaystyle

FRAC = "\\frac{a}{b}"


def test_frac_adds_displaystyle_without_touching_surrounding_space():
    assert apply_displaystyle(f"${FRAC}$") == f"$\\displaystyle {FRAC}$"


def test_existing_inline_math_is_not_padded():
    assert apply_displaystyle("若 $a$ 与 $b$ 则") == "若 $a$ 与 $b$ 则"


def test_existing_displaystyle_is_not_duplicated():
    assert apply_displaystyle("$\\displaystyle \\int x dx$") == "$\\displaystyle \\int x dx$"


def test_inner_whitespace_of_existing_math_is_preserved():
    assert apply_displaystyle("$ a + b $") == "$ a + b $"


def test_inner_whitespace_is_preserved_when_adding_displaystyle():
    # \displaystyle 固定加在环境内容开头（规格 §72），内容其余部分逐字保留。
    assert apply_displaystyle(f"$ {FRAC} $") == f"$\\displaystyle {FRAC} $"


def test_display_math_is_never_touched():
    assert apply_displaystyle(f"$$\n{FRAC}\n$$") == f"$$\n{FRAC}\n$$"


def test_no_outer_space_is_added_to_adjacent_inline_math():
    assert apply_displaystyle("$a$与$b$") == "$a$与$b$"


def test_text_without_math_is_returned_unchanged():
    assert apply_displaystyle("普通正文，x = 1。") == "普通正文，x = 1。"


def test_plain_text_without_math_is_untouched():
    assert apply_displaystyle("普通正文, x = 1.") == "普通正文, x = 1."
