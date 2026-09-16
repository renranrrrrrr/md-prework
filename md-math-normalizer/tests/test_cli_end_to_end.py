"""End-to-end CLI acceptance: modes, exit codes, encoding and newline preservation."""

from __future__ import annotations

from pathlib import Path

import pytest

from md_math_normalizer import cli

INPUT = "1. 设 a、b、c 为正实数，且 a+b=10.\n"
NORMALIZED = "1. 设 $a, b, c$ 为正实数, 且 $a+b=10$.\n"


def _write(path: Path, text: str) -> Path:
    """Write UTF-8 with LF endings, bypassing platform newline translation."""
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["md-math-normalizer", *argv])
    return cli.main()


def test_output_mode_writes_new_markdown_file(workdir, monkeypatch):
    source = _write(workdir / "input.md", INPUT)
    target = workdir / "output.md"

    assert _run(monkeypatch, str(source), "-o", str(target)) == 0
    assert target.read_bytes() == NORMALIZED.encode("utf-8")
    assert source.read_bytes() == INPUT.encode("utf-8")  # source untouched


def test_check_mode_exit_codes(workdir, monkeypatch, capsys):
    raw = _write(workdir / "raw.md", INPUT)
    assert _run(monkeypatch, str(raw), "--check") == 1  # fixable, not yet normalized

    good = _write(workdir / "good.md", NORMALIZED)
    assert _run(monkeypatch, str(good), "--check") == 0  # already conforming

    broken = _write(workdir / "broken.md", "设 $x+1 为正数\n")
    assert _run(monkeypatch, str(broken), "--check") == 2  # cannot be fixed safely

    capsys.readouterr()


def test_in_place_replaces_file_atomically(workdir, monkeypatch):
    source = _write(workdir / "input.md", INPUT)
    assert _run(monkeypatch, str(source), "--in-place") == 0
    assert source.read_bytes() == NORMALIZED.encode("utf-8")
    assert not list(workdir.glob(".tmp_md_norm_*"))


def test_conflicting_arguments_exit_2(workdir, monkeypatch, capsys):
    source = _write(workdir / "input.md", INPUT)
    assert _run(monkeypatch, str(source), "--check", "-o", str(workdir / "x.md")) == 2
    assert _run(monkeypatch, str(source), "-o", str(workdir / "x.md"), "--in-place") == 2
    assert _run(monkeypatch, str(source)) == 2  # neither -o nor --in-place nor --check
    capsys.readouterr()


def test_non_markdown_paths_are_rejected(workdir, monkeypatch, capsys):
    source = _write(workdir / "input.txt", INPUT)
    assert _run(monkeypatch, str(source), "-o", str(workdir / "out.md")) == 2

    source_md = _write(workdir / "input.md", INPUT)
    assert _run(monkeypatch, str(source_md), "-o", str(workdir / "out.txt")) == 2
    capsys.readouterr()


def test_bom_and_crlf_are_preserved_byte_for_byte(workdir, monkeypatch):
    source = workdir / "bom.md"
    source.write_bytes(b"\xef\xbb\xbf" + INPUT.replace("\n", "\r\n").encode("utf-8"))
    target = workdir / "bom_out.md"

    assert _run(monkeypatch, str(source), "-o", str(target)) == 0
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0
    assert raw[3:].decode("utf-8") == NORMALIZED.replace("\n", "\r\n")


def test_lf_input_gets_no_bom_and_no_crlf(workdir, monkeypatch):
    source = _write(workdir / "plain.md", NORMALIZED)
    target = workdir / "plain_out.md"

    assert _run(monkeypatch, str(source), "-o", str(target)) == 0
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw == NORMALIZED.encode("utf-8")


def test_output_check_roundtrip_is_zero(workdir, monkeypatch):
    source = _write(workdir / "input.md", INPUT)
    target = workdir / "output.md"
    assert _run(monkeypatch, str(source), "-o", str(target)) == 0
    assert _run(monkeypatch, str(target), "--check") == 0
