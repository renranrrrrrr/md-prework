"""PaddleOCR-VL 唯一 producer 的核心实现（md-prework 的 OCR Producer 层）。

职责边界（本层红线）：

- 只做"调用外部 OCR + 保存证据与产物"：**不切题、不调 LLM、不写题库、不建索引**；
- 原始证据生成后**不可修改**：``evidence.json`` 已存在时默认拒绝重跑（除非显式 ``--overwrite``），
  规范化视图、切题结果都只能是派生结果，不能回写证据；
- 图片严格按文件头校验；下载失败**不伪造本地路径**；
- 远端压力分类只保留一份真源：``remote_pressure.py``。

两个 CLI（``prework_ocr.py`` 与 ``pdf2md.py``）都调用本模块，不复制任何 OCR 逻辑。

证据粒度来自真实 capability probe（见 ``capability/README.md``）：
``prunedResult.parsing_res_list`` 提供 block_id / block_order / block_label / block_content /
block_bbox / block_polygon_points / group_id；``block_order`` 会为 null，因此阅读顺序以
**列表顺序**为准并另存 ``reading_order``。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from remote_pressure import classify_text, is_retryable_remote_error


PRODUCER_NAME = "prework-ocr"
PRODUCER_VERSION = "prework-ocr/v1"
EVIDENCE_SCHEMA_VERSION = "md-prework/ocr-evidence/v1"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"
DEFAULT_PROVIDER = "aistudio"

#: 令牌环境变量名（按顺序解析；两个名字并存期都要能工作）。
DEFAULT_TOKEN_ENVS = ("PADDLEOCR_API_KEY", "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN")

EXIT_OK = 0
EXIT_ITEM_FAILED = 1
EXIT_ENVIRONMENT = 2
EXIT_INTERRUPTED = 130

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

_DATA_URL_PREFIXES = ("data:image/", "data:application/")

_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
)


class EvidenceExistsError(RuntimeError):
    """证据已存在：原始证据不可修改，必须显式 --overwrite 才能重跑。"""


# ------------------------------------------------------------------ 令牌


def _read_user_env(name: str) -> str:
    """用户级环境变量兜底（刚设置的用户变量可能还没进入当前进程）。"""

    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value or "")


def resolve_token(
    explicit: str = "",
    token_envs: Sequence[str] = DEFAULT_TOKEN_ENVS,
) -> tuple[str, str]:
    """返回 (令牌, 来源说明)；来源说明只写变量名，绝不写值。"""

    if explicit:
        return explicit, "参数 --token"
    for name in token_envs:
        if not name:
            continue
        value = os.environ.get(name)
        if value:
            return value, f"进程环境变量 {name}"
        value = _read_user_env(name)
        if value:
            return value, f"用户级环境变量 {name}"
    names = " 或 ".join(token_envs)
    raise EnvironmentError(f"未找到令牌：请设置环境变量 {names}，不要写进配置文件。")


# ------------------------------------------------------------------ 数据模型


@dataclass(frozen=True)
class BlockEvidence:
    """Stable Core 里的一个版面块。

    命名遵循"事实与解释分离"：

    - ``sequence_index``：块在 ``parsing_res_list`` 数组里的真实位置（事实）；
    - ``provider_block_order``：Paddle 显式给出的 ``block_order``，**可为 null**（事实）；
    - ``reading_order``：留给语义层后续派生的解释字段，证据层不写。
    """

    provider_block_id: Any
    provider_block_order: Any
    sequence_index: int
    label: str
    content: str
    bbox: Any = None
    polygon: Any = None
    group_id: Any = None

    def to_dict(self, *, page_seq: int) -> dict[str, Any]:
        return {
            "block_ref": f"p{page_seq:04d}:b{self.sequence_index:04d}",
            "provider_block_id": self.provider_block_id,
            "provider_block_order": self.provider_block_order,
            "sequence_index": self.sequence_index,
            "label": self.label,
            "content": self.content,
            "bbox": self.bbox,
            "polygon": self.polygon,
            "group_id": self.group_id,
            "content_sha256": sha256_text(self.content),
        }


@dataclass(frozen=True)
class PageEvidence:
    """一页的结构化证据（Stable Core 部分）。"""

    page_seq: int
    width: int | None
    height: int | None
    markdown_text: str
    images: Mapping[str, str] = field(default_factory=dict)
    output_images: Mapping[str, Any] = field(default_factory=dict)
    input_image_url: str = ""
    blocks: tuple[BlockEvidence, ...] = ()
    layout_boxes_count: int = 0
    preprocessor_applied: bool = False
    raw_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_seq": self.page_seq,
            "page_index_source": "response_sequence",
            "coordinate_space": {
                "kind": "preprocessed_page" if self.preprocessor_applied else "provider_page",
                "width": self.width,
                "height": self.height,
            },
            "layout_detection": {"boxes_count": self.layout_boxes_count},
            "order_consistency": order_consistency(self.blocks),
            "markdown_text": self.markdown_text,
            "images": {str(key): _redact_remote_value(value) for key, value in self.images.items()},
            "output_images": {
                str(key): _redact_remote_value(value) for key, value in self.output_images.items()
            },
            "input_image_url": _redact_remote_value(self.input_image_url),
            "raw_ref": self.raw_ref,
            "blocks": [block.to_dict(page_seq=self.page_seq) for block in self.blocks],
        }


def order_consistency(blocks: Sequence[BlockEvidence]) -> dict[str, Any]:
    """块顺序诊断：数组顺序是 canonical，``block_order`` 只作提示与校验。"""

    orders = [block.provider_block_order for block in blocks]
    present = [value for value in orders if value is not None]
    conflicts = 0
    monotonic = True
    previous: int | None = None
    for value in present:
        current = _optional_int(value)
        if current is None:
            continue
        if previous is not None and current < previous:
            monotonic = False
            conflicts += 1
        previous = current
        position = orders.index(value)
        if previous is not None and position < 0:
            conflicts += 1
    return {
        "ordered_blocks": len(blocks),
        "block_order_present": len(present),
        "block_order_monotonic": bool(monotonic),
        "block_order_conflicts": int(conflicts),
    }


@dataclass(frozen=True)
class DocumentResult:
    """一次文档识别的完整结果。

    ``pages`` 是 Stable Core 需要的结构化证据；``raw_pages`` 是逐页 provider
    原始 ``prunedResult``，只用于写 sidecar 文件，**不会**内嵌进证据主文件。
    """

    markdown: str
    pages: tuple[PageEvidence, ...]
    envelope_keys: tuple[str, ...] = ()
    model_settings: Mapping[str, Any] = field(default_factory=dict)
    raw_pages: tuple[Mapping[str, Any] | None, ...] = ()
    raw_envelope: Any = None

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def images_mapping(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for page in self.pages:
            merged.update(dict(page.images))
        return merged


@dataclass
class ConvertOutcome:
    """单个 PDF 的处理结果（同时作为 ocr_status.jsonl 的一行）。"""

    pdf: str
    status: str = STATUS_FAILED
    reason: str = ""
    error: str = ""
    markdown: str = ""
    evidence: str = ""
    stdout_markdown: bool = False
    pages: int = 0
    chars: int = 0
    blocks: int = 0
    images_total: int = 0
    images_saved: int = 0
    media_dir: str = ""
    missing_images: list[str] = field(default_factory=list)
    image_failures: list[str] = field(default_factory=list)
    model: str = ""
    token_source: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf": self.pdf,
            "status": self.status,
            "reason": self.reason,
            "error": self.error,
            "markdown": self.markdown,
            "evidence": self.evidence,
            "stdout_markdown": self.stdout_markdown,
            "pages": self.pages,
            "chars": self.chars,
            "blocks": self.blocks,
            "images_total": self.images_total,
            "images_saved": self.images_saved,
            "media_dir": self.media_dir,
            "missing_images": list(self.missing_images),
            "image_failures": list(self.image_failures),
            "model": self.model,
            "token_source": self.token_source,
            "duration_seconds": round(self.duration_seconds, 3),
        }


# ------------------------------------------------------------------ 工具函数


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_remote_value(value: Any) -> Any:
    """远端 URL / data URL 只保留占位与摘要，不把可点击的临时链接写进证据。"""

    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.lower().startswith(("http://", "https://", "data:")):
        return f"<<remote:{sha256_text(stripped)[:16]}>>"
    return value


def detect_image_format(data: bytes) -> str:
    """按文件头判断图片格式，识别不出来返回空串。"""

    for signature, name in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return name
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    return ""


def decode_image_payload(
    payload: str,
    *,
    downloader: Callable[[str], bytes] | None = None,
) -> bytes:
    """把图片负载还原成字节：远端 URL 走下载，base64 / data URL 直接解码。"""

    data = str(payload).strip()
    if data.lower().startswith(("http://", "https://")):
        return (downloader or download_image)(data)
    if data.lower().startswith(_DATA_URL_PREFIXES) and "," in data:
        data = data.split(",", 1)[1]
    compact = "".join(data.split())
    padded = compact + "=" * (-len(compact) % 4)
    return base64.b64decode(padded)


def download_image(
    url: str,
    *,
    timeout: float = 120.0,
    attempts: int = 3,
    delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[str], None] | None = None,
) -> bytes:
    """下载图片 URL，失败按指数退避重试（CDN 抖动不该变成永久缺图）。"""

    last_error: Exception | None = None
    wait = delay
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": f"{PRODUCER_NAME}/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            if on_retry:
                on_retry(
                    f"[重试] 图片下载失败（第 {attempt}/{attempts - 1} 次）：{exc}；{wait:.0f}s 后重试"
                )
            sleep(wait)
            wait *= 2
    raise OSError(f"下载失败（已重试 {attempts} 次）：{last_error}")


def safe_relative_path(name: str) -> pathlib.PurePosixPath:
    """把图片键名规整成安全的相对路径（去掉绝对前缀与 ..）。"""

    relative = pathlib.PurePosixPath(str(name).replace("\\", "/").lstrip("/"))
    return pathlib.PurePosixPath(*[part for part in relative.parts if part not in ("", "..")])


def materialize_images(
    images: Mapping[str, str],
    media_dir: pathlib.Path,
    *,
    keep: bool = True,
    downloader: Callable[[str], bytes] | None = None,
    on_retry: Callable[[str], None] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]], list[str]]:
    """导出图片到 ``media_dir``，返回 (src 映射, 素材登记, 失败说明)。

    素材登记逐张记录 ``state``：``saved`` / ``missing`` / ``remote``，
    供 evidence 与汇总使用；拿不到文件时**绝不**写不存在本地路径。
    """

    mapping: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    failures: list[str] = []

    for name, payload in images.items():
        relative = safe_relative_path(name)
        record: dict[str, Any] = {
            "src": str(name),
            "relative": str(relative),
            "kind": "url" if str(payload).lower().startswith(("http://", "https://")) else "inline",
        }
        if not relative.name:
            record["state"] = "missing"
            record["detail"] = "图片键名无效"
            records.append(record)
            failures.append(f"图片键名无效，已跳过：{name!r}")
            continue
        if not keep:
            record["state"] = "remote"
            records.append(record)
            continue
        try:
            blob = decode_image_payload(payload, downloader=downloader)
        except Exception as exc:  # noqa: BLE001 - 单张图片失败不应中断文档
            record["state"] = "missing"
            record["detail"] = f"{type(exc).__name__}: {exc}"
            records.append(record)
            failures.append(f"图片 {name} 获取失败：{type(exc).__name__}: {exc}")
            continue
        image_format = detect_image_format(blob)
        if not image_format:
            record["state"] = "missing"
            record["detail"] = f"不是可识别图片（{len(blob)} 字节）"
            records.append(record)
            failures.append(f"图片 {name} 内容不是可识别图片（{len(blob)} 字节），已跳过")
            continue
        expected_suffix = "." + image_format.lower().replace("jpeg", "jpg")
        if relative.suffix.lower() != expected_suffix:
            relative = relative.with_suffix(expected_suffix)
        target = media_dir.joinpath(*relative.parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        except OSError as exc:
            record["state"] = "missing"
            record["detail"] = f"写入失败：{exc}"
            records.append(record)
            failures.append(f"图片 {name} 写入失败：{exc}")
            continue
        stored = str(pathlib.PurePosixPath(media_dir.name) / relative)
        mapping[str(name)] = stored
        record.update(
            {
                "state": "saved",
                "file": stored,
                "format": image_format,
                "bytes": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
        records.append(record)
    return mapping, records, failures


def normalize_block_math(markdown: str) -> str:
    """去掉每行行尾空白（``$$ ... $$ `` 的行尾空格会让渲染器认不出块级公式）。"""

    return "\n".join(line.rstrip() for line in markdown.split("\n"))


_IMG_SRC_RE = re.compile(r'(?P<prefix><img[^>]*?src=")(?P<src>[^"]+)(?P<suffix>")')


def rewrite_image_sources(markdown: str, mapping: Mapping[str, str]) -> str:
    """把 Markdown 里的图片 src 重写成实际落盘位置（只动 src 取值）。"""

    if not mapping:
        return markdown

    def _replace(match: re.Match[str]) -> str:
        replacement = mapping.get(match.group("src"))
        if replacement is None:
            return match.group(0)
        return f'{match.group("prefix")}{replacement}{match.group("suffix")}'

    return _IMG_SRC_RE.sub(_replace, markdown)


def find_missing_image_sources(markdown: str, base_dir: pathlib.Path) -> list[str]:
    """列出「还没有本地化」的 img src，判定按 Markdown 渲染器的心智模型。

    * ``http(s)://`` → 远端引用，说明没本地化；
    * ``data:`` URL → 已经内嵌在 markdown 里，不算缺失；
    * 其它一律当本地路径 → 相对 ``base_dir``（markdown 所在目录）解析，文件不存在才算缺失。

    返回原始 src 文本，按出现顺序去重；不依赖任何映射表，因此对"图片没下载成功"和
    "本地文件被别人删了"两种情况都成立。
    """

    missing: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'<img[^>]*?src="([^"]+)"', markdown):
        source = match.group(1).strip()
        if not source or source in seen:
            continue
        lowered = source.lower()
        if lowered.startswith("data:"):
            continue
        if lowered.startswith(("http://", "https://")):
            seen.add(source)
            missing.append(source)
            continue
        if not (base_dir / source.replace("/", os.sep)).is_file():
            seen.add(source)
            missing.append(source)
    return missing


def find_broken_image_sources(markdown: str, mapping: Mapping[str, str]) -> list[str]:
    """兼容旧名：只报"仍在映射表之外的远端引用"。"""

    broken: list[str] = []
    for match in re.finditer(r'<img[^>]*?src="([^"]+)"', markdown):
        source = match.group(1)
        if source in mapping:
            continue
        if source.lower().startswith(("http://", "https://")):
            broken.append(source)
    return broken


# ------------------------------------------------------------------ SDK 适配


async def predict_with_retry(
    call: Callable[[], Any],
    *,
    attempts: int = 3,
    delay: float = 10.0,
    sleep: Callable[[float], Any] = asyncio.sleep,
    on_retry: Callable[[str], None] | None = None,
) -> Any:
    """单次推理的重试：只重试**临时**远端压力，额度耗尽/本地错误直接抛。

    这与调度器的 cooldown 职责不同：调度器只决定"何时提交下一份 PDF"。
    """

    wait = delay
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 - 需要按错误文本判断
            if attempt >= attempts or not is_retryable_remote_error(exc):
                raise
            if on_retry:
                on_retry(
                    f"[重试] 第 {attempt}/{attempts - 1} 次遇到服务端压力：{exc}；{wait:.0f}s 后重试"
                )
            await sleep(wait)
            wait *= 2
    raise RuntimeError("unreachable")


class PaddleVLClient:
    """PaddleOCR-VL（AI Studio）客户端：一次文档识别，保留完整结构化返回。

    直接使用 SDK 结果对象（而不是 MCP wrapper 的 ``DocParsingResult``），
    因为 wrapper 只保留 markdown / pages / images_mapping，会丢掉块结构。
    """

    def __init__(
        self,
        *,
        token: str,
        model: str = DEFAULT_MODEL,
        request_timeout: float = 120.0,
        poll_timeout: float = 900.0,
        base_url: str = "",
    ) -> None:
        self._token = token
        self._model = model
        self._request_timeout = request_timeout
        self._poll_timeout = poll_timeout
        self._base_url = base_url
        self._client: Any = None

    async def __aenter__(self) -> "PaddleVLClient":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def start(self) -> None:
        from paddleocr_mcp.inference.shared.paddleocr_api_sdk import AsyncPaddleOCRClient

        self._client = AsyncPaddleOCRClient(
            token=self._token,
            base_url=self._base_url or None,
            request_timeout=self._request_timeout,
            poll_timeout=self._poll_timeout,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def ocr_document(
        self,
        pdf_path: pathlib.Path,
        *,
        page_ranges: str = "",
        attempts: int = 3,
        delay: float = 10.0,
        on_retry: Callable[[str], None] | None = None,
    ) -> DocumentResult:
        """识别一份 PDF；返回 markdown 视图 + 逐页结构化证据 + 信封键名。"""

        if self._client is None:
            raise RuntimeError("client 未启动")

        from paddleocr_mcp.inference.shared.paddleocr_api_sdk import (
            PaddleOCRVLOptions,
            resolve_document_model,
        )
        from paddleocr._api_client._poller import parse_doc_parsing_result

        client = self._client
        model = resolve_document_model(self._model)
        options = PaddleOCRVLOptions()
        ranges = page_ranges or None

        async def _call() -> tuple[Any, Any]:
            try:
                job_id = await client._submit(model, None, str(pdf_path), options, ranges, None)
                jsonl_data, _ = await client._poller.poll_until_done(job_id)
                return parse_doc_parsing_result(job_id, jsonl_data), jsonl_data
            except AttributeError:
                # 私有成员不可用时退回公开 API：逐页证据仍在（raw / pruned_result），
                # 只是拿不到作业信封。
                result = await client.parse_document(
                    model=model, file_path=str(pdf_path), options=options, page_ranges=ranges
                )
                return result, None

        result, jsonl_data = await predict_with_retry(
            _call, attempts=attempts, delay=delay, on_retry=on_retry
        )
        return document_result_from_sdk(result, jsonl_data)


def document_result_from_sdk(result: Any, jsonl_data: Any = None) -> DocumentResult:
    """把 SDK 的文档解析结果转成 producer 的证据模型。"""

    pages: list[PageEvidence] = []
    raw_pages: list[Mapping[str, Any] | None] = []
    for index, page in enumerate(list(getattr(result, "pages", []) or [])):
        pruned = _as_mapping(getattr(page, "pruned_result", None))
        raw_pages.append(pruned or None)
        blocks = tuple(_blocks_from_pruned(pruned))
        settings = _as_mapping(pruned.get("model_settings"))
        pages.append(
            PageEvidence(
                page_seq=index,
                width=_optional_int(pruned.get("width")),
                height=_optional_int(pruned.get("height")),
                markdown_text=str(getattr(page, "markdown_text", "") or ""),
                images=dict(_as_mapping(getattr(page, "markdown_images", None))),
                output_images=dict(_as_mapping(getattr(page, "output_images", None))),
                input_image_url=str(getattr(page, "input_image_url", "") or ""),
                blocks=blocks,
                layout_boxes_count=_layout_boxes_total(pruned),
                preprocessor_applied=bool(settings.get("use_doc_preprocessor")),
            )
        )
    markdown = "\n".join(page.markdown_text for page in pages)
    return DocumentResult(
        markdown=markdown,
        pages=tuple(pages),
        envelope_keys=tuple(_envelope_keys(jsonl_data)),
        model_settings=_page_model_settings(result),
        raw_pages=tuple(raw_pages),
        raw_envelope=jsonl_data,
    )


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


BLOCK_LIST_KEYS = ("parsing_res_list", "parsingResList", "blocks")


def find_block_list(pruned: Mapping[str, Any] | None) -> list[Any]:
    if not isinstance(pruned, Mapping):
        return []
    for key in BLOCK_LIST_KEYS:
        value = pruned.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _blocks_from_pruned(pruned: Mapping[str, Any]) -> list[BlockEvidence]:
    blocks: list[BlockEvidence] = []
    for index, raw in enumerate(find_block_list(pruned)):
        if not isinstance(raw, Mapping):
            continue
        order = raw.get("block_order", raw.get("blockOrder", raw.get("order")))
        blocks.append(
            BlockEvidence(
                provider_block_id=raw.get("block_id", raw.get("blockId", raw.get("id"))),
                provider_block_order=order,
                # 数组位置是事实；阅读顺序（reading_order）留给语义层派生。
                sequence_index=index,
                label=str(raw.get("block_label") or raw.get("blockLabel") or raw.get("label") or ""),
                content=str(
                    raw.get("block_content") or raw.get("blockContent") or raw.get("content") or ""
                ),
                bbox=raw.get("block_bbox", raw.get("blockBbox", raw.get("bbox"))),
                polygon=raw.get("block_polygon_points", raw.get("polygon_points")),
                group_id=raw.get("group_id", raw.get("groupId")),
            )
        )
    return blocks


def _page_model_settings(result: Any) -> dict[str, Any]:
    """取第一页的 model_settings 作为 producer 配置（逐页副本仍在 raw 里）。"""

    for page in list(getattr(result, "pages", []) or []):
        settings = _as_mapping(_as_mapping(getattr(page, "pruned_result", None)).get("model_settings"))
        if settings:
            return settings
    return {}


def _layout_boxes_total(pruned: Mapping[str, Any]) -> int:
    layout = _as_mapping(pruned.get("layout_det_res"))
    boxes = layout.get("boxes")
    return len(boxes) if isinstance(boxes, list) else 0


def _envelope_keys(jsonl_data: Any) -> list[str]:
    keys: set[str] = set()
    if not isinstance(jsonl_data, list):
        return []
    for line in jsonl_data:
        if not isinstance(line, Mapping):
            continue
        keys.update(str(key) for key in line.keys())
        result = line.get("result")
        if isinstance(result, Mapping):
            keys.update(f"result.{key}" for key in result.keys())
    return sorted(keys)


# ------------------------------------------------------------------ 证据与产物


def build_evidence(
    document: DocumentResult,
    *,
    source_path: pathlib.Path,
    model: str,
    provider: str,
    token_source: str,
    page_ranges: str = "",
    producer_version: str = PRODUCER_VERSION,
    created_at: str = "",
    asset_records: Sequence[Mapping[str, Any]] = (),
    raw_mode: str = "page",
    mcp_version: str = "",
) -> dict[str, Any]:
    """构造不可变证据包（``md-prework/ocr-evidence/v1`` 的 Stable Core）。"""

    source_sha256 = sha256_file(source_path)
    document_id = f"{source_path.stem}-{source_sha256[:8]}"
    config_hash = producer_config_hash(
        model=model,
        provider=provider,
        page_ranges=page_ranges,
        mcp_version=mcp_version,
        raw_mode=raw_mode,
    )
    page_payloads: list[dict[str, Any]] = []
    for page in document.pages:
        payload = page.to_dict()
        payload["raw_ref"] = (
            f"{source_path.stem}_prework/raw/page-{page.page_seq:04d}.json"
            if raw_mode != "none"
            else ""
        )
        page_payloads.append(payload)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "document_id": document_id,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "identity": {
            "document_id": document_id,
            "source_sha256": source_sha256,
            "producer_config_hash": config_hash,
        },
        "producer": {
            "name": PRODUCER_NAME,
            "version": producer_version,
            "model": model,
            "provider": provider,
            "paddleocr_mcp_version": mcp_version,
            "settings": dict(document.model_settings),
            "token_source": token_source,
        },
        "source": {
            "filename": source_path.name,
            "sha256": source_sha256,
            "size_bytes": source_path.stat().st_size,
        },
        "envelope_keys": list(document.envelope_keys),
        "page_count": document.page_count,
        "block_count": sum(len(page.blocks) for page in document.pages),
        "content_hash": evidence_content_hash(page_payloads),
        "assets": [dict(record) for record in asset_records],
        "pages": page_payloads,
    }


def producer_config_hash(
    *,
    model: str,
    provider: str,
    page_ranges: str,
    mcp_version: str,
    raw_mode: str,
    producer_version: str = PRODUCER_VERSION,
) -> str:
    """producer 配置指纹：配置相同 = 同一份证据可以复用。"""

    payload = {
        "producer_version": producer_version,
        "model": model,
        "provider": provider,
        "page_ranges": page_ranges,
        "paddleocr_mcp_version": mcp_version,
        "raw_mode": raw_mode,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def paddleocr_mcp_version() -> str:
    try:
        from importlib.metadata import version

        return version("paddleocr-mcp")
    except Exception:  # noqa: BLE001 - 只是记录用，拿不到就留空
        return ""


def evidence_content_hash(page_payloads: Sequence[Mapping[str, Any]]) -> str:
    """内容哈希：绑定页序、块序、块标签与块内容（不含派生视图）。"""

    digest = hashlib.sha256()
    for page in page_payloads:
        digest.update(
            f"P{page.get('page_seq')}|"
            f"{(page.get('coordinate_space') or {}).get('width')}x"
            f"{(page.get('coordinate_space') or {}).get('height')}\n".encode()
        )
        for block in page.get("blocks") or []:
            digest.update(
                (
                    f"B{block.get('sequence_index')}|{block.get('provider_block_id')}|"
                    f"{block.get('label')}|{block.get('content_sha256')}\n"
                ).encode("utf-8", "replace")
            )
        digest.update(str(page.get("markdown_text") or "").encode("utf-8", "replace"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_json_atomic(path: pathlib.Path, payload: Any) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def write_evidence(
    path: pathlib.Path, payload: Mapping[str, Any], *, overwrite: bool = False
) -> pathlib.Path:
    """写证据包；已存在且未显式 overwrite 时拒绝（原始证据不可修改）。"""

    if path.exists() and not overwrite:
        raise EvidenceExistsError(
            f"证据已存在，原始证据不可修改：{path.name}；确需重跑请加 --overwrite 并另开输出目录"
        )
    return write_json_atomic(path, payload)


def write_raw_pages(
    document: DocumentResult,
    raw_dir: pathlib.Path,
    *,
    mode: str = "page",
) -> list[pathlib.Path]:
    """落盘 provider 原始页结果（第二级：Provider Raw Page Artifact）。

    ``none`` 不写（测试/轻量）；``page`` 写每页完整 ``prunedResult``（生产默认）；
    ``full`` 额外写作业信封（调试用，写盘前剔除 token / 凭证 / 请求头类字段）。
    """

    if mode == "none":
        return []
    written: list[pathlib.Path] = []
    raw_dir.mkdir(parents=True, exist_ok=True)
    for page, raw in zip(document.pages, document.raw_pages):
        payload: Any = raw
        if mode == "full":
            payload = {
                "page_seq": page.page_seq,
                "pruned_result": raw,
                "markdown": page.markdown_text,
                "output_images": dict(page.output_images),
                "input_image_url": page.input_image_url,
            }
        if not payload:
            continue
        written.append(write_json_atomic(raw_dir / f"page-{page.page_seq:04d}.json", payload))
    if mode == "full" and document.raw_envelope is not None:
        written.append(
            write_json_atomic(raw_dir / "envelope.json", scrub_secrets(document.raw_envelope))
        )
    return written


_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|signature)",
    re.IGNORECASE,
)
_LONG_OPAQUE_RE = re.compile(r"(?<![A-Za-z0-9_\-/])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-/])")


def scrub_secrets(value: Any, *, depth: int = 0) -> Any:
    """递归剔除敏感字段：token / 凭证 / 请求头 / 长随机串一律不落盘。"""

    if depth > 12:
        return "<<depth-limit>>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY_RE.search(key_text):
                result[key_text] = "<<redacted>>"
                continue
            result[key_text] = scrub_secrets(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [scrub_secrets(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        has_upper = any(char.isupper() for char in value)
        has_lower = any(char.islower() for char in value)
        has_digit = any(char.isdigit() for char in value)
        if _LONG_OPAQUE_RE.search(value) and has_upper and has_lower and has_digit:
            return "<<redacted:opaque>>"
        return value
    return value


def write_status_log(output_dir: pathlib.Path, rows: Sequence[ConvertOutcome]) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ocr_status.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
    return path


def write_summary(
    output_dir: pathlib.Path,
    rows: Sequence[ConvertOutcome],
    *,
    model: str,
    provider: str,
    token_source: str,
    command: str,
    schema_version: str = PRODUCER_VERSION,
) -> pathlib.Path:
    payload = {
        "schema_version": schema_version,
        "model": model,
        "provider": provider,
        "token_source": token_source,
        "command": command,
        "pdfs": len(rows),
        "ok": sum(1 for row in rows if row.status == STATUS_OK),
        "skipped": sum(1 for row in rows if row.status == STATUS_SKIPPED),
        "failed": sum(1 for row in rows if row.status == STATUS_FAILED),
        "rows": [row.to_dict() for row in rows],
    }
    return write_json_atomic(output_dir / "ocr_summary.json", payload)


# ------------------------------------------------------------------ 单份编排


def default_client_factory(**kwargs: Any) -> PaddleVLClient:
    return PaddleVLClient(
        token=kwargs.get("token", ""),
        model=kwargs.get("model", DEFAULT_MODEL),
        request_timeout=kwargs.get("request_timeout", 120.0),
        poll_timeout=kwargs.get("poll_timeout", 900.0),
        base_url=kwargs.get("base_url", ""),
    )


async def convert_pdf(
    pdf: pathlib.Path,
    *,
    output_dir: pathlib.Path | None,
    client_factory: Callable[..., Any] | None = None,
    token: str = "",
    token_source: str = "",
    model: str = DEFAULT_MODEL,
    provider: str = DEFAULT_PROVIDER,
    keep_images: bool = True,
    overwrite: bool = False,
    page_ranges: str = "",
    retries: int = 3,
    retry_delay: float = 10.0,
    write_evidence_file: bool = True,
    raw_mode: str = "page",
    download: bool = True,
    log: Callable[[str], None] | None = None,
    downloader: Callable[[str], bytes] | None = None,
) -> ConvertOutcome:
    """处理一份 PDF：OCR → 证据落盘 → 图片落盘 → Markdown 视图。

    ``output_dir`` 为空表示写在 PDF 同目录（兼容旧行为）。
    """

    started = time.monotonic()
    target_dir = output_dir or pdf.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    # 产物布局：成品 .md 留在 PDF 同目录，其余中间产物统一进 <stem>_prework/。
    md_path = target_dir / f"{pdf.stem}.md"
    prework_dir = target_dir / f"{pdf.stem}_prework"
    evidence_path = prework_dir / "evidence.json"
    raw_dir = prework_dir / "raw"
    media_dir = prework_dir / "media"
    diagnostics_dir = prework_dir / "diagnostics"
    outcome = ConvertOutcome(pdf=pdf.name, model=model, token_source=token_source)

    if write_evidence_file and evidence_path.exists() and not overwrite:
        outcome.status = STATUS_SKIPPED
        outcome.reason = "evidence_exists"
        outcome.evidence = str(evidence_path.relative_to(target_dir).as_posix())
        # 跳过时仍然检查成品 md 里有没有"还没本地化"的图片，提示可用 --overwrite 补齐
        if md_path.is_file():
            outcome.markdown = md_path.name
            outcome.missing_images = find_missing_image_sources(
                md_path.read_text(encoding="utf-8"), target_dir
            )
        outcome.duration_seconds = time.monotonic() - started
        return outcome
    if md_path.exists() and not overwrite and not write_evidence_file:
        outcome.status = STATUS_SKIPPED
        outcome.reason = "markdown_exists"
        outcome.markdown = md_path.name
        outcome.duration_seconds = time.monotonic() - started
        return outcome

    factory = client_factory or default_client_factory
    client = factory(token=token, model=model)
    await client.start()
    try:
        document = await client.ocr_document(
            pdf,
            page_ranges=page_ranges,
            attempts=retries,
            delay=retry_delay,
            on_retry=log,
        )
    finally:
        await client.close()

    markdown = normalize_block_math(document.markdown)
    if not markdown.strip():
        outcome.status = STATUS_FAILED
        outcome.reason = "empty_markdown"
        outcome.duration_seconds = time.monotonic() - started
        return outcome

    images = document.images_mapping
    mapping: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    if images:
        mapping, records, failures = materialize_images(
            images,
            media_dir,
            keep=keep_images and download,
            downloader=downloader,
            on_retry=log,
        )
        markdown = rewrite_image_sources(markdown, mapping)

    md_path.write_text(markdown, encoding="utf-8")
    outcome.markdown = md_path.name
    outcome.pages = document.page_count
    outcome.chars = len(markdown)
    outcome.blocks = sum(len(page.blocks) for page in document.pages)
    outcome.images_total = len(images)
    outcome.images_saved = len(mapping)
    outcome.media_dir = (
        str(media_dir.relative_to(target_dir).as_posix())
        if images and keep_images and download
        else ""
    )
    # 缺图判定按渲染器心智模型（远端引用 / 本地路径不存在都算缺失）
    outcome.missing_images = find_missing_image_sources(markdown, target_dir)
    outcome.image_failures = failures

    if write_evidence_file:
        mcp_version = paddleocr_mcp_version()
        prework_dir.mkdir(parents=True, exist_ok=True)
        for directory in (raw_dir, media_dir, prework_dir / "semantic", diagnostics_dir):
            directory.mkdir(parents=True, exist_ok=True)
        evidence = build_evidence(
            document,
            source_path=pdf,
            model=model,
            provider=provider,
            token_source=token_source,
            page_ranges=page_ranges,
            asset_records=records,
            raw_mode=raw_mode,
            mcp_version=mcp_version,
        )
        write_evidence(evidence_path, evidence, overwrite=overwrite)
        write_raw_pages(document, raw_dir, mode=raw_mode)
        # raw/ 里同时保留 Paddle 原始 Markdown（未做本地化改写前的上游文本）
        (raw_dir / "paddle.md").write_text(document.markdown, encoding="utf-8")
        write_json_atomic(diagnostics_dir / "producer.json", outcome.to_dict())
        outcome.evidence = str(evidence_path.relative_to(target_dir).as_posix())

    outcome.status = STATUS_OK
    outcome.duration_seconds = time.monotonic() - started
    return outcome


def collect_pdfs(inputs: Sequence[str]) -> tuple[list[pathlib.Path], list[str]]:
    """把输入展开成 PDF 列表：目录只取直接子级，重复按绝对路径去重。"""

    pdfs: list[pathlib.Path] = []
    problems: list[str] = []
    seen: set[str] = set()

    def _add(candidate: pathlib.Path) -> None:
        key = str(candidate.resolve()).lower()
        if key not in seen:
            seen.add(key)
            pdfs.append(candidate)

    for raw in inputs:
        path = pathlib.Path(raw).expanduser()
        if not path.exists():
            problems.append(f"路径不存在：{path}")
            continue
        if path.is_dir():
            found = sorted(
                item
                for item in path.iterdir()
                if item.is_file() and item.suffix.lower() == ".pdf"
            )
            if not found:
                problems.append(f"目录下没有 PDF（已忽略子目录）：{path}")
                continue
            for item in found:
                _add(item)
            continue
        if path.suffix.lower() != ".pdf":
            problems.append(f"不是 PDF 文件，已跳过：{path}")
            continue
        _add(path)
    return pdfs, problems


def force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文与标记符号会乱码或抛异常。"""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def classify_log_text(text: str) -> str:
    """暴露给调用方的远端压力分类（供调度器复用同一真源）。"""

    return classify_text(text).kind
