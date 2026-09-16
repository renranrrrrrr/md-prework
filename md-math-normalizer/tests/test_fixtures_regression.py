"""Fixture-driven regression suite: every fixture in tests/fixtures is exercised."""

from __future__ import annotations

import pathlib

import pytest

from md_math_normalizer import normalize_markdown_text
from md_math_normalizer.errors import NormalizationError
from md_math_normalizer.validator import validate_markdown_text


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_every_fixture_is_covered(fixture_cases, fixtures_dir):
    on_disk = {
        path
        for name in ("basic", "html", "math", "regression", "malformed")
        for path in (fixtures_dir / name).glob("*.md")
        if not path.name.endswith(".expected.md")
    }
    covered = {case.values[0] for case in fixture_cases}
    assert on_disk == covered


@pytest.mark.parametrize("case", pytest.param(None, id="fixture-cases"))
def test_fixtures_match_expected_output(case, fixture_cases):
    for item in fixture_cases:
        source_path, expected_path = item.values
        source = _read(source_path)

        if expected_path is None:
            with pytest.raises(NormalizationError) as excinfo:
                normalize_markdown_text(source)
            assert str(excinfo.value).startswith("ERROR_"), source_path.name
            continue
        expected = _read(expected_path)
        assert normalize_markdown_text(source) == expected, source_path.name
        assert normalize_markdown_text(expected) == expected, expected_path.name
        assert validate_markdown_text(expected).is_valid, expected_path.name


@pytest.mark.parametrize("case", pytest.param(None, id="fixture-cases"))
def test_fixtures_are_idempotent(case, fixture_cases):
    for item in fixture_cases:
        source_path, expected_path = item.values
        if expected_path is None:
            continue
        once = normalize_markdown_text(_read(source_path))
        assert normalize_markdown_text(once) == once, source_path.name


def test_examples_expected_is_a_fixed_point(fixtures_dir):
    repo_root = fixtures_dir.parent.parent
    expected_path = repo_root / "examples" / "expected.md"
    expected = _read(expected_path)
    assert normalize_markdown_text(expected) == expected
    assert validate_markdown_text(expected).is_valid


def test_examples_input_normalizes_to_expected(fixtures_dir):
    repo_root = fixtures_dir.parent.parent
    source = _read(repo_root / "examples" / "input.md")
    expected = _read(repo_root / "examples" / "expected.md")
    assert normalize_markdown_text(source) == expected
