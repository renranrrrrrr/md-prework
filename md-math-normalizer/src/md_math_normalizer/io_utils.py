from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import InvalidFileTypeError


def detect_newline_style(text: str) -> str:
    crlf_count = text.count("\r\n")
    lf_count = text.count("\n")
    lf_only = lf_count - crlf_count
    if crlf_count == 0:
        return "\n"
    if crlf_count >= lf_only:
        return "\r\n"
    return "\n"


def read_markdown_file(path: str | Path) -> tuple[str, bool, str]:
    p = Path(path)
    if p.suffix.lower() != ".md":
        raise InvalidFileTypeError(f"unsupported file suffix: {p.suffix}")
    raw = p.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    return text, has_bom, detect_newline_style(text)


def normalize_newlines(text: str, newline: str) -> str:
    if newline == "\r\n":
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.replace("\r\n", "\n")


def atomic_write_markdown(
    path: str | Path,
    text: str,
    *,
    has_bom: bool = False,
    newline: str = "\n",
) -> Path:
    target = Path(path)
    output_text = normalize_newlines(text, newline)
    if has_bom:
        output_bytes = b"\xef\xbb\xbf" + output_text.encode("utf-8")
    else:
        output_bytes = output_text.encode("utf-8")

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_md_norm_", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(output_bytes)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return target


def write_markdown_file(
    path: str | Path,
    text: str,
    *,
    has_bom: bool = False,
    newline: str = "\n",
) -> Path:
    p = Path(path)
    if p.suffix.lower() != ".md":
        raise InvalidFileTypeError(f"unsupported file suffix: {p.suffix}")
    return atomic_write_markdown(p, text, has_bom=has_bom, newline=newline)
