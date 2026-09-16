"""回归：句末标点不得被合并吞进数学环境（真实 OCR 文件暴露的破坏性 bug）。

真实案例：OCR 输出 `... 上为增函数  $ (k \\in \\mathbf{Z}) $.  $ y = \\cot x $ 在 ...`。
标点标准化会给句号补一个空格，合并阶段若把 `" . "` 当成可合并间隙，就会把句号
甚至后面的正文吞进数学环境。
"""

from md_math_normalizer import normalize_markdown_text
from md_math_normalizer.math_merger import merge_inline_math
from md_math_normalizer.validator import validate_markdown_text

LINE = (
    " $ y = \\tan x $ 在  $ \\left(-\\frac{\\pi}{2}, \\frac{\\pi}{2}\\right) $ 上为增函数"
    "  $ (k \\in \\mathbf{Z}) $.  $ y = \\cot x $ 在  $ (k\\pi, \\pi + k\\pi) $ 上为减函数"
    "  $ (k \\in \\mathbf{Z}) $."
)


def test_period_between_environments_blocks_merging():
    assert merge_inline_math("$a$.  $b$") == "$a$.  $b$"
    assert merge_inline_math("$a$!  $b$") == "$a$!  $b$"


def test_prose_after_period_is_never_swallowed():
    result = normalize_markdown_text(LINE)
    # 句号后的正文与下一个公式必须完整保留。
    assert "$ (k \\in \\mathbf{Z}) $." in result
    assert "y = \\cot x" in result
    assert "上为增函数" in result
    assert "上为减函数" in result
    # 数学环境里不允许出现汉字。
    for fragment in result.split("$")[1::2]:
        assert not any("\u4e00" <= char <= "\u9fff" for char in fragment), fragment


def test_swallowing_regression_is_idempotent_and_valid():
    once = normalize_markdown_text(LINE)
    assert normalize_markdown_text(once) == once
    assert validate_markdown_text(once).is_valid
