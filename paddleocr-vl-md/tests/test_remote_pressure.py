"""远端服务压力分类的回归测试。

全部使用实测到的真实错误文本形态，不访问任何远端服务。
真实形态来源：
    RateLimitError(429)         → "Execution failed: HTTP 429: 任务提交队列已满，请稍后重试"
    ServiceUnavailableError(503)→ "Service unavailable: HTTP 503: service busy"
    InvalidRequestError(10010)  → "Execution failed: Bad request: 任务提交队列已满，请稍后重试"
    InvalidRequestError(12002)  → "Execution failed: Bad request: 今日提交任务已达上限"
"""

from __future__ import annotations

import pathlib
import sys

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import remote_pressure as rp  # noqa: E402

BACKPRESSURE_TEXTS = [
    # 业务码 10010（实测：队列已满，HTTP 400 + code）
    ("HTTP 400: code 10010 任务提交队列已满", rp.REASON_QUEUE_FULL),
    ("  [失败] InvalidRequestError: Bad request: 任务提交队列已满，请稍后重试", rp.REASON_QUEUE_FULL),
    # 业务码 12002（请求频率过高）
    ("HTTP 400: code 12002 请求频率过高", rp.REASON_RATE_LIMIT),
    ("Bad request: 请求过于频繁，请降低请求频率", rp.REASON_RATE_LIMIT),
    # HTTP 429
    ("Execution failed: HTTP 429: 任务提交队列已满，请稍后重试", rp.REASON_HTTP_429),
    # 只有英文短语、没有状态码时归为 rate_limit（不是 http_429）
    ("Rate limit exceeded: too many requests", rp.REASON_RATE_LIMIT),
    # HTTP 503 / 504
    ("Service unavailable: HTTP 503: service busy", rp.REASON_HTTP_503),
    ("Service unavailable: HTTP 504: gateway timeout", rp.REASON_HTTP_503),
    ("HTTP 503 Service Temporarily Unavailable", rp.REASON_HTTP_503),
]

QUOTA_TEXTS = [
    "Bad request: 今日提交任务已达上限，请明日再试",
    "今日免费额度已用完",
    "quota exhausted: daily limit reached",
    "余额不足，请充值",
]

NON_BACKPRESSURE_TEXTS = [
    # 普通 OCR 失败
    "  [失败] 未识别到内容：扫描件.pdf",
    "ERROR_UNBALANCED_INLINE_MATH unclosed inline math at line 1:5",
    "Job abc-123 failed: page 3 unreadable",
    # Markdown check 失败
    "ERROR_NESTED_MATH_ENVIRONMENT misplaced closing delimiter",
    "result: needs normalization",
    # 本地参数/环境问题
    "[输入] 路径不存在：D:\\nope\\missing.pdf",
    "错误：--workers 必须是正整数，收到 0",
    "找不到 md-math-normalizer。请把它放在与本工具同级的目录下",
    "缺少依赖 paddleocr_mcp，请先安装：paddleocr-mcp",
    # 鉴权失败（不是限流）
    "Authentication failed: token expired",
    # 空文本
    "",
]


@pytest.mark.parametrize(("text", "reason"), BACKPRESSURE_TEXTS)
def test_backpressure_texts_are_detected(text, reason):
    verdict = rp.classify_text(text)
    assert verdict.kind == rp.REMOTE_BACKPRESSURE, text
    assert verdict.reason == reason, text
    assert verdict.evidence, text
    assert rp.should_cool_down(verdict) is True


@pytest.mark.parametrize("text", QUOTA_TEXTS)
def test_quota_exhausted_is_not_backpressure(text):
    verdict = rp.classify_text(text)
    assert verdict.kind == rp.QUOTA_EXHAUSTED, text
    assert verdict.reason == rp.REASON_QUOTA
    # 额度耗尽重试无意义：不得触发 cooldown
    assert rp.should_cool_down(verdict) is False


@pytest.mark.parametrize("text", NON_BACKPRESSURE_TEXTS)
def test_non_backpressure_texts_are_not_misclassified(text):
    verdict = rp.classify_text(text)
    assert verdict.kind != rp.REMOTE_BACKPRESSURE, text
    assert rp.should_cool_down(verdict) is False, text


def test_quota_detection_wins_over_queue_full_wording():
    """“今日提交任务已达上限”含“上限”字样，但必须归为额度耗尽而非临时限流。"""
    verdict = rp.classify_text("Bad request: 今日提交任务已达上限")
    assert verdict.kind == rp.QUOTA_EXHAUSTED


def test_status_code_numbers_are_not_partially_matched():
    """5030 之类的数字不得被当成 503。"""
    assert rp.classify_text("HTTP 400: code 50301").kind != rp.REMOTE_BACKPRESSURE


def test_retryable_helper_covers_429_and_503():
    assert rp.is_retryable_remote_error(RuntimeError("HTTP 429: rate limit")) is True
    assert rp.is_retryable_remote_error(RuntimeError("HTTP 503: busy")) is True
    assert rp.is_retryable_remote_error(RuntimeError("code 10010 队列已满")) is True
    # 额度耗尽不重试
    assert rp.is_retryable_remote_error(RuntimeError("今日提交任务已达上限")) is False
    # 普通错误不重试
    assert rp.is_retryable_remote_error(RuntimeError("file not found")) is False
    assert rp.is_retryable_remote_error(RuntimeError("Authentication failed")) is False


def test_classifier_is_shared_single_source_of_truth():
    """分类逻辑只有一份：OCR 核心复用 remote_pressure，前端不得再维护关键词表。

    合并两套 producer 之后，唯一的 OCR 实现是 ``ocr_producer.py``；
    ``pdf2md.py`` / ``prework_ocr.py`` 只是 CLI 前端，必须委托给它。
    """

    core = (TOOL_DIR / "ocr_producer.py").read_text(encoding="utf-8")
    assert "remote_pressure" in core, "ocr_producer.py 应复用 remote_pressure 的分类"

    prework = (TOOL_DIR / "prework_ocr.py").read_text(encoding="utf-8")
    assert "ocr_producer" in prework, "prework_ocr.py 必须委托给唯一的核心实现"

    legacy = (TOOL_DIR / "pdf2md.py").read_text(encoding="utf-8")
    assert "ocr_producer" in legacy, "pdf2md.py 必须委托给唯一的核心实现"

    for module in ("ocr_producer.py", "prework_ocr.py", "pdf2md.py"):
        source = (TOOL_DIR / module).read_text(encoding="utf-8")
        assert "_RATE_LIMIT_HINTS" not in source, f"{module} 不应再维护模糊关键词表"
        # 关键词表只能出现在 remote_pressure.py 里
        assert "任务提交队列已满" not in source, f"{module} 不应复制远端压力关键词表"
        assert "今日提交任务已达上限" not in source, f"{module} 不应复制额度耗尽关键词表"
