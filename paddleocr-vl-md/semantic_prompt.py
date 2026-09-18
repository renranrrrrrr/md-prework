"""Phase 5 第五段：prompt 模板 + 响应解析 + 校验 + 非法响应重试（先不接真实 API）。

顺序按 GPT 的裁决：``prompt template → 假响应解析 → schema 校验 → 重试``，
全部可离线测试；真实 provider 只在下一步接入。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from semantic_prediction import (
    BOUNDARY_RELATIONS,
    PREDICTION_SCHEMA,
    ROLES,
    PredictionError,
    validate_prediction,
)


SYSTEM_PROMPT = f"""你是 PDF→题目结构分类器。只做结构判断，不改写任何正文。

输入是一个窗口的 block 列表，每个 block 有 block_ref / label / raw_text / normalized_text /
normalization_status / page_seq / bbox / sequence_index / signals。

你必须输出 JSON（schema: {PREDICTION_SCHEMA}），字段：

- roles[]：{{"block_ref": "...", "role": "..."}}，**必须按窗口顺序逐块覆盖**，一块一条；
- boundaries[]：{{"left_ref": "...", "right_ref": "...", "relation": "..."}}，
  **必须覆盖窗口内全部相邻块**，顺序与窗口一致；
- splits[]：{{"block_ref": "...", "anchor": "..."}}，只在块内确实包含多道题时给出；
- uncertain[]：{{"kind": "ROLE|BOUNDARY|SPLIT", "refs": [...], "reason": "..."}}。

role 只能取：{", ".join(ROLES)}
boundary relation 只能取：{", ".join(BOUNDARY_RELATIONS)}

硬约束：
1. 不得改写、复述或补全正文；
2. 不得输出字符 offset，splits 只给**原文中唯一**的 anchor 片段；
3. 不得输出任何数值 confidence；不确定就显式给 UNCERTAIN；
4. roles 必须逐块覆盖，boundaries 必须覆盖全部相邻边界；
5. 页边界本身不是语义边界（跨页的同一道题仍是 SAME_PROBLEM）。

splits 的唯一正例：若同一 block 文本为「1. 甲题 2. 乙题」，则第二题起点应输出
{{"block_ref": "<该块>", "anchor": "2. 乙题"}}。
"""


class PredictionProvider(Protocol):
    def predict(self, window: Mapping[str, Any]) -> Mapping[str, Any]: ...


def build_user_message(window: Mapping[str, Any]) -> str:
    """把窗口 payload 序列化给模型（不含任何参考答案或提示性结论）。"""

    payload = {
        "window_id": window.get("window_id"),
        "window_size": window.get("window_size"),
        "blocks": window.get("blocks") or [],
    }
    return "窗口 block（JSON）：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_response(text: str) -> dict[str, Any]:
    """解析模型文本响应：去掉代码围栏后按 JSON 解析；失败抛 PredictionError。"""

    cleaned = _FENCE_RE.sub("", str(text or "").strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PredictionError(f"响应不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise PredictionError("响应必须是 JSON 对象")
    return payload


@dataclass
class AttemptRecord:
    attempt: int
    ok: bool
    error: str = ""
    raw: str = ""


@dataclass
class PromptedResult:
    prediction: dict[str, Any]
    attempts: list[AttemptRecord] = field(default_factory=list)


def predict_with_retry(
    provider: Callable[[str, str], str],
    window: Mapping[str, Any],
    *,
    attempts: int = 2,
    on_retry: Callable[[str], None] | None = None,
) -> PromptedResult:
    """调 provider 拿文本 → 解析 → 校验；非法响应重试，重试耗尽则抛错（保留原始文本）。"""

    records: list[AttemptRecord] = []
    last_error: str = ""
    last_raw: str = ""
    for attempt in range(1, max(1, attempts) + 1):
        raw = provider(SYSTEM_PROMPT, build_user_message(window))
        last_raw = raw
        try:
            prediction = parse_response(raw)
            validate_prediction(prediction, window)
        except PredictionError as exc:
            last_error = str(exc)
            records.append(AttemptRecord(attempt=attempt, ok=False, error=last_error, raw=raw))
            if attempt < max(1, attempts) and on_retry:
                on_retry(f"[重试] 第 {attempt} 次响应不合法：{last_error}")
            continue
        records.append(AttemptRecord(attempt=attempt, ok=True, raw=raw))
        return PromptedResult(prediction=prediction, attempts=records)
    raise PredictionError(f"响应在 {max(1, attempts)} 次尝试后仍不合法：{last_error}")
