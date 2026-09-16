"""Public API contract: normalization, check semantics and file-type rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from md_math_normalizer import (
    check_markdown_file,
    normalize_markdown_file,
    normalize_markdown_text,
)
from md_math_normalizer.errors import InvalidFileTypeError, NormalizationError

RAW = "1. 设 a、b、c 为正实数，且 a+b=10.\n"
NORMALIZED = "1. 设 $a, b, c$ 为正实数, 且 $a+b=10$.\n"


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def test_normalize_markdown_text_returns_conforming_text():
    result = normalize_markdown_text(RAW)
    assert result == NORMALIZED
    assert normalize_markdown_text(result) == result


def test_normalize_markdown_text_rejects_unsafe_input():
    with pytest.raises(NormalizationError):
        normalize_markdown_text("设 $x+1 为正数")


def test_check_reports_fixable_file(workdir):
    path = _write(workdir / "raw.md", RAW)
    result = check_markdown_file(path)
    assert result.is_valid is False
    assert result.needs_normalization is True
    assert result.conforming is False
    assert result.has_fatal_error is False
    assert [d.code for d in result.diagnostics] == ["ERROR_UNWRAPPED_MATH"]


def test_check_reports_conforming_file(workdir):
    path = _write(workdir / "good.md", NORMALIZED)
    result = check_markdown_file(path)
    assert result.is_valid is True
    assert result.needs_normalization is False
    assert result.conforming is True
    assert result.has_fatal_error is False
    assert result.diagnostics == ()


def test_check_reports_fatal_error(workdir):
    path = _write(workdir / "broken.md", "设 $x+1 为正数\n")
    result = check_markdown_file(path)
    assert result.is_valid is False
    assert result.needs_normalization is False
    assert result.conforming is False
    assert result.has_fatal_error is True
    assert any(d.code == "ERROR_UNBALANCED_INLINE_MATH" for d in result.diagnostics)


def test_normalize_markdown_file_requires_md_suffixes(workdir):
    source = _write(workdir / "input.md", RAW)
    with pytest.raises(InvalidFileTypeError):
        normalize_markdown_file(_write(workdir / "input.txt", RAW), workdir / "out.md")
    with pytest.raises(InvalidFileTypeError):
        normalize_markdown_file(source, workdir / "out.txt")


def test_normalize_markdown_file_returns_written_path(workdir):
    source = _write(workdir / "input.md", RAW)
    target = workdir / "out.md"
    written = normalize_markdown_file(source, target)
    assert written == target
    assert target.read_bytes() == NORMALIZED.encode("utf-8")


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path
