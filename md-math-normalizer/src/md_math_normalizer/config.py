from __future__ import annotations

import re
import sys
import unicodedata

FULLWIDTH_NORMALIZATION_ENABLED = True

SUPPORTED_INPUT_SUFFIX = {".md"}

# ---------------------------------------------------------------------------
# 字符标准化规则（唯一来源）
#
# 规则只包含“具有明确 ASCII 等价形式”的字符：
#   * 全角 ASCII 变体（U+FF01..U+FF5D）
#   * 汉字/中文排版标点（顿号、句号、括号、引号等）
#   * 少量与 ASCII 存在唯一兼容分解的非 ASCII 字符
#
# 数学符号（≤ ≥ √ ∈ × ÷ π α ² ³ ₁ ① Ⅰ 等）一律不进入映射表。
# ---------------------------------------------------------------------------

ASCII_PUNCTUATION_MAP = {
    "\u3001": ",",  # 、 表意逗号
    "\u3002": ".",  # 。 表意句号
    "\u3008": "<",
    "\u3009": ">",
    "\u300a": "<",
    "\u300b": ">",
    "\u300c": '"',
    "\u300d": '"',
    "\u300e": '"',
    "\u300f": '"',
    "\u3010": "[",
    "\u3011": "]",
    "\u3014": "[",
    "\u3015": "]",
    "\u3016": "[",
    "\u3017": "]",
    "\u3018": "[",
    "\u3019": "]",
    "\u301a": "[",
    "\u301b": "]",
    "\u301c": "~",
    "\u3030": "~",
    "\u30fb": ".",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2025": "..",
    "\u2026": "...",
    "\u2027": ".",
    "\u2032": "'",
    "\u2033": '"',
    "\u203b": "*",
    "\uff01": "!",
    "\uff0c": ",",
    "\uff0e": ".",
    "\uff1a": ":",
    "\uff1b": ";",
    "\uff1f": "?",
    "\uff08": "(",
    "\uff09": ")",
    "\uff3b": "[",
    "\uff3d": "]",
    "\uff5b": "{",
    "\uff5d": "}",
    "\uff0d": "-",
    "\uff5e": "~",
    "\ufe41": "(",
    "\ufe42": ")",
    "\ufe43": "[",
    "\ufe44": "]",
    "\ufe45": "{",
    "\ufe46": "}",
}


def _is_fullwidth_ascii_variant(char: str) -> bool:
    """True for U+FF01..U+FF5D, the fullwidth forms of printable ASCII."""
    return 0xFF01 <= ord(char) <= 0xFF5D


def _is_cjk_char(char: str) -> bool:
    point = ord(char)
    return (
        0x2E80 <= point <= 0x2FFF
        or 0x3000 <= point <= 0x303F
        or 0x3040 <= point <= 0x30FF
        or 0x3400 <= point <= 0x4DBF
        or 0x4E00 <= point <= 0x9FFF
        or 0xF900 <= point <= 0xFAFF
        or 0xFE30 <= point <= 0xFE4F
        or 0x20000 <= point <= 0x3FFFF
    )


def _is_math_or_number_form(char: str, point: int) -> bool:
    """True for compatibility forms that carry mathematical meaning.

    Homeopathic compatibility characters such as superscripts (``²``),
    subscripts (``₁``), enclosed alphanumerics (``①``), Roman numerals (``Ⅰ``),
    number forms (``⅓``) and letterlike symbols (``ℕ``) must never be folded to
    ASCII: that would rewrite mathematical content.
    """
    return (
        0x2070 <= point <= 0x209F  # 上标/下标
        or 0x2150 <= point <= 0x218F  # 数字形式 / 罗马数字
        or 0x2460 <= point <= 0x24FF  # 带圈字母数字
        or 0x2100 <= point <= 0x214F  # 字母式符号
        or 0x1D400 <= point <= 0x1D7FF  # 数学字母数字符号
        or unicodedata.category(char) == "No"
    )


def _build_ascii_compat_map() -> dict[str, str]:
    """Build the per-character ASCII compatibility map.

    Only two groups are mapped:

    * fullwidth ASCII variants (U+FF01..U+FF5D), which have a unique and
      lossless ASCII counterpart;
    * other characters whose NFKC form is non-empty, pure ASCII, and which are
      neither CJK/CJK punctuation nor a mathematical number form.

    Everything else — superscripts and subscripts (``²``, ``₁``), enclosed
    alphanumerics (``①``), Roman numerals (``Ⅰ``), CJK characters and CJK
    brackets (``【】``) — is deliberately left alone so no mathematical content
    is silently rewritten.
    """
    mapping: dict[str, str] = {}
    for point in range(sys.maxunicode + 1):
        char = chr(point)
        if point < 0x80:
            continue
        if unicodedata.category(char) in {"Cc", "Cs", "Co", "Cn"}:
            continue
        if _is_fullwidth_ascii_variant(char):
            mapping[char] = chr(point - 0xFEE0)
            continue
        if _is_cjk_char(char) or _is_math_or_number_form(char, point):
            continue
        try:
            decomposed = unicodedata.normalize("NFKC", char)
        except ValueError:  # pragma: no cover - defensive, never triggered in practice
            continue
        if not decomposed or decomposed == char:
            continue
        if all(ord(part) < 0x80 for part in decomposed):
            mapping[char] = decomposed
    return mapping


ASCII_COMPAT_MAP = _build_ascii_compat_map()

# 合并后的最终映射：显式标点规则优先于通用兼容分解。
CHARACTER_MAP = {**ASCII_COMPAT_MAP, **ASCII_PUNCTUATION_MAP}
CHARACTER_TRANSLATION = str.maketrans(CHARACTER_MAP)

DISPLAYSTYLE_COMMANDS = {
    r"\frac",
    r"\dfrac",
    r"\cfrac",
    r"\sum",
    r"\prod",
    r"\coprod",
    r"\int",
    r"\iint",
    r"\iiint",
    r"\iiiint",
    r"\oint",
    r"\bigcup",
    r"\bigcap",
    r"\bigsqcup",
    r"\bigvee",
    r"\bigwedge",
    r"\bigoplus",
    r"\bigotimes",
    r"\bigodot",
}

# 行首题号：数字 + 句点。句点后紧跟空白、左括号或文字都算题号；
# 句点后紧跟数字（例如正文中的 ``10.5``）不算题号。
QUESTION_NUMBER_RE = re.compile(r"(?m)^[ \t]*[0-9]+\.[ \t]*(?=[ \t(（]|[^0-9\s])")

# 分值：数字 + “分”，允许外面包一层圆括号，例如 （16分） / (16分) / 16分。
SCORE_RE = re.compile(r"[(（]?[0-9]+(?=分)")

# 编号标记：编号数字属于正文标签，不进入数学环境。
#
# 三类明确形态：
#   1. 行首编号：``### 1.`` / ``1.`` / ``1)`` / ``1 、`` / ``(2)`` / ``（10）`` / ``1``
#   2. 标签前缀编号：``例1`` / ``例 10`` / ``第3`` / ``图2-1``
#   3. 圈码与罗马数字：``①`` / ``ⅱ``
#
# 行首编号单独成 token 时也视为标签（例如页眉 ``1 三角函数的图象与性质``）。这与
# 规格"所有数字必须数学化"不同，属于有意偏离：编号是版面标签，包进数学环境只是噪音。
NUMBERING_MARKER_RES: tuple[re.Pattern[str], ...] = (
    # 选择题选项标记：``(A)`` / ``（D）``。选项常写在行中间（一行两个选项），
    # 所以不限行首。只认**大写**字母：``f(x)``、``g(x)`` 这类函数记号不能误伤。
    re.compile(r"[(（](?P<option>[A-Z])[)）]"),
    # 行首编号，允许前置 Markdown 结构字符（``### ``、``- `` 等）。
    re.compile(
        r"(?m)^[^\w\n]*?"
        r"(?:"
        r"(?P<paren_a>[0-9]+)[)）]"
        r"|[(（](?P<paren_b>[0-9]+)[)）]"
        r"|(?P<num>[0-9]+)(?=[.、)）][ \t]|[ \t]|$)"
        r")"
    ),
    # 标签前缀编号：例/题/图/表/式/注/第 + 编号。
    re.compile(r"[例题图表式注第]\s*(?P<num>[0-9]+(?:[.\-][0-9]+)*)"),
    # 圈码与罗马数字。
    re.compile(r"(?P<num>[\u2460-\u2473\u2160-\u217F])"),
)

# Markdown 结构字符：数学候选不得跨越这些字符。
MARKDOWN_BOUNDARY_CHARS = set("`*_~[]()!<>|#")

# 经过字符标准化后允许留在正文中的标点（不属于数学候选内容）。
# 逗号与数字间小数点除外；它们由数学候选扫描器单独判定。
ALLOWED_TEXT_PUNCTUATION = set(",.:;!?")
