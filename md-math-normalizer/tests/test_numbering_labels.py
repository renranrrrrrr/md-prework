"""编号标记保持原样：``例1`` / ``（10）`` / ``### 1.`` 等不进入数学环境。

这是对规格 §50「除题号、分值外不存在其他数字例外」的有意偏离：编号是版面标签，
包进数学环境（``例$1$``）只产生噪音，用户明确要求保持原样。
"""

from __future__ import annotations

import pytest

from md_math_normalizer import normalize_markdown_text
from md_math_normalizer.validator import validate_markdown_text

# (输入, 期望输出)
KEPT_AS_IS = [
    ("例1 求函数 y 的定义域.", "例1 求函数 $y$ 的定义域."),
    ("例 10 证明函数 g(x) 不是周期函数.", "例 10 证明函数 $g$($x$) 不是周期函数."),
    ("(2) 求函数 f(x) 的值域.", "(2) 求函数 $f$($x$) 的值域."),
    ("### 1. 三角函数性质", "### 1. 三角函数性质"),
    ("1 三角函数的图象与性质", "1 三角函数的图象与性质"),
    ("2 与正弦曲线 x 对称.", "2 与正弦曲线 $x$ 对称."),
    ("① $ af(x)+b $", "① $ af(x)+b $"),
    ("①当 $ x>1 $时.", "①当 $ x>1 $时."),
    ("（10）  $ y = A \\sin x $", "(10)  $ y = A \\sin x $"),
    ("第3节 三角函数的图象", "第3节 三角函数的图象"),
    ("图2-1 中的曲线", "图2-1 中的曲线"),
    ("(A) 关于 x 轴对称 (B) 关于 y 轴对称", "(A) 关于 $x$ 轴对称 (B) 关于 $y$ 轴对称"),
    ("（D）是由 g(x) 的图象平移得到", "(D)是由 $g$($x$) 的图象平移得到"),
    ("4 已知 f(x)=1.", "4 已知 $f$($x$)$=1$."),
]

# 规格规定的例外仍然生效。
OTHER_EXCEPTIONS = [
    ("1. 设 a、b、c 为正实数，且 a+b=10.", "1. 设 $a, b, c$ 为正实数, 且 $a+b=10$."),
    ("9.（16分）求 sum_{i=1}^n a_i.", "9.(16分)求 $sum_{i=1}^n a_i$."),
]

# 正文里的数字仍必须数学化。
STILL_MATHIFIED = [
    ("结果为 10.", "结果为 10."),
    ("已知 x=1, y=2.", "已知 $x=1, y=2$."),
]


@pytest.mark.parametrize(("source", "expected"), KEPT_AS_IS + OTHER_EXCEPTIONS + STILL_MATHIFIED)
def test_numbering_labels_stay_plain(source, expected):
    assert normalize_markdown_text(source) == expected


def test_numbering_labels_are_not_reported_as_violations():
    for source, expected in KEPT_AS_IS + OTHER_EXCEPTIONS + STILL_MATHIFIED:
        result = validate_markdown_text(expected)
        assert result.is_valid, (source, [d.code for d in result.diagnostics])


def test_numbering_labels_are_idempotent():
    for source, expected in KEPT_AS_IS:
        assert normalize_markdown_text(expected) == expected, source
