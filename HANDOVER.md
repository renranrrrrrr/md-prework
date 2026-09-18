# md-prework 交接文档

> 最后更新：2026-09-18 ｜ 交接版本：**main @ `b2f23ca`**（本文档所在提交；上一个功能提交 `1f7901e`，milestone tag `prework-semantic-foundation-v1` → `cf13a1c`）
> 适用读者：接手本仓库继续开发的工程师（假定熟悉 Python / pytest / 命令行，不假定了解历史决策）

---

## 0. 一句话概览

`md-prework` 把 PDF 变成**可复跑、可追溯的题目证据与派生视图**，四段管线：

```
PDF ──▶ OCR Producer ──▶ OCR Evidence（不可变） ──▶ Normalized View（派生）
                                                        │
                                                        ▼
                               （可选）Semantic Layer：5-block 窗口 → LLM 判定 → 确定性 assembler → Problem Candidate
```

它**只负责上游生产**：不写数据库、不做题库业务、不建 taxonomy、不资产化。
下游题库工程（ProblemBank）与它**完全独立**，两者只通过版本化 artifact / CLI 契约对接。

当前状态：前三段已完成并冻结口径，第四段（语义层）已完成零模型链 + **第一轮真实 LLM baseline**，
下一步是 schema 最小化与成本优化（详见第 11 节）。

---

## 1. 项目边界（红线，改动前务必确认）

1. **md-prework 与 ProblemBank 完全独立**。本仓库不得出现题库业务逻辑（ORM、题目创建、taxonomy、asset 上传、Payload 组装）。只允许输出 artifact，让对方在自己的 adapter 里消费。
2. **唯一 producer**：`paddleocr-vl-md/ocr_producer.py` 是唯一的 OCR 实现。历史上有两套同源实现（本仓库旧 `pdf2md.py`、以及被删掉的 `problembank-ocr-producer/`），**不允许再出现第二套**。
3. **Evidence 不可变**：`evidence.json` 一旦生成不得修改；Normalized View / 语义判定 / 切题结果都是**派生结果**，永不回写证据。
4. **原始 PDF 直传**：不得先把 PDF 渲染成图片再送 PaddleOCR-VL。
5. **分层调用**：producer 不调 LLM、不切题；normalizer 不联网；**只有语义层允许调用模型**。
6. **凭据只走环境变量名**：代码、日志、产物里不得出现 token 值；不许把 token 写进配置文件。
7. **不要顺手优化 normalizer**：`text_math_v1.1` 已冻结。发现新问题 → 进 benchmark → 单独开 `v1.2`，不要边跑边改。
8. 本批次**不做 LaTeX renderer**、不做多模态回看、不做多 Agent。

---

## 2. 仓库与版本

| 项 | 值 |
| --- | --- |
| 远端 | `https://github.com/renranrrrrrr/md-prework.git` |
| 默认分支 | `main` |
| 当前 main | `1f7901e`（本轮 baseline 工具改进） |
| milestone tag | `prework-semantic-foundation-v1` → `cf13a1c` |
| 已删除的远端分支 | `temp`（其内容已并入 main）、`codex/problembank-ocr-producer`（重复 producer，已废弃） |
| 本地开发副本 | `D:\Documents\ProblemBank\tmp\mdprework`（该目录被 ProblemBank 仓库 gitignore，不属于 ProblemBank 的版本控制） |

**目录结构**

```
md-prework/
  md-math-normalizer/     # 独立子项目：中文数学 Markdown 规范化器（被 Normalized View 调用）
  paddleocr-vl-md/        # 主链路：producer + evidence + normalized view + semantic layer + tools
  HANDOVER.md             # 本文档
```

**踩坑提醒（重要）**：目前这台机器上的克隆是**受限 refspec**（`remote.origin.fetch` 只覆盖 `main`），
所以 `git branch -r` 看不到其它远端分支，直接 `git merge` 别的分支会报 `unrelated histories`。需要时显式 fetch：

```powershell
git fetch --depth 50 origin <branch>:refs/remotes/origin/<branch>
```

---

## 3. 系统架构与职责

### 3.1 四层

| 层 | 职责 | 唯一实现 | 输出 |
| --- | --- | --- | --- |
| ① OCR Producer | 调 PaddleOCR-VL、保全证据与素材、重试与背压 | `ocr_producer.py` | `evidence.json` + `raw/` + `media/` + 成品 `.md` |
| ② MD Math Normalizer | 数学表示规范化（**格式级**，不做语义纠错） | `md-math-normalizer/`（独立项目） | `normalized-view.json` |
| ③ Semantic Layer（可选） | 5-block 窗口 → LLM 判 ROLE/BOUNDARY/SPLIT → 确定性 assembler | `semantic_*.py` | `semantic-run.json` + Problem Candidate |
| ④ Tools / Benchmarks | 能力探测、结构性统计、回归对照 | `tools/` | 各类报告 JSON/MD |

### 3.2 数据流（实际代码路径）

```
<stem>.pdf
  └─ ocr_producer.convert_pdf()
       ├─ PaddleVLClient.ocr_document()            # SDK 直连，保留 page.raw / pruned_result
       ├─ materialize_images()                     # 图片下载 + 文件头校验 + src 改写
       ├─ write_evidence() / write_raw_pages()     # 证据 + 原始页
       └─ <stem>.md                                # 最终成品（可读/可编辑视图）

normalized_view.process_file()
  └─ <stem>_prework/normalized-view.json          # 派生：block_ref → normalized_content

semantic_baseline.run_document()
  ├─ semantic_window.load_document()              # evidence + normalized view → 文档级 block 流
  ├─ semantic_prediction / semantic_prompt        # 组装 payload + 校验 + 重试
  ├─ semantic_provider_deepseek                   # 真实 provider（Responses API + JSON Schema）
  ├─ semantic_reconcile                           # overlap 汇总 + 扩窗触发 A–F
  └─ semantic_assembler                           # SPLIT 解析 + 候选物化
```

---

## 4. 代码地图（`paddleocr-vl-md/`）

### 4.1 生产链路

| 文件 | 作用 | 备注 |
| --- | --- | --- |
| `ocr_producer.py` | **唯一 producer 核心**：SDK 直连、证据构建、图片处理、重试、布局落盘 | 44 KB，最重要的文件 |
| `prework_ocr.py` | 编排入口：`convert` / `batch`、`--output-dir`、`--stdout`、`--jobs`、status/summary | 给外部编排用 |
| `pdf2md.py` | 面向人的前端（位置参数、产物写在 PDF 同目录） | 逻辑全部委托 `ocr_producer` |
| `prework_paths.py` | **产物布局唯一真源**：新旧两种布局的路径解析 | 工具都走它，别自己拼路径 |
| `remote_pressure.py` | 远端压力分类（背压 / 配额耗尽 / 本地错误） | 纯函数，被 producer 与调度器共用 |
| `run_pipeline.py` | 单文件全流程（OCR → 规范化 → 复检 → 标记） | 保留的旧编排 |
| `run_pipeline_parallel.py` | 多 PDF 受限并发 + 自适应冷却 | 调度器与 `Backoff` / `ScheduleStats` 在这里 |
| `ocr_mark.py` | 只标记可疑行（页眉残片等），不改文件 | 辅助 |

### 4.2 规范化与语义层

| 文件 | 作用 |
| --- | --- |
| `normalized_view.py` | Normalized View V1：按 label 分发（`text→text_math_v1.1`、`formula→formula_v1`、其余 `preserve`），硬不变量（块数/引用/原文不变、fatal 不丢块、幂等） |
| `semantic_window.py` | 文档级 block 流 + `window=5 / stride=3` 窗口 + `expand_window(9/13)` |
| `semantic_prediction.py` | `semantic-prediction/v1` 结构校验 + `FakeSemanticProvider` + 稳定 `window_id` |
| `semantic_prompt.py` | system 提示模板 + 响应解析（去代码围栏）+ 非法响应重试 |
| `semantic_provider_deepseek.py` | DeepSeek provider（Responses API + JSON Schema structured output，含 token 用量留档） |
| `semantic_reconcile.py` | overlap reconcile（冲突降级 `UNCERTAIN`）+ 自适应扩窗触发 A–F |
| `semantic_assembler.py` | SPLIT anchor 解析（重复不切）+ Problem Candidate 物化（只存引用、`excluded` 全登记） |
| `semantic_chain.py` | 零模型端到端链（窗口 → 预测 → reconcile → 扩窗 → split → assembler） |
| `semantic_baseline.py` | baseline 运行器：`--provider fake/deepseek`、`--jobs`、断点续跑、逐窗留档、硬门统计 |

### 4.3 工具与报告（`tools/`、`capability/`）

| 文件 | 作用 |
| --- | --- |
| `tools/probe_paddle_capabilities.py` | Paddle 返回结构能力探测（真实调用，产物见 `capability/`） |
| `tools/paddle_capability.py` | 探针纯函数（结构树、能力判定、脱敏） |
| `tools/block_evidence_benchmark.py` | 块级结构统计 + **extraction loss** 硬指标 |
| `tools/weak_question_alignment.py` | 与旧重建结果的弱对齐（block span 分布） |
| `tools/weak_alignment_anomalies.py` | 弱对齐异常定性（ALIGNMENT_DRIFT / SOLUTION_BLEED / …） |
| `tools/normalizer_diff_audit.py` | Normalizer 改动分桶（HIGH_RISK / WRAP_MATH_ONLY / …）+ `standalone_numeric_wraps` |
| `tools/block_evidence_benchmark.py` 等 | 全部离线可复跑 |

### 4.4 测试

| 目录 | 数量 | 命令 |
| --- | --- | --- |
| `paddleocr-vl-md/tests/` | **195 项** | `python -m pytest tests -q`（全部离线，不触网/不调模型） |
| `md-math-normalizer/tests/` | **154 项** | `$env:PYTHONPATH="src"; python -m pytest -q -o addopts=""`（该 env 里没装 pytest-timeout） |

---

## 5. 数据契约（四个 schema）

### 5.1 `md-prework/ocr-evidence/v1`（不可变证据，Stable Core）

```jsonc
{
  "schema_version": "md-prework/ocr-evidence/v1",
  "document_id": "<stem>-<source_sha256 前 8 位>",
  "identity": { "document_id": "…", "source_sha256": "…", "producer_config_hash": "…" },
  "producer": { "name": "prework-ocr", "model": "PaddleOCR-VL-1.6", "provider": "aistudio",
                "paddleocr_mcp_version": "0.8.5", "settings": { … model_settings … } },
  "source": { "filename": "paper.pdf", "sha256": "…", "size_bytes": 123 },
  "envelope_keys": ["errorCode", "result.layoutParsingResults", …],
  "page_count": 2, "block_count": 40, "content_hash": "…",
  "assets": [ { "src": "imgs/a.jpg", "state": "saved", "file": "…", "sha256": "…" } ],
  "pages": [{
    "page_seq": 0,
    "page_index_source": "response_sequence",
    "coordinate_space": { "kind": "provider_page", "width": 1191, "height": 1684 },
    "layout_detection": { "boxes_count": 77 },
    "order_consistency": { "ordered_blocks": 36, "block_order_present": 30,
                           "block_order_monotonic": true, "block_order_conflicts": 0 },
    "markdown_text": "…",
    "images": { "imgs/a.jpg": "<<remote:…>>" },
    "raw_ref": "<stem>_prework/raw/page-0000.json",
    "blocks": [{
      "block_ref": "p0000:b0000", "provider_block_id": 0, "provider_block_order": null,
      "sequence_index": 0, "label": "text", "content": "…",
      "bbox": [x1,y1,x2,y2], "polygon": [[x,y],…], "group_id": 0, "content_sha256": "…"
    }]
  }]
}
```

要点：`sequence_index` 是**数组位置事实**；`provider_block_order` 是 Paddle 字段（**可为 null，实测 11%–33%**）；
阅读顺序属于"解释"，留给语义层派生。原始页完整 `prunedResult` 在 `raw/page-NNNN.json`。

### 5.2 `md-prework/normalized-view/v1`（派生）

```jsonc
{ "schema_version": "md-prework/normalized-view/v1",
  "evidence_hash": "…",
  "blocks": [{ "block_ref": "p0000:b0000", "label": "text",
               "profile": "text_math_v1", "profile_version": "text_math_v1.1",
               "status": "changed|unchanged|preserved|fatal",
               "normalized_content": "…", "actions": [{ "kind": "NORMALIZE_TEXT", "impact": "substantive" }],
               "diagnostics": [] }] }
```

`fatal` 表示**这个块的规范化失败**（`normalized_content=null`，原文仍可用），不代表管线失败。

### 5.3 `md-prework/semantic-prediction/v1`（模型输出，当前 verbose 版）

```jsonc
{ "schema": "md-prework/semantic-prediction/v1", "window_id": "w0007",
  "roles":      [{ "block_ref": "p0001:b0017", "role": "PROBLEM_START" }],
  "boundaries": [{ "left_ref": "…", "right_ref": "…", "relation": "SAME_PROBLEM" }],
  "splits":     [{ "block_ref": "…", "anchor": "4. 已知" }],
  "uncertain":  [{ "kind": "BOUNDARY|ROLE|SPLIT", "refs": ["…"], "reason": "…" }] }
```

枚举：role ∈ {`PROBLEM_START` `PROBLEM_CONTINUATION` `SOLUTION_START` `SOLUTION_CONTINUATION` `SHARED_CONTEXT` `NON_PROBLEM` `UNCERTAIN`}；
relation ∈ {`SAME_PROBLEM` `NEW_PROBLEM` `NOT_RELATED` `UNCERTAIN`}。
硬约束：roles 按窗口顺序**逐块覆盖**；boundaries 覆盖**全部相邻**；不得改写正文；splits 只给**原文唯一 anchor**（不给 offset）；**不得出现数值 confidence**。

> 注意：**这份 schema 正在被最小化**（见第 11 节）。改动时同步的地方：`semantic_prediction.validate_prediction`、`semantic_provider_deepseek.PREDICTION_JSON_SCHEMA`、`semantic_prompt.SYSTEM_PROMPT`、`semantic_reconcile`、`semantic_assembler`。

### 5.4 `md-prework/problem-candidates/v1`（assembler 产物）

```jsonc
{ "schema_version": "md-prework/problem-candidates/v1", "candidate_count": 2,
  "candidates": [{ "candidate_id": "pc0001",
                   "statement_refs": [{ "block_ref": "…", "start": 0, "end": 12 }],
                   "solution_refs": [], "shared_refs": ["…"],
                   "number": { "number": "1", "numbering_system": "arabic" },
                   "diagnostics": [] }],
  "excluded": [{ "block_ref": "…", "reason": "excluded_role:NON_PROBLEM" }],
  "diagnostics": ["orphan_solution:…"] }
```

候选**只保存引用**（`block_ref` + 半开区间），从不复制正文。

---

## 6. 产物布局

### 6.1 新布局（本轮起，生产默认）

```
<docs>/<stem>.pdf                    原始 PDF
<docs>/<stem>.md                     最终成品（唯一留在 PDF 目录的产物）
<docs>/<stem>_prework/
    evidence.json                    Stable Evidence（不可变）
    normalized-view.json             派生视图
    raw/paddle.md                    Paddle 原始 Markdown
    raw/page-0000.json               每页原始 prunedResult
    media/…                          图片素材（文件头校验后落盘）
    semantic/semantic-run.json       语义层运行结果
    semantic/calls/w0000.json        逐窗留档（请求/原始响应/尝试/预测/usage）
    diagnostics/producer.json        该文档的运行记录
<docs>/diagnostics/ocr_status.jsonl  batch 状态（编排级）
<docs>/diagnostics/ocr_summary.json  batch 汇总
```

### 6.2 旧布局（历史数据，仍然兼容读取）

```
<docs>/<stem>.evidence.json          → 等价于 evidence.json
<docs>/<stem>.normalized.json        → 等价于 normalized-view.json
<docs>/<stem>_raw/page-NNNN.json
<docs>/<stem>_media/…
<docs>/<stem>.semantic-run.json      （旧布局下语义汇总落在这里）
<docs>/calls/<stem>/w####.json       （旧布局下的逐窗留档）
```

**所有工具通过 `prework_paths.py` 解析路径，两种布局都能读**；不要在新代码里手拼路径。

---

## 7. 环境、依赖与凭据

| 项 | 值 |
| --- | --- |
| Python 环境 | conda env **`pb`**（本机：`C:\Users\Administrator\.conda\envs\pb\python.exe`） |
| PaddleOCR 依赖 | `paddleocr-mcp>=0.8.5`（只做 API 客户端，不本地推理），`paddleocr` 包提供 SDK 与 Responses 解析 |
| Paddle 凭据 | 用户级环境变量 `PADDLEOCR_API_KEY`（代码里兼容 `PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN`） |
| LLM 凭据 | 用户级环境变量 `DEEPSEEK_API_KEY`（**必须独立，禁止读 Codex 自己的配置**） |
| 模型 | `PaddleOCR-VL-1.6`（AI Studio）/ `deepseek-v4-flash`（DeepSeek） |

令牌解析规则：只读**环境变量名**（进程级 → 用户级注册表兜底），日志只打印来源变量名；任何产物、报告、日志里都不写 token 值。

---

## 8. 常用命令

```powershell
# 0) 统一入口（本机没有 conda 命令时用绝对路径的解释器）
$py = "C:\Users\Administrator\.conda\envs\pb\python.exe"
cd D:\Documents\ProblemBank\tmp\mdprework\paddleocr-vl-md

# 1) OCR：单份 / 批量（原始 PDF → evidence + raw + media + 成品 md）
& $py prework_ocr.py convert --input <pdf> --output-dir <out>            # 图片默认导出
& $py prework_ocr.py batch --pdf-dir <dir> --output-dir <out> --jobs 2
& $py pdf2md.py <pdf> --no-images                                       # 人用前端（与 PDF 同目录）

# 2) Normalized View（派生）
& $py normalized_view.py --evidence-dir <out>

# 3) 语义窗口（离线，可先看 payload）
& $py semantic_window.py --evidence <out>/<stem>_prework/evidence.json \
      --output <out>/<stem>_prework/semantic/request.json

# 4) 单窗真实试跑（会留档 request/attempts/prediction）
& $py semantic_provider_deepseek.py --evidence <evidence.json> --window 0

# 5) 语义 baseline（fake 免费；deepseek 真实计费）
& $py semantic_baseline.py --evidence-dir <dir> --provider fake --jobs 4 --output <report.json>
& $py semantic_baseline.py --evidence-dir <dir> --provider deepseek --jobs 4 \
      --output <report.json>          # 默认断点续跑：已有语义产物就跳过（--force 重跑）

# 6) 工具
& $py tools/block_evidence_benchmark.py --evidence-dir <dir> --output <r.json> --markdown <r.md>
& $py tools/normalizer_diff_audit.py --evidence-dir <dir> --output <r.json> --markdown <r.md>

# 7) 测试
& $py -m pytest tests -q                       # md-prework：195 项
cd ..\md-math-normalizer; $env:PYTHONPATH="src"; & $py -m pytest -q -o addopts=""   # 154 项
```

---

## 9. 已完成与实测结果

### 9.1 Phase 3：证据层（完成）

18 份真实 PDF（全国高中数学联赛模拟题）：

| 指标 | 值 |
| --- | --- |
| 文档 / 页 / 块 / 图片 | 18 / 124 / **2245** / 98（全部落盘） |
| **extraction loss** | **0**（逐页比对 provider raw 与证据块数） |
| label 分布 | text 1329、display_formula 192、paragraph_title 139、header 124、number 122、inline_formula 102、figure_title 97、image 96、doc_title 18、formula_number 16、table 6、vision_footnote 2、algorithm 1、footer 1 |
| `provider_block_order` null | 11.6%–32.9%（中位 21.2%），conflicts **0**，非单调页 **0** |
| bbox / polygon 完整率 | 100% / 100% |
| Paddle wrapper 结论 | **B：SDK 返回块结构，MCP wrapper 丢掉**（wrapper 只有 markdown/pages/images_mapping） |

### 9.2 Phase 4：Normalized View（冻结于 `text_math_v1.1`）

| 指标 | 值 |
| --- | --- |
| byte_changed / cosmetic_only / substantive_changed | 1385 / 293 / 1092 |
| preserved（混排或拆分守卫） / fatal | 5 / 6 |
| 幂等失败 | **0** |
| `standalone_numeric_wraps` | **0**（改造前 538） |
| 剩余 HIGH_RISK | 151（抽样以设计行为为主：补 `\displaystyle`、中文散文里的拉丁变量包裹） |
| 公式块断言 | `normalized_content == raw.strip()`，294/294 成立 |

### 9.3 Phase 5：语义层 —— 第一轮真实 baseline（18 份，742 窗口）

| 质量指标 | 值 |
| --- | --- |
| 窗口 / 候选 | 742（5-block、stride=3）/ **312**（旧 268 题仅作 regression 参考） |
| UNCERTAIN 条目 | 569 |
| role 冲突 / boundary 冲突 | 576 / 66（18/18 文档有冲突） |
| split | resolved 133、ambiguous 0、not_found 3 |
| **silent_loss** | **15** ⚠️（未修，见第 10 节） |
| 非法响应 | **2 / 742**（均重试成功） |
| 扩窗触发（未执行扩窗调用） | OVERLAP_CONFLICT 408、TAIL 395、HEAD 349、MODEL_UNCERTAIN 327、SIGNAL_CONFLICT 190、EDGE_SPLIT 70 |

| 成本指标（408 次调用有 usage 记录） | 合计 | 每次平均 |
| --- | --- | --- |
| 输入 tokens | 916,185（缓存 394,739） | 2,246 |
| 输出 tokens | 1,250,556 | 3,065 |
| └ 推理 tokens | **1,109,901（占输出 88.8%）** | **2,720** |

---

## 10. 已知问题与待办（按优先级）

1. **`silent_loss = 15`（must-fix）**：有 15 个块既没被候选引用、也没进 `excluded`、也没有诊断。
   已知样例：`mock_02 p0004:b0011`、`mock_04 p0004:b0006`、`mock_06 p0001:b0016`。
   定位方式：用 `semantic_baseline.run_document` 里的 gate 逻辑逐个文档重算，检查是不是 `UNCERTAIN` 角色块在 split 拆分后丢失登记。
2. **成本大头是推理 token（88.8%）**，不是输入或 JSON 结构。见第 11 节的门。
3. **前 8 份文档没有 usage 记录**（当时还没接 `usage` 字段），所以成本只能按后 10 份外推。
4. **9/13 扩窗调用尚未执行**：目前只记录触发原因。执行前先看触发率（很高，会让调用量翻 2–3 倍）。
5. **UNCERTAIN / 冲突率偏高（569 / 576）**：这是 5-block、stride=3 的结构性结果（窗口边缘看不清），不是模型能力问题；扩窗机制就是为了解决它。
6. **弱对齐仍然不可信**：243/268 题对齐，18 例 `SOLUTION_BLEED`；尝试加"单调搜索 + 答案区截断"后对齐率掉到 125，已降级为实验开关（`--monotonic` / `--zone-cutoff`）。**新的 span 分布要等这套修好才算数**，别用它下窗口参数结论。
7. **旧布局与新布局并存**：18 份历史数据是旧布局（工具已兼容），新跑的数据走 `<stem>_prework/`。
8. 题号抽取很朴素：只识别题首 `1.` / `一、`（`semantic_assembler.extract_question_number`），仅用于 provenance 与回归对照。

---

## 11. 下一步计划（已与项目负责人确认）

> 原则：**先拿到第一份真实 LLM baseline，不提前优化 prompt**；用"成本下降 + 结构判断质量不降"作为门的唯一判据。

1. **完整报告 semantic-v1 质量与 usage/cost** —— 已完成（见第 9.3 节）。
2. **不改 prompt 语义内容**（枚举、硬约束、正例都保留）。
3. **schema 信息最小化**：
   - 删除回显：输出不再返回 `schema` / `window_id`；
   - 删除 `uncertain[].reason`；
   - **尝试删除 `boundaries`**：由 `roles` 确定性推导（roles 顺序固定 → 相邻关系可推）；
   - 输入只发：`index` + `label` + 有效文本（normalize 成功发 normalized，fatal 发 raw 并标 `fatal`）+ 必要的页断标记（`page_end`，跨页不得断题）；
   - 同步改：`semantic_prediction` 校验、`semantic_provider_deepseek.PREDICTION_JSON_SCHEMA`、`semantic_prompt` 输出说明、`semantic_reconcile`、`semantic_assembler`（引用由位置映射回 `block_ref`）。
4. **A/B 验证**：固定同一组窗口（建议 mock_09 的 44 窗 + mock_13 的 48 窗，共 92 窗），verbose 与 minimal 各跑一次，比较：token/次、JSON 合法率、roles 一致率、split 一致率。
5. **reasoning / 输出上限测试**：同组窗口试更低推理档位与 `max_output_tokens` 上限，量化省下的 token 与质量变化。
6. **门的判据**：成本显著下降 **且** 结构判断质量不降 → 冻结 **semantic-v2**；否则只保留 schema 最小化，回退推理档位实验。

预期：第 3–4 项能削掉的是输出里 JSON 的那部分（约 10%），**真正的成本大头要靠第 5 项**（推理占输出 88.8%）。

---

## 12. 协作与汇报流程

- 项目负责人要求：**每完成一步，用"电脑操作技能"（`computer-use` / cua_repl）直接操作他本机已打开的 ChatGPT 浏览器窗口**，把进度汇报给 GPT 并取回反馈，然后继续下一步。
  - **不要使用 browser-llm 插件**（已卸载，负责人明确要求用电脑操作技能直接操作浏览器窗口）。
  - 汇报内容建议包含：本轮做了什么、实测数字、发现的问题、需要对方裁决的点。
- 汇报产物（prompt 文本、粘贴记录）可留在 `D:\Documents\ProblemBank\tmp\cap_probe\` 下作为留痕。
- 每步之后跑测试并记录数字；不要只报"代码写了"，要报**可复现的实测结果**。

---

## 13. 参考数据位置（本机）

| 内容 | 路径 |
| --- | --- |
| 真实 PDF（18 份） | `D:\Documents\ProblemBank\storage\imports\math_olympic_2011_zk1_batch\pdfs\` |
| 证据 + 语义运行（本轮） | `…\math_olympic_2011_zk1_batch\blocks_20260918\`（`*.evidence.json`、`*.semantic-run.json`、`calls\<stem>\w*.json`） |
| 各类报告 | `…\math_olympic_2011_zk1_batch\blocks_20260918_benchmark\` |
| 旧的 268 题重建结果（regression 参考，**不是真值**） | `…\storage\imports\reconstruction_benchmark\math_olympic_2011_zk1\docs\<doc>\rule_plan.json` |
| 旧 Markdown / 图片快照 | `…\math_olympic_2011_zk1_batch\ocr_markdown\`、`reocr_20260917\` |
| 本会话留痕（提示词、日志） | `D:\Documents\ProblemBank\tmp\cap_probe\` |
| 被隔离的派生文件（可恢复） | `D:\Documents\ProblemBank\tmp\superseded\` |
| 能力探测产物 | `paddleocr-vl-md/capability/`（仓库内） |

> 说明：这些目录属于 ProblemBank 工作区的 `storage/`（被 gitignore），**不在 md-prework 仓库里**。若换机器，需要把这些数据一起带走，或重新用第 8 节命令生成（重跑 OCR 会消耗 Paddle 配额）。

---

## 14. 常见坑（踩过的）

1. **受限 refspec 的克隆**：看不到/取不到其它远端分支，`git merge` 会报 `unrelated histories`（见第 2 节）。
2. **DeepSeek Responses 返回里 `reasoning` 与 `message` 两种 item**：只能取 `message` 的 `output_text`，否则会把思维链拼进 JSON 导致解析在 char 0 失败（已修，见 `_extract_text`）。
3. **并发下语义产物撞车**：旧布局多份文档共用一个目录，`calls/w0000.json` 与 `semantic-run.json` 会互相覆盖。现在按文档分子目录、并给汇总文件加 `<stem>.` 前缀。
4. **`usage` 不记就查不到账**：provider 已把 `input/output/reasoning/cached` tokens 写进逐窗留档的 `usage` 字段；新写 provider 时保持这个习惯。
5. **测试有副作用**：`_cooldown_diag.txt` 会被调度器测试重写（现已由 `.gitignore` 覆盖，不再进版本控制）。
6. **md-math-normalizer 的 pytest 配置**：本机 env 没装 `pytest-timeout`，跑它的测试需要 `-o addopts=""`，并设置 `PYTHONPATH=src`。
7. **Windows 终端编码**：脚本内部已 `reconfigure(encoding="utf-8")`；跨进程调用时不要依赖系统默认代码页。

---

## 15. 验收清单（接手后建议先跑一遍）

```powershell
# 1) 两边测试全绿
cd D:\Documents\ProblemBank\tmp\mdprework\paddleocr-vl-md
C:\Users\Administrator\.conda\envs\pb\python.exe -m pytest tests -q      # 期望 195 passed
cd ..\md-math-normalizer
$env:PYTHONPATH="src"; C:\Users\Administrator\.conda\envs\pb\python.exe -m pytest -q -o addopts=""   # 期望 154 passed

# 2) 结构硬指标仍然成立（离线，不花钱）
cd ..\paddleocr-vl-md
C:\Users\Administrator\.conda\envs\pb\python.exe tools\block_evidence_benchmark.py `
  --evidence-dir D:\Documents\ProblemBank\storage\imports\math_olympic_2011_zk1_batch\blocks_20260918 `
  --output <tmp>\bench.json
#  期望：documents 18 / pages 124 / blocks 2245 / extraction_loss_blocks 0

# 3) 语义链离线自洽（假 provider，不花钱）
C:\Users\Administrator\.conda\envs\pb\python.exe semantic_baseline.py `
  --evidence-dir <同上> --provider fake --output <tmp>\fake.json
#  期望：hard_gates 全 true（覆盖完整 / schema 合法 / 无静默丢失 / reconcile 正常）
```

---

## 16. 关键决策记录（为什么是现在这个样子）

| 决策 | 原因 |
| --- | --- |
| 证据粒度用 Paddle 的 `parsing_res_list` | 实测 SDK 已提供 block_id/order/label/bbox/polygon/group_id；wrapper 丢掉，所以 producer 直连 SDK，不再基于"被压平的 Markdown"反推结构 |
| `sequence_index`（数组事实）与 `provider_block_order`（provider 字段）分开 | `block_order` 有 11%–33% 为 null，排序不能依赖它；事实与解释分离 |
| 图片默认落盘、缺图按渲染器语义判定 | 素材是证据的一部分；"远端引用 / 本地路径不存在"都算未本地化，`data:` 不算缺失 |
| Normalized View 只做派生、fatal 不丢块 | 语义层需要始终能看到原文；格式层失败不该让语义层失去证据 |
| 孤立数字不再自动数学化 | 由项目负责人裁决：数学意义上的数字 ≠ 需要数学排版；`standalone_numeric_wraps` 从 538 降到 0 |
| 语义层用 5-block / stride=3 + A–F 自适应扩窗 | 弱对齐实测 p50=2、p90=5 块，5 块覆盖约 90% 题目；跨页不重置窗口 |
| 模型只产出结构操作（ROLE/BOUNDARY/SPLIT），assembler 确定性物化 | LLM 不写正文，结构落地可复算、可回归 |
| 删除 `temp` 与 `codex/problembank-ocr-producer` | 前者内容已并入 main，后者是重复 producer；保留会形成两套同源实现 |
