"""远端服务压力的统一分类（pdf2md 与并发调度器共用的唯一真源）。

背景：PaddleOCR / AI Studio 是共享服务，高峰期会返回三类东西：

  1. HTTP 400 + 业务码 10010「任务提交队列已满」——**临时**，值得等一会再试
  2. HTTP 400 + 业务码 12002「请求频率过高」——**临时**，值得退避
  3. HTTP 429 / 503 / 504——**临时**，标准限流与过载

还有一种**不是**临时限流的情况：当日额度耗尽（`今日...已达上限` 之类）。它重试也没用，
必须与临时限流分开，否则调度器会一直冷却后继续撞墙。

分类依据来自实测的异常文本。paddleocr-mcp 会把异常类型包一层丢掉，但 HTTP 状态码与
服务端消息保留在字符串里，例如：

    RateLimitError            → "Execution failed: HTTP 429: 任务提交队列已满，请稍后重试"
    ServiceUnavailableError   → "Service unavailable: HTTP 503: service busy"
    InvalidRequestError(10010)→ "Execution failed: Bad request: 任务提交队列已满，请稍后重试"

因此这里同时匹配：HTTP 状态码、业务错误码、以及服务端中文/英文短语。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 分类常量

REMOTE_BACKPRESSURE = "REMOTE_BACKPRESSURE"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
OCR_FAILURE = "OCR_FAILURE"
PIPELINE_FAILURE = "PIPELINE_FAILURE"
LOCAL_ENVIRONMENT = "LOCAL_ENVIRONMENT"
NONE = "NONE"

# 细分原因（只用于汇总，不参与决策）
REASON_QUEUE_FULL = "queue_full"
REASON_RATE_LIMIT = "rate_limit"
REASON_HTTP_429 = "http_429"
REASON_HTTP_503 = "http_503"
REASON_QUOTA = "quota_exhausted"
REASON_OTHER = "other"

# ---------------------------------------------------------------- 匹配规则

# HTTP 状态码：用边界避免把 "5030" 之类当成 503
_HTTP_429_RE = re.compile(r"\bhttp[\s_\-]?429\b|\bstatus[\s_:=]*429\b", re.IGNORECASE)
_HTTP_503_RE = re.compile(r"\bhttp[\s_\-]?(?:503|504)\b|\bstatus[\s_:=]*(?:503|504)\b", re.IGNORECASE)

# 业务错误码（Paddle 服务端返回，实测以 HTTP 400 + code 形式出现）
_CODE_10010_RE = re.compile(r"\b10010\b")
_CODE_12002_RE = re.compile(r"\b12002\b")

# 临时性服务压力短语（中英）
_BACKPRESSURE_PHRASES = (
    "队列已满",
    "请求频率过高",
    "请求过于频繁",
    "访问频率",
    "服务繁忙",
    "服务繁忙请稍后",
    "稍后重试",
    "too many requests",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "server busy",
    "service busy",
    "service unavailable",
    "temporarily unavailable",
    "queue is full",
    "queue full",
)

# 额度耗尽短语：**不是**临时限流，重试无意义
_QUOTA_PHRASES = (
    "额度已耗尽",
    "额度耗尽",
    "余额不足",
    "配额已用完",
    "配额用尽",
    "今日免费额度",
    "quota exhausted",
    "quota exceeded",
    "out of quota",
    "insufficient quota",
    "no remaining quota",
)

# 当日次数上限也算“用完了”，但必须带“今日/当天/每日”限定，
# 否则 "已达上限" 会被误判（队列满的提示里也可能出现“上限”）。
_DAILY_LIMIT_RE = re.compile(
    r"(今日|当天|本日|每日|today|daily)[^。\n]{0,20}?(已达|超过|用完|用尽|上限|limit)"
    r"|(已达|超过|用完|用尽)[^。\n]{0,10}?(今日|当天|每日)(上限|限额|额度)",
    re.IGNORECASE,
)

# 非远端压力类的本地/环境信号（仅用于更好的分类，不影响 cooldown 决策）
_LOCAL_ENV_PHRASES = (
    "找不到 md-math-normalizer",
    "缺少依赖",
    "no module named",
    "modulenotfounderror",
    "找不到 python 解释器",
    "不是 .pdf",
)


@dataclass(frozen=True)
class BackpressureVerdict:
    kind: str
    reason: str
    evidence: str


def _find(phrases, lowered: str) -> str | None:
    for phrase in phrases:
        if phrase.lower() in lowered:
            return phrase
    return None


def classify_text(text: str) -> BackpressureVerdict:
    """对一段日志文本分类。

    顺序很重要：**先判额度耗尽**，再判临时限流。因为“今日提交任务已达上限”里同时
    含“上限”（旧实现会当限流重试），但它其实重试无用。
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return BackpressureVerdict(NONE, REASON_OTHER, "")

    # 1) 额度耗尽（永久，不触发 cooldown）
    quota = _find(_QUOTA_PHRASES, lowered)
    if quota:
        return BackpressureVerdict(QUOTA_EXHAUSTED, REASON_QUOTA, quota)
    daily = _DAILY_LIMIT_RE.search(text or "")
    if daily:
        return BackpressureVerdict(QUOTA_EXHAUSTED, REASON_QUOTA, daily.group(0))

    # 2) 临时性服务压力
    if _HTTP_429_RE.search(text or ""):
        return BackpressureVerdict(REMOTE_BACKPRESSURE, REASON_HTTP_429, "HTTP 429")
    if _HTTP_503_RE.search(text or ""):
        return BackpressureVerdict(REMOTE_BACKPRESSURE, REASON_HTTP_503, "HTTP 503/504")
    if _CODE_10010_RE.search(text or ""):
        return BackpressureVerdict(REMOTE_BACKPRESSURE, REASON_QUEUE_FULL, "code 10010")
    if _CODE_12002_RE.search(text or ""):
        return BackpressureVerdict(REMOTE_BACKPRESSURE, REASON_RATE_LIMIT, "code 12002")

    phrase = _find(_BACKPRESSURE_PHRASES, lowered)
    if phrase:
        reason = REASON_QUEUE_FULL if "队列" in phrase or "queue" in phrase else REASON_RATE_LIMIT
        return BackpressureVerdict(REMOTE_BACKPRESSURE, reason, phrase)

    # 3) 本地环境问题
    env = _find(_LOCAL_ENV_PHRASES, lowered)
    if env:
        return BackpressureVerdict(LOCAL_ENVIRONMENT, REASON_OTHER, env)

    return BackpressureVerdict(NONE, REASON_OTHER, "")


def classify_failure(text: str) -> BackpressureVerdict:
    """对失败任务的输出分类（空文本或未命中即返回 NONE）。"""
    return classify_text(text)


def should_cool_down(verdict: BackpressureVerdict) -> bool:
    """只有**临时**服务压力才触发调度器冷却；额度耗尽不触发。"""
    return verdict.kind == REMOTE_BACKPRESSURE


# ---------------------------------------------------------------- pdf2md 侧复用

def is_retryable_remote_error(exc: BaseException) -> bool:
    """pdf2md 的 OCR 重试判断：临时远端压力才重试。

    比旧的关键词表更准：覆盖 HTTP 429/503/504 与业务码，且**排除**额度耗尽
    （重试无意义）与本地错误。
    """
    verdict = classify_text(str(exc))
    return verdict.kind == REMOTE_BACKPRESSURE
