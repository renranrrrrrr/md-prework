from md_math_normalizer.char_normalizer import normalize_text
from md_math_normalizer.protector import protect_markdown


def test_punctuation_and_dunhao_are_normalized():
    # 逗号/冒号/分号/叹号/问号按规则补一个空格；括号与顿号直接替换。
    assert normalize_text("a、b、c") == "a, b, c"
    assert normalize_text("a、b、c，d") == "a, b, c, d"
    assert normalize_text("x：y；z！w？") == "x: y; z! w?"


def test_brackets_and_full_stop_are_normalized():
    assert normalize_text("g（h）【i】") == "g(h)[i]"
    assert normalize_text("结束。") == "结束."


def test_sentence_period_is_not_separated_from_following_math():
    assert normalize_text("结果为10.") == "结果为10."


def test_fullwidth_alpha_numbers_are_normalized():
    text = "ＡＢＣ１２３"
    assert normalize_text(text) == "ABC123"


def test_protected_spans_remain_unchanged():
    text = "x$y$ `a-b`"
    protected = protect_markdown(text)
    assert any(s.kind.name == "INLINE_CODE" for s in protected)
    protected_text = normalize_text(text, protected)
    assert "a-b" in protected_text
