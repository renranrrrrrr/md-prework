from __future__ import annotations

from pathlib import Path

from .config import SUPPORTED_INPUT_SUFFIX
from .errors import InvalidFileTypeError, ValidationError
from .io_utils import read_markdown_file, write_markdown_file
from .models import CheckResult, Diagnostic, Severity
from .pipeline import normalize_pipeline
from .validator import validate_markdown_text


def _ensure_md_suffix(path: str | Path) -> None:
    p = Path(path)
    if p.suffix.lower() not in SUPPORTED_INPUT_SUFFIX:
        raise InvalidFileTypeError(f"invalid input suffix: {p.suffix}")


def _validation_diagnostic(message: str) -> tuple[Diagnostic]:
    return (
        Diagnostic(
            code="ERROR_VALIDATION_FAILED",
            severity=Severity.ERROR,
            message=message,
            line=1,
            column=1,
            context="",
        ),
    )


def normalize_markdown_text(text: str) -> str:
    result = normalize_pipeline(text)
    return result.text


def normalize_markdown_file(
    input_path: str | Path,
    output_path: str | Path,
) -> Path:
    _ensure_md_suffix(input_path)
    _ensure_md_suffix(output_path)
    text, has_bom, newline = read_markdown_file(input_path)
    result = normalize_pipeline(text)
    return write_markdown_file(
        output_path,
        result.text,
        has_bom=has_bom,
        newline=newline,
    )


def check_markdown_file(input_path: str | Path) -> CheckResult:
    """Inspect a file without modifying it.

    ``is_valid`` reports whether the text still needs normalizing, and
    ``needs_normalization`` distinguishes "already conforming" from "conforming
    after a safe automatic pass", which drives the CLI exit codes 0/1/2. A file
    whose structure cannot be normalized safely is reported as invalid with the
    underlying diagnostics.
    """
    _ensure_md_suffix(input_path)
    text, _, _ = read_markdown_file(input_path)

    current = validate_markdown_text(text)
    try:
        # 非 strict：允许输入尚未规范化，只需要回答“能否安全修复”。
        normalized = normalize_pipeline(text, strict=False).text
    except ValidationError as exc:
        return CheckResult(
            False,
            False,
            _validation_diagnostic(str(exc)) + current.diagnostics,
            conforming=False,
            has_fatal_error=True,
        )

    needs_normalization = normalized != text
    return CheckResult(
        is_valid=current.is_valid,
        needs_normalization=needs_normalization,
        diagnostics=current.diagnostics,
        conforming=not needs_normalization,
        has_fatal_error=False,
    )
