"""DeepSeek（Responses API + JSON Schema）语义分类 provider 适配器。

设计要点（按 GPT 的裁决）：

* 走 **Responses API**，用 **JSON Schema structured output** 约束结构
  （不用旧的 Chat Completions ``json_object``，那只保证是合法 JSON）；
* 令牌只按**环境变量名**解析，代码与日志里不出现密钥值；
* 模型只产出 ``semantic-prediction/v1`` 的 JSON 文本，解析与校验由
  ``semantic_prompt.predict_with_retry`` 负责。

用法（先做单窗试跑）：

    python semantic_provider_deepseek.py --evidence <doc.evidence.json> --window 0
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

import semantic_prompt as prompt_mod
import semantic_window as windowing


DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TOKEN_ENV = "DEEPSEEK_API_KEY"

#: 与 ``semantic-prediction/v1`` 对应的 JSON Schema（structured output 用）。
PREDICTION_JSON_SCHEMA: dict[str, Any] = {
    "name": "semantic_prediction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "window_id", "roles", "boundaries", "splits", "uncertain"],
        "properties": {
            "schema": {"type": "string", "enum": ["md-prework/semantic-prediction/v1"]},
            "window_id": {"type": "string"},
            "roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["block_ref", "role"],
                    "properties": {
                        "block_ref": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": list(prompt_mod.ROLES),
                        },
                    },
                },
            },
            "boundaries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["left_ref", "right_ref", "relation"],
                    "properties": {
                        "left_ref": {"type": "string"},
                        "right_ref": {"type": "string"},
                        "relation": {
                            "type": "string",
                            "enum": list(prompt_mod.BOUNDARY_RELATIONS),
                        },
                    },
                },
            },
            "splits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["block_ref", "anchor"],
                    "properties": {
                        "block_ref": {"type": "string"},
                        "anchor": {"type": "string"},
                    },
                },
            },
            "uncertain": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "refs", "reason"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["ROLE", "BOUNDARY", "SPLIT"]},
                        "refs": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
}


def resolve_token(explicit: str = "", token_env: str = DEFAULT_TOKEN_ENV) -> tuple[str, str]:
    """解析令牌；返回 (令牌, 来源变量名)，绝不打印值。"""

    if explicit:
        return explicit, "参数 --token"
    for name in (token_env, "DEEPSEEK_API_KEY"):
        value = os.environ.get(name) or _read_user_env(name)
        if value:
            return value, name
    raise SystemExit(
        f"未找到 DeepSeek 令牌：请设置环境变量 {token_env}"
        "（或用户级同名变量）；不要写进配置文件。"
    )


def _read_user_env(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        return str(value or "")
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class DeepSeekResponsesProvider:
    """Responses API 客户端：一次调用返回模型文本。"""

    token: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 120.0

    def __call__(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user}]},
            ],
            "text": {"format": {"type": "json_schema", **PREDICTION_JSON_SCHEMA}},
        }
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 只回状态与响应体，不带上请求头
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        return _extract_text(payload)


def _extract_text(payload: Mapping[str, Any]) -> str:
    """从 Responses API 的响应里取输出文本（兼容 output_text / output[].content[]）。"""

    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return str(payload["output_text"])
    chunks: list[str] = []
    for item in payload.get("output") or []:
        for content in (item or {}).get("content") or []:
            text = (content or {}).get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
    if not chunks:
        raise RuntimeError("响应用里没有可解析的文本输出")
    return "\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 语义分类单窗试跑")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--view", default="")
    parser.add_argument("--window", type=int, default=0, help="窗口序号（0 起）")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--token", default="")
    parser.add_argument("--output", default="", help="把预测写到这里")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    evidence_path = pathlib.Path(args.evidence)
    if args.view:
        view_path = pathlib.Path(args.view)
    else:
        import prework_paths

        view_path = prework_paths.view_path_for(evidence_path)
    document = windowing.load_document(evidence_path, view_path)
    windows = prompt_mod.with_window_id(windowing.build_windows(document)) if hasattr(
        prompt_mod, "with_window_id"
    ) else None
    if windows is None:  # with_window_id 在 semantic_prediction 里
        import semantic_prediction as prediction_mod

        windows = prediction_mod.with_window_id(windowing.build_windows(document))
    window = windows[args.window]

    token, source = resolve_token(args.token)
    print(f"[deepseek] model={args.model} token={source} window={args.window}", file=sys.stderr)
    provider = DeepSeekResponsesProvider(token=token, model=args.model)
    result = prompt_mod.predict_with_retry(provider, window, attempts=2)
    output = json.dumps(result.prediction, ensure_ascii=False, indent=2)
    if args.output:
        pathlib.Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
