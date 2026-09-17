# PaddleOCR-VL 结构化返回能力报告（capability probe）

本目录是**第一施工任务**的验收物：只确认"当前 PaddleOCR-VL / AI Studio 链路到底返回了哪些结构证据"，
不改任何生产逻辑，也不实现任何 layout detector 的替代品。

| 文件 | 说明 |
| --- | --- |
| `capability_report.json` | 机器可读的能力结论（验收清单逐项） |
| `structure_map.json` | 返回对象的键/类型结构树（只记类型与长度，不记内容） |
| `../tools/paddle_capability.py` | 纯函数层：结构描述、能力判定、脱敏（不联网） |
| `../tools/probe_paddle_capabilities.py` | 真实调用入口：`--input` 跑一次，或用 `--replay` 离线复算 |
| `../tests/fixtures/paddle_structured_result.fixture.json` | 脱敏后的真实返回 fixture |
| `../tests/test_paddle_capability.py` | 离线回归测试（不访问远端） |

---

## 1. 结论（一句话）

**PaddleOCR-VL 的结构化能力是完整的，而且早就在返回里了：页面、块列表、块 id、块标签、块 bbox、
多边形、分组、版面框、Markdown、图片全部可得；丢失发生在 `paddleocr-mcp` 这一层的 wrapper 上**
（它只保留 `markdown` / `pages` / `images_mapping`）。因此归属判定为 **B：SDK 返回，wrapper 丢掉**。

| 能力 | 结论 | 证据位置 |
| --- | --- | --- |
| `markdown` | ✅ | `layoutParsingResults[i].markdown.text` |
| `images_mapping` | ✅ | `layoutParsingResults[i].markdown.images`（值是 CDN URL，不是 base64） |
| `page_results`（多页） | ✅ | 每行 JSONL 一个 `layoutParsingResults` 条目；实测 4 页 = 4 行 |
| `blocks` | ✅ | `prunedResult.parsing_res_list`（实测首页 27 块、4 页共 72 块） |
| `block_id` / `block_label` / `block_bbox` / `block_content` | ✅ | 每个块字典的键 |
| `block_order` | ⚠️ 存在但**部分为 null**（实测 19.4%） | `block_order` 键在；`header` / `doc_title` 这类块为 `null` |
| 额外结构 | ✅ | `block_polygon_points`、`group_id`、`layout_det_res.boxes`（202 框，含 `cls_id`/`score`/`order`/`polygon_points`） |
| `outputImages` | ✅ | `layout_det_res` 版面可视化图 URL |
| 原始 provider response | ✅ | 作业信封（每行 JSONL 的 `result`）可完整取得 |
| `exports` | ❌（本次为空对象） | `layoutParsingResults[i].exports` |
| MCP wrapper 是否暴露块 | ❌ | `paddleocr_mcp.inference.types.DocParsingResult` 只有 3 个字段 |

对应的 `capability_report.json` 关键片段：

```json
{
  "markdown": true,
  "images_mapping": true,
  "page_results": true,
  "blocks": true,
  "block_id": true,
  "block_order": true,
  "block_label": true,
  "block_bbox": true,
  "block_content": true,
  "raw_provider_response_accessible": true,
  "gap_classification": "B",
  "block_count_page1": 27,
  "block_count_total": 72,
  "block_order_null_ratio": 0.1944
}
```

## 2. 证据分层（实际取到的层级关系）

```text
作业信封（每行 JSONL）
  errorCode / errorMsg / logId
  result
    dataInfo { numPages, pages[{width,height}], type: "pdf" }
    layoutParsingResults[ i ]           ← 每行恰好 1 条，即 1 页
      inputImage                        ← 页图 URL
      markdown { text, images }         ← wrapper 只保留这两项
      outputImages { layout_det_res }   ← 版面可视化图 URL
      prunedResult
        width / height / page_count
        model_settings                  ← use_layout_detection / format_block_content 等实际取值
        doc_preprocessor_res            ← 预处理信息（含 angle）
        layout_det_res { boxes[] }      ← 版面框：cls_id / label / score / coordinate / order / polygon_points
        parsing_res_list[ n ]           ← 真正的块列表
          block_id / block_order / block_label / block_content / block_bbox
          block_polygon_points / group_id
    preprocessedImages                  ← 预处理页图（信封级）
```

实测标签词表（`paddleocr-vl-md` 侧文档，不含手写规则）：
`text`、`display_formula`、`inline_formula`、`paragraph_title`、`figure_title`、`image`、
`chart`、`header`、`number`、`doc_title`。

## 3. 实跑条件（可复现）

| 项目 | 值 |
| --- | --- |
| 日期 | 2026-09-18 |
| 模型 / provider | `PaddleOCR-VL-1.6` / `aistudio`（`paddleocr-mcp 0.8.5`） |
| 主探测 PDF | `paper-01_book-1-7_pdf-6-12.pdf`（双栏书页，含正文、行内/独立公式、插图） |
| 页范围 | `1-4`（多页） |
| 二次抽样 PDF | `mock_09.pdf`（试卷版式，页 1-2，66 块） |
| 令牌来源 | 环境变量名 `PADDLEOCR_API_KEY`（只记录来源变量名，从不打印值） |

复现命令（真实调用；`--raw-dump` 必须写仓库外路径）：

```powershell
conda run -n pb python tools\probe_paddle_capabilities.py `
  --input <pdf> --output-dir capability `
  --fixture-out tests\fixtures\paddle_structured_result.fixture.json `
  --page-ranges 1-4 --retries 8 --retry-delay 20 `
  --raw-dump <仓库外路径>\envelope.json
```

离线复算（不再消耗配额，报告与 fixture 都由同一份信封重建）：

```powershell
conda run -n pb python tools\probe_paddle_capabilities.py `
  --input <pdf> --output-dir capability `
  --fixture-out tests\fixtures\paddle_structured_result.fixture.json `
  --page-ranges 1-4 --replay <仓库外路径>\envelope.json
```

离线测试：

```powershell
conda run -n pb python -m pytest tests\test_paddle_capability.py -q
```

## 4. 实测发现的四个坑（下一步必须处理）

1. **页码没有显式字段**：`layoutParsingResults` 条目里没有 `pageIndex`，每行 `dataInfo.numPages` 都是 1，
   所以页码只能由"行序 = 页序"推导。Evidence 里的 `page_index` 必须由 producer 生成，不能等 Paddle 给。
2. **`block_order` 会为 null**：`header` / `doc_title` 这类块实测为 `null`（本批 19.4%，试卷抽样 15.2%）。
   阅读顺序不能只读 `block_order`，必须以**块列表顺序**为主、`block_order` 为非空时做校验/修正。
3. **块不止 bbox**：还有 `block_polygon_points`（四点浮点多边形）和 `group_id`。倾斜/多栏页面上
   bbox 会跨栏，多边形才是可靠几何证据；`group_id` 可用于"同属一个版面区域"的分组线索。
4. **图片是 CDN URL，且信封里另有一套预处理图**：`markdown.images` 与 `inputImage`/`outputImages`
   /`preprocessedImages` 都是远端 URL，需要下载 + 文件头校验（旧 producer 已有这套逻辑）；
   `preprocessedImages` 可用来对照预处理是否翻转/纠偏。

## 5. 对后续施工的含义

- **可以做 block 级 Evidence**：不需要任何替代性布局分析，直接把 `parsing_res_list` 原样落盘即可；
  5-block 滑窗 LLM 分类可以建立在 Paddle 真实块粒度上，不必再从被压平的 Markdown 反推结构。
- **producer 合并时**：唯一 producer 要原生保存"信封 + 每页 prunedResult + 块列表 + 图片引用",
  而不是只写 Markdown；`markdown` 降级为可读视图。
- **判定为 B 意味着**：不需要改服务端调用方式，只需要在 producer 里直接使用 SDK 结果对象
  （`DocParsingPage.raw` / `.pruned_result`）或复制 `parse_document` 的两行私有调用，
  就能拿到全部结构；MCP wrapper 的 `DocParsingResult` 不应再作为证据来源。

## 6. 本次遵守的红线

- 只探测返回对象结构，没有改变任何生产逻辑；
- 没有打印令牌、没有把请求头或签名 URL 写盘（fixture 已脱敏：长文本截断、URL 与 base64 负载替换为占位符）；
- 没有实现任何 layout detector 的替代品；
- 完整原始响应只落在仓库外路径，仓库内只保留脱敏 fixture 与报告。
