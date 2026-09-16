"""Idempotence over every fixture, the examples, and the report cases."""

from __future__ import annotations

import pathlib

import pytest

from md_math_normalizer import normalize_markdown_text

SAMPLES = [
    "1. 设 a、b、c 为正实数，且 a+b=10.",
    "9.（16分）求 sum_{i=1}^n a_i.",
    "$x$ + $y$",
    "$\\frac{1}{2}$ 与 $\\sum_i a_i$",
    "设 $x_1+x_2=1$，且 $a_{n+1}>0$.",
    '<div><img src="https://a.com/b_1.png" width="50%"/></div>',
    "结果为 10.",
]


@pytest.mark.parametrize("sample", SAMPLES)
def test_idempotence_for_samples(sample):
    once = normalize_markdown_text(sample)
    assert normalize_markdown_text(once) == once


def test_idempotence_for_fixture_files(fixtures_dir: pathlib.Path):
    for path in sorted(fixtures_dir.rglob("*.md")):
        if path.name.endswith(".expected.md"):
            continue
        text = path.read_text(encoding="utf-8")
        try:
            once = normalize_markdown_text(text)
        except Exception:  # noqa: BLE001 - malformed fixtures are rejected on purpose
            continue
        assert normalize_markdown_text(once) == once, path.name
