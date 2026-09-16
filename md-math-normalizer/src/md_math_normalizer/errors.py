class MarkdownNormalizerError(Exception):
    """Base error for md_math_normalizer."""


class NormalizationError(MarkdownNormalizerError):
    """Raised when normalization cannot be completed safely."""


class ValidationError(NormalizationError):
    """Raised when result violates invariant constraints."""


class UnsafeInputError(NormalizationError):
    """Raised for malformed/ambiguous input that must not be auto-fixed."""


class InvalidFileTypeError(NormalizationError):
    """Raised for invalid file path/suffix."""

