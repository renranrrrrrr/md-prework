"""D6: unbalanced and nested delimiters, and legal adjacent environments.

嵌套只按一条原则判定：**已配对的环境内部又出现未转义的 ``$``**。
"收尾分隔符放错位置" 这类形态（``$a $b$ c$``）由未闭合检测覆盖 —— 结果同样是
拒绝文件，但诊断更准确，也不会因为 OCR 的宽松空格写法误报。
"""

from md_math_normalizer.math_parser import parse_math_spans

# 环境内部还有 $ —— 真正的非法嵌套。
NESTED = [
    "$$ a $b$ c $$",
]

# 没有可配对的收尾定界符 —— 报未闭合。
UNBALANCED = [
    "a + $b + c",
    "$a $b$",
]

LEGAL = [
    "$a$ $b$",
    "$a$ = $b$",
    "$a$+$b$",
    "$ x $",
    "$a + b $",
    "$a$与$b$",
    "$$a$$",
    "$$a$$\n$$b$$",
    r"$a\$,b$",
    # OCR 常见写法：行内公式整体带空格，收尾 $ 前后都是空格/标点。
    "正弦函数 $ y = \\sin x $ 和正切函数 $ y = \\tan x $ 为奇函数",
    "$ y = \\sin x $ 在 $ \\left[-\\frac{\\pi}{2}, \\frac{\\pi}{2}\\right] $ 上递增",
    "$a $ 后面跟 $b $",
    "余弦函数 $ y = \\cos x $ 在其定义域上为偶函数。",
    "①当 $ \\omega>1 $时，纵坐标不变，横坐标缩短为原来的 $ \\frac{1}{\\omega} $倍；",
    "若 $D$，则 $x$ 为正数",
    "### $1$. 三角函数性质",
    "$T$, 使 $x$ 都有  $ f(x+T)=f(x) $ 成立.",
]


def test_legal_adjacent_environments_are_not_nested():
    for text in LEGAL:
        _, diags = parse_math_spans(text)
        assert not any(d.code == "ERROR_NESTED_MATH_ENVIRONMENT" for d in diags), text
        assert not any(d.code == "ERROR_UNBALANCED_INLINE_MATH" for d in diags), text


def test_unbalanced_reports_are_unchanged():
    _, diags = parse_math_spans("$$a+b")
    assert any(d.code == "ERROR_UNBALANCED_DISPLAY_MATH" for d in diags)

    for text in UNBALANCED:
        _, diags = parse_math_spans(text)
        assert any(d.code == "ERROR_UNBALANCED_INLINE_MATH" for d in diags), text


def test_nested_display_environment_is_reported():
    _, diags = parse_math_spans("$$ a $b$ c $$")
    assert any(d.code == "ERROR_NESTED_MATH_ENVIRONMENT" for d in diags)


def test_nested_region_is_rejected():
    """$$ a $b$ c $$ 必须被拒绝（环境内部出现未转义的 $）。"""
    spans, diags = parse_math_spans("$$ a $b$ c $$")
    assert any(d.code == "ERROR_NESTED_MATH_ENVIRONMENT" for d in diags)
    assert spans == ()


def test_space_padded_style_is_accepted():
    """OCR 的宽松写法两两配对成合法环境，不得报错。"""
    text = "$a $b$ c$"
    spans, diags = parse_math_spans(text)
    assert diags == ()
    assert len(spans) == 2


def test_unbalanced_inline_is_reported_when_display_block_is_broken():
    text = "设 $x+1 为正数\n$$ unclosed display\n"
    spans, diags = parse_math_spans(text)
    assert any(d.code == "ERROR_UNBALANCED_INLINE_MATH" for d in diags)
    assert len(spans) <= 1


def test_unbalanced_everywhere_is_reported():
    text = "设 $x+1 为正数\n$$ unclosed display\n`inline code\n"
    _, diags = parse_math_spans(text)
    assert any(d.code == "ERROR_UNBALANCED_INLINE_MATH" for d in diags)
