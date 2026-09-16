from __future__ import annotations

import re
from dataclasses import dataclass


def line_col(text: str, offset: int) -> tuple[int, int]:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if offset > len(text):
        raise ValueError("offset out of range")
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    column = offset - last_newline if last_newline == -1 else offset - last_newline
    return line, column


def is_escaped(text: str, offset: int) -> bool:
    if offset <= 0 or offset > len(text):
        return False
    backslashes = 0
    i = offset - 1
    while i >= 0 and text[i] == "\\":
        backslashes += 1
        i -= 1
    return (backslashes % 2) == 1


def iter_lines_with_offsets(text: str):
    start = 0
    for index, line in enumerate(text.splitlines(True), start=1):
        yield index, line, start
        start += len(line)


def is_in_spans(offset: int, spans: tuple | list) -> bool:
    for span in spans:
        if span.start <= offset < span.end:
            return True
        if span.start > offset:
            break
    return False
