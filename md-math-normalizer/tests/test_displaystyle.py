from md_math_normalizer.displaystyle import apply_displaystyle


def test_frac_adds_displaystyle():
    assert apply_displaystyle("$\\frac{a}{b}$") == "$\\displaystyle \\frac{a}{b}$"


def test_sum_adds_displaystyle():
    assert apply_displaystyle("$\\sum_{i=1}^n a_i$").startswith("$\\displaystyle \\sum")


def test_display_math_unchanged():
    assert apply_displaystyle("$$\\frac{a}{b}$$") == "$$\\frac{a}{b}$$"


def test_keep_existing_displaystyle():
    assert apply_displaystyle("$\\displaystyle \\int x dx$") == "$\\displaystyle \\int x dx$"
