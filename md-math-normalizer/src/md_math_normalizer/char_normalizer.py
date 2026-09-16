"""Deterministic Unicode and Chinese punctuation normalization.

Every replacement comes from the single map defined in :mod:`config`. There is
no generic NFKC pass over the text: a character is only rewritten when the map
contains an explicit ASCII equivalent for it.
"""

from __future__ import annotations

from .config import (
    ALLOWED_TEXT_PUNCTUATION,
    ASCII_COMPAT_MAP,
    ASCII_PUNCTUATION_MAP,
    CHARACTER_TRANSLATION,
    FULLWIDTH_NORMALIZATION_ENABLED,
)

__all__ = ["ASCII_COMPAT_MAP", "ASCII_PUNCTUATION_MAP", "normalize_text"]

# 需要在后面补一个空格的标点：逗号、感叹号、问号、冒号、分号。
_SPACING_PUNCTUATION = set(",!?:;")


def _is_question_number_period(text: str, index: int) -> bool:
    """True when the period at ``index`` terminates a leading question number.

    ``9.（16分）`` must stay compact: the number and the period form a question
    number, not a sentence ending followed by a new sentence.
    """
    position = index - 1
    digits = 0
    while position >= 0 and text[position].isdigit():
        digits += 1
        position -= 1
    if digits == 0:
        return False
    # 数字之前只允许空白（或行首），否则只是普通句子中的 ``10.``。
    while position >= 0:
        char = text[position]
        if char == "\n":
            return True
        if not char.isspace():
            return False
        position -= 1
    return True


def _needs_space_after_period(text: str, index: int) -> bool:
    next_char = text[index + 1] if index + 1 < len(text) else ""
    if next_char == "" or next_char.isspace():
        return False
    if next_char in ".,;:!?":
        # 省略号等连续标点不拆开。
        return False
    previous_is_digit = index > 0 and text[index - 1].isdigit()
    if previous_is_digit and next_char.isdigit():
        return False
    if _is_question_number_period(text, index):
        return False
    return True


def _ensure_punctuation_spacing(text: str) -> str:
    """Insert one space after ASCII punctuation that must be followed by space.

    A period is only spaced when it does not sit between two digits (decimals),
    is not a sentence terminator already followed by a space, and is not part of
    a punctuation run such as the ``...`` produced from an ellipsis.
    Question numbers such as ``9.(16分)`` keep their compact form.
    """
    output: list[str] = []
    for index, char in enumerate(text):
        output.append(char)
        if char == ".":
            if _needs_space_after_period(text, index):
                output.append(" ")
            continue
        if char not in _SPACING_PUNCTUATION:
            continue
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if next_char and not next_char.isspace():
            output.append(" ")
    return "".join(output)


def _normalize_unprotected(text: str) -> str:
    if not text:
        return text
    # 省略号在映射前先合并，避免 ``……`` 逐字映射成 ``... ...``。
    collapsed = text.replace("\u2026\u2026", "\u2026")
    if FULLWIDTH_NORMALIZATION_ENABLED:
        mapped = collapsed.translate(CHARACTER_TRANSLATION)
    else:
        mapped = collapsed
    return _ensure_punctuation_spacing(mapped)


def normalize_text(text: str, protected_spans: tuple[object, ...] = ()) -> str:
    """Normalize text while preserving every protected span byte-for-byte.

    Protected spans use the project's ``start`` and ``end`` offsets. Each
    unprotected segment is normalized independently, so protected Markdown,
    code, and math content are never altered by this pass.
    """

    if not protected_spans:
        return _normalize_unprotected(text)

    pieces: list[str] = []
    cursor = 0
    text_length = len(text)

    for span in protected_spans:
        start = max(cursor, min(text_length, int(span.start)))
        end = max(start, min(text_length, int(span.end)))
        pieces.append(_normalize_unprotected(text[cursor:start]))
        pieces.append(text[start:end])
        cursor = end

    pieces.append(_normalize_unprotected(text[cursor:]))
    return "".join(pieces)
