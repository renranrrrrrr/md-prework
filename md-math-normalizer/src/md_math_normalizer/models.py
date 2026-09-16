from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class SpanKind(Enum):
    TEXT = auto()
    PROTECTED = auto()
    INLINE_MATH = auto()
    DISPLAY_MATH = auto()


class ProtectedKind(Enum):
    FENCED_CODE = auto()
    INLINE_CODE = auto()
    RAW_HTML = auto()
    URL = auto()
    MARKDOWN_LINK_DESTINATION = auto()
    MARKDOWN_IMAGE = auto()
    AUTOLINK = auto()
    ESCAPED_SEQUENCE = auto()


class Severity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: SpanKind


@dataclass(frozen=True)
class ProtectedSpan:
    start: int
    end: int
    kind: ProtectedKind


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    line: int
    column: int
    context: str


@dataclass(frozen=True)
class CheckResult:
    is_valid: bool
    needs_normalization: bool
    diagnostics: tuple[Diagnostic, ...]
    # 文本是否已经是管线认可的规范形式。is_valid=False 可能是“尚未规范化
    # 但可安全修复”，也可能是“无法安全处理”；后者由 has_fatal_error 标记。
    conforming: bool = False
    # 是否存在无法安全自动处理的问题（未闭合定界符、非法嵌套等）。
    has_fatal_error: bool = False


@dataclass(frozen=True)
class PipelineResult:
    text: str
    diagnostics: tuple[Diagnostic, ...]
