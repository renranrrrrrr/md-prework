from __future__ import annotations

import pathlib

import pytest

FIXTURE_DIRS = ("basic", "html", "math", "regression", "malformed")
ERROR_DIRS = ("malformed",)

# Fixtures whose whole purpose is to be rejected by the pipeline.
EXPECTED_ERROR_STEMS = {"malformed_1", "malformed_nested"}


def _pairs(fixtures_dir: pathlib.Path):
    """Collect (input, expected) pairs; ``<stem>.expected.md`` supplies the expectation."""
    for name in FIXTURE_DIRS:
        directory = fixtures_dir / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.name.endswith(".expected.md"):
                continue
            if path.stem in EXPECTED_ERROR_STEMS:
                yield pytest.param(path, None, id=f"{name}-{path.stem}-error")
                continue
            expected = path.with_name(f"{path.stem}.expected.md")
            if not expected.is_file():
                raise AssertionError(f"fixture {path} has no {expected.name}")
            yield pytest.param(path, expected, id=f"{name}-{path.stem}")


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_cases(fixtures_dir: pathlib.Path):
    """Every fixture file, paired with its expectation when it has one."""
    cases = list(_pairs(fixtures_dir))
    if not cases:
        pytest.fail("no fixtures were collected")
    return cases
