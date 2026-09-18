# pdf2md —— 独立 PDF → Markdown 工具

用 **PaddleOCR-VL 的 AI Studio API** 把 PDF 转成 Markdown。
不依赖 DSH、不依赖任何智能体、不启动 MCP 服务、不做本地推理（不装 paddlepaddle）。
它只是一个普通的命令行程序，调 HTTP API。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `ocr_producer.py` | **唯一 producer 核心**：调 PaddleOCR-VL、保存结构化证据、下载并校验图片、远端压力重试 |
| `prework_ocr.py` | **编排入口**（推荐）：`convert` / `batch`、`--output-dir`、`--stdout`、`ocr_status.jsonl` / `ocr_summary.json` |
| `pdf2md.py` | 人用前端（兼容旧用法）：位置参数、产物写在 PDF 同目录；逻辑全部委托给 `ocr_producer.py` |
| `ocr_mark.py` | 只标记可疑行（页眉残片、标题异常），**不修改文件** |
| `run_pipeline.py` | 单文件全流程：OCR → 规范化 → 复检 → 标记 |
| `run_pipeline_parallel.py` | **多 PDF 受限并发 + 自适应限流**：每个 PDF 一个独立子进程 |
| `remote_pressure.py` | 远端错误分类（背压 / 额度耗尽 / OCR 失败 / 环境错误），纯函数 |
| `tools/` + `capability/` | PaddleOCR-VL 结构化返回的能力探测、报告与脱敏 fixture（见 `capability/README.md`） |
| `运行全流程.cmd` | **拖拽入口**：把文件/文件夹直接拖上去即可（串行） |
| `pdf2md.cmd` | 只跑 OCR 的包装脚本 |
| `requirements.txt` | 唯一依赖 `paddleocr-mcp>=0.8.5`（不装 paddlepaddle） |
| `.venv/` | 独立虚拟环境 |
| `tests/` | 调度器 / 限流 / 冷却恢复 / 证据与图片校验自动测试（假 worker 与假客户端，不触碰远端） |

## 唯一 producer：OCR → 结构化证据

两套历史 producer 已合并成一份实现（`ocr_producer.py`），入口是 `prework_ocr.py`：

```powershell
# 单份：证据 + Markdown + 图片都写进受控目录
.\.venv\Scripts\python.exe prework_ocr.py convert `
  --input .\pdfs\paper.pdf --output-dir .\out\run-1 --keep-images

# 批量：额外写 ocr_status.jsonl；--jobs N 复用调度器的自适应冷却
.\.venv\Scripts\python.exe prework_ocr.py batch `
  --pdf-dir .\pdfs --output-dir .\out\run-1 --jobs 2

# 被编排调用：Markdown 走 stdout，进度与警告走 stderr（只允许单个输入）
.\.venv\Scripts\python.exe prework_ocr.py convert `
  --input .\pdfs\paper.pdf --output-dir .\out\run-1 --stdout
```

产物：

```text
<output-dir>/
  <stem>.md               # Markdown 视图（图片 src 已改成本地相对路径）
  <stem>.evidence.json    # 不可变结构化证据（md-prework/ocr-evidence/v1）
  <stem>_raw/page-0001.json   # 每页原始 prunedResult，逐字节保留
  <stem>_media/           # 图片按文件头校验后落盘
  ocr_status.jsonl        # batch 模式：每份一行状态
  ocr_summary.json        # 每次运行：汇总（含每份 pages/blocks/images/失败原因）
```

证据分三级（见 `capability/README.md` 的实跑证据）：

1. **Stable Core**（`<stem>.evidence.json`）：`block_ref` / `provider_block_id` /
   `provider_block_order`（Paddle 字段，可为 null）/ `sequence_index`（数组位置，事实）/
   `label` / `content` / `bbox` / `polygon` / `group_id`，以及每页的 `page_seq`、
   `page_index_source`、`coordinate_space`、`layout_detection.boxes_count`、`order_consistency`；
   **不写**解释性的 `reading_order`（留给语义层派生），也**不内嵌** provider raw；
2. **Provider Raw Page**（`<stem>_raw/page-NNNN.json`，默认开启）：每页完整 `prunedResult`；
3. **Heavy Binary**（`preprocessedImages` 之类）：只以引用 + sha256 形式出现，绝不内嵌 base64。

`--evidence-raw` 取值：`none`（测试/轻量）、`page`（生产默认）、`full`（调试，额外落作业信封，
写盘前剔除 token / 凭证 / 请求头）。证据身份用 `document_id = <stem>-<source sha256 前 8 位>`
与 `producer_config_hash` 记录，不把文件名当身份。

三条不变量：

1. **证据不可变**：`<stem>.evidence.json` 已存在时默认拒绝重跑（`--overwrite` 才覆盖），
   规范化、切题等任何派生结果都不得回写证据；
2. **不伪造图片**：下载失败或文件头不认识时只登记 `state=missing` 并警告，
   绝不把 Markdown 指向不存在的本地路径；
3. **不越界**：producer 不切题、不调 LLM、不写题库、不建索引。

完整的工程流程与规则顺序见同级规范化器项目的 [`../md-math-normalizer/docs/全流程设计文档.md`](../md-math-normalizer/docs/全流程设计文档.md)，
技术细节与实测数据见 [`../md-math-normalizer/docs/技术报告.md`](../md-math-normalizer/docs/技术报告.md)。

## 一次跑完全流程（推荐）

**最简单：把任意多个文件或文件夹拖到 `运行全流程.cmd` 上。**

命令行等价写法：

```powershell
.\.venv\Scripts\python.exe run_pipeline.py "D:\a\1.pdf" "D:\b\2.md" "D:\试卷"
```

对每个输入依次执行：

```text
[1/4] OCR        PDF → 同目录 .md（.md 输入自动跳过）
[2/4] 体检       --check，判定 0 已规范 / 1 可修复 / 2 需人工
[3/4] 规范化     生成 <同名>.规范化.md，原文件不动
[4/4] 复检       对规范化结果再 --check，必须为 0
[标记] 可疑行    只报告，不修改
```

选项：

| 选项 | 说明 |
| --- | --- |
| `--overwrite` | 覆盖已存在的 `.md` / `.规范化.md` |
| `--keep-images` | OCR 时导出文档内图片 |
| `--verbose` | 打印全部诊断（默认每阶段只显示前 5 行） |
| `--diagnostics-limit N` | 调整每阶段显示行数 |
| `--no-pause` | 结束不等待按键（脚本调用用） |

输入规则：目录只取**直接子级**的 `.pdf`/`.md`（不进子目录）；重复传入按绝对路径去重；`*.规范化.md` 不再作为输入，避免自我循环。

退出码：`0` 全部成功 / `1` 有失败项 / `2` 输入或环境问题 / `130` 中断。

## 多 PDF 并发（`run_pipeline_parallel.py`）

一个 PDF 从输入到产出「已规范化并通过检查的 Markdown」是一个**不可拆分的 worker 任务**；
并发只发生在不同 PDF 的完整任务之间，单个 PDF 内部仍由 `run_pipeline.py` 串行执行。

```powershell
# 两个 PDF 并行（默认 workers=2）
.\.venv\Scripts\python.exe run_pipeline_parallel.py "D:\a\1.pdf" "D:\b\2.pdf"

# 整个目录，最多 3 个同时跑
.\.venv\Scripts\python.exe run_pipeline_parallel.py "D:\试卷" --workers 3

# 完全串行（等价于逐个跑 run_pipeline.py）
.\.venv\Scripts\python.exe run_pipeline_parallel.py "D:\试卷" --workers 1
```

| 选项 | 说明 |
| --- | --- |
| `--workers N` | **最大**并发 worker 数，正整数，**默认 2**（不按 CPU 核数自动放大）。这是上限而不是保证值 |
| `--launch-interval S` | 两次启动新 worker 之间的最小间隔秒数，默认 `0.5`；`0` 表示不间隔（测试/本地服务用） |
| `--verbose` | worker 打印全部诊断 |
| `--overwrite` | 覆盖已存在的产物 |
| `--keep-images` | OCR 时导出文档内图片 |

`--workers > 4` 会给出一行告警（不阻塞执行）：远端 OCR 是共享配额服务，
并发开太大只会把限流触发得更早，建议先用默认 2 跑通再调。

行为要点：

- **每个 worker 是独立操作系统进程**（`subprocess` 启动 `run_pipeline.py`），不用线程池、不用进程池调用内部函数。
- 任意时刻活动子进程数 **≤ `--workers`**；有界队列，一个结束才补下一个。
- 状态行有三种：`OK`（真的做了 OCR + 规范化）、`SKIP`（产物已存在，worker 什么都没做，退 0）、
  `FAILED`（该任务失败，其余继续）。`SKIP` 单独计入 `Skipped:`，不会和真正处理的混淆。
- 只接受 `.pdf`（这条链路的目标就是 PDF → 最终 Markdown）；目录不递归。
- 同一 PDF 被多个入口引用时**只执行一次**（`resolve()` + 大小写归一 + 去重）。
- **失败隔离**：某个 PDF 失败只标记该任务，其余继续；`--workers 0`、找不到入口/解释器等
  才返回 2（全局性错误）。
- **日志隔离**：每个 worker 的 stdout/stderr 独立捕获，成功后只打印一行任务状态，
  失败才输出该任务自己的日志（带文件名标头），不会交叉。
- **Ctrl+C**：不再启动新任务、终止所有运行中的 worker、返回 `130`。
- 不自行写/删任何文件，全部产物由原 pipeline 的原子写入负责。

### 自适应限流（远端背压）

远端 OCR（AI Studio）会动态限流，硬并发迟早撞上。调度器只在 **“要不要再启动一个新 worker”** 这一点上
做自适应，**从不修改、不重跑、不杀死已经跑起来的 PDF pipeline**：

- **识别**：worker 的 stdout + stderr 一起过分类器，命中以下任一条即判为远端背压
  `REMOTE_BACKPRESSURE`：错误码 `10010`（任务提交队列已满）、`12002`（今日提交已达上限）、
  `HTTP 429`、`HTTP 503/504`、`queue_full` / `rate_limit` 文本。
- **全局冷却**：一旦触发，暂停启动新 worker，冷却时长按级数递增 `5 → 10 → 20 → 40 → 60` 秒。
  冷却只挡新任务；已在跑的 worker 继续跑完，不会被中断。
- **渐进恢复**：每成功一次，背压级数 **降 1 级**（不是清零），冷却时间随之减半，避免刚恢复就把并发打满
  又立刻被限流。连续失败才继续升级。
- **配额耗尽单独分类**：`今日提交任务已达上限` 这类**日报配额**判为 `QUOTA_EXHAUSTED`，
  **不计入背压、不触发冷却、不重试**——等下去也不会变好，报告直接说清楚，由人决定。
- **统计**：汇总里给出背压次数、配额事件次数、峰值冷却时长、峰值背压级数、以及各原因（HTTP 429/503 等）计数。
- **不重复执行**：被冷却挡住的任务只是**延后启动**，仍在待办队列里，每个 PDF 全程只跑一次完整 pipeline。

汇总形如：

```text
============================================================
处理完成
Total:                5
Succeeded:            2
Failed:               3
Skipped:              0
Workers:              2
Elapsed:              78.3s
Backpressure events:  3
Quota events:         0
Peak cooldown:        20s
Peak backoff level:   3
  Paddle queue full: 0
  HTTP 429: 2
  HTTP 503/504: 1
说明：3 个失败来自远端限流，可稍后用相同命令重跑这些文件（调度器本身不会自动重跑）。
Failed:
- BUSY_1.pdf  [REMOTE_BACKPRESSURE]  http_429
- BUSY_2.pdf  [REMOTE_BACKPRESSURE]  http_429
- DOWN_4.pdf  [REMOTE_BACKPRESSURE]  http_503
============================================================
```

`failure_kind` 取值：`REMOTE_BACKPRESSURE`、`QUOTA_EXHAUSTED`、`OCR_FAILURE`、
`PIPELINE_FAILURE`、`LOCAL_ENVIRONMENT`（正常完成则为 `NONE`）。

若规范化器不在默认位置，设置 `MD_MATH_NORMALIZER=<规范化器项目目录>`。

## 安装（一次性）

```powershell
cd D:\Tools\paddleocr-vl-md
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

没有 `uv` 就用标准库方案：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 令牌

脚本按以下顺序查找令牌：

1. `--token` 命令行参数
2. 进程环境变量 `PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN`
3. **用户级**环境变量 `PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN`（直接读注册表）

第 3 条是兜底：刚设置的用户变量不会进入已经在运行的终端或工具，脚本自己查注册表，
所以无需重启终端。

## 用法

### 模式 1：给一个文件夹

转换该目录下的全部 PDF —— **只转这个目录，不进子目录**。

```powershell
.\.venv\Scripts\python.exe pdf2md.py "D:\Users\lenovo\Documents"
```

```text
D:\Users\lenovo\Documents\1.pdf     → 会转
D:\Users\lenovo\Documents\1\2.pdf   → 不会转
```

### 模式 2：给一个或多个 PDF

```powershell
.\.venv\Scripts\python.exe pdf2md.py "D:\a\1.pdf"
.\.venv\Scripts\python.exe pdf2md.py "D:\a\1.pdf" "D:\b\2.pdf" "D:\c\3.pdf"
```

两种模式可以混用，重复传同一文件会自动去重：

```powershell
.\.venv\Scripts\python.exe pdf2md.py "D:\试卷" "D:\额外的题.pdf"
```

## 输出位置

**每个 PDF 的同目录**生成同名 `.md`：

```text
D:\试卷\第一套.pdf        →  D:\试卷\第一套.md
D:\试卷\第一套_media\     （加了 --keep-images 且文档含图时才有）
```

已存在同名 `.md` 时默认**跳过**，不覆盖；要覆盖加 `--overwrite`。

## 选项

| 选项 | 说明 |
| --- | --- |
| `--overwrite` | 覆盖已存在的同名 `.md`（默认跳过） |
| `--keep-images` | 把文档内图片导出到 `<同名>_media/`（默认只写 markdown 文本） |
| `--model` | 模型名，默认 `PaddleOCR-VL-1.6` |
| `--poll-timeout` | 单份 PDF 总轮询超时秒数，默认 900 |
| `--retries` | 遇到配额/排队错误时的总尝试次数，默认 3 |
| `--retry-delay` | 首次重试等待秒数，之后翻倍，默认 10 |
| `--token` | 直接传令牌，覆盖环境变量 |

退出码：`0` 全部成功；`1` 有文件失败；`2` 输入或依赖有问题；`130` 手动中断。

单个 PDF 失败不会中断整批，会打印失败原因后继续处理后面的文件。

## 常见服务端报错

AI Studio 是共享服务，报错通常来自服务端而不是脚本：

| 报错 | 含义与处理 |
| --- | --- |
| `任务提交队列已满，请稍后重试` | 服务端排队满。脚本会自动退避重试；仍失败就过几分钟再跑 |
| `今日提交任务已达上限` | 当日配额用尽，等次日或换 `PaddleOCR-VL` 之外的模型 |
| `Authentication failed` | 令牌无效或过期，去 AI Studio 重新生成 |

重试只针对"配额/排队/繁忙"类错误；参数错误、文件不存在这类不会被重试。

## 图片导出说明

文档解析会同时返回正文图片。默认只写 markdown 文本、**不落盘图片**（避免意外生成大量文件）；
加 `--keep-images` 才导出到 `<同名>_media/`，并按原路径保留子目录结构，同时把 markdown 里
`<img src="...">` 的路径改写成实际位置（只改 `src` 属性取值，不动其它内容）。

导出行为要点：

- 图片托管在 CDN 上，AI Studio 返回的是**图片 URL**（本地推理返回 base64，两者都支持）；
  URL 下载失败会**退避重试 3 次**，避免把偶发的 SSL 抖动变成永久缺失。
- 落盘前按文件头校验格式，识别不出格式的负载会被跳过并报告，不会写出坏图片。
- **下载最终失败的图片，其 markdown 引用会保留远端 URL 并打印警告**——不会把引用改成
  不存在的本地路径。看到这类警告就要重跑一次（加 `--overwrite`）。

## 输出规范化（pdf2md 自动做的小清理）

1. 去掉每行的**行尾空白**。OCR 会输出 `  $$ ... $$ `（前后带空格），行尾空格会让标准
   Markdown 渲染器认不出 `$$` 块。块级/行内公式的内容一律不动。
2. 图片引用改写（见上）。除此之外**不改动 OCR 输出的任何内容**，包括它那种
   `$ y = \sin x $` 的宽松空格写法。

## 可疑行标记：ocr_mark.py

页眉/页脚残片混进正文是 OCR 的常见问题，但"这一行到底是页眉还是正文"需要版面上下文，
纯本地规则无法可靠判断——**误删正文的代价远大于留下噪音**。所以本工具只扫描、只报告，
**绝不修改文件**。

```powershell
# 单个文件
.\.venv\Scripts\python.exe ocr_mark.py "D:\试卷\第一套.md"

# 整个目录（只取该目录下的 .md，不进子目录）
.\.venv\Scripts\python.exe ocr_mark.py "D:\试卷"

# JSON 输出，便于后续处理
.\.venv\Scripts\python.exe ocr_mark.py "D:\试卷\第一套.md" --json
```

检测规则：

| 规则 | 判定 | 例子 |
| --- | --- | --- |
| R1 | 整行像「页码 + 书名」，且行内无 `$` | `1 三角函数的图象与性质`、`___ 1 三角函数的图象与性质` |
| R2 | 标题里中文之间夹空格 | `## 三 角函数` |
| R3 | 标题以解题语气词开头 | `## 分析 正确理解所给等式…` |
| R4 | 引用了名字含 `header` 的图片 | `<img src="imgs/img_in_header_image_box...">` |
| R5 | 空标题 | 只有一个 `#` |

退出码：`0` 没有可疑行；`1` 有可疑行待人工确认；`2` 输入有问题。

实测在该项目 20 页样例上标出 7 处，与人工通读结论完全一致（4 处页眉残片、1 处误判标题、
1 处页眉残片标题、1 处结尾乱码），无漏报、无误报。

## 和 md-math-normalizer 的配合

三者通过文件契约衔接，互不依赖：

```text
PDF ──(pdf2md)──> 同目录 .md ──(md-math-normalizer)──> 规范化后的 .md
                        └──(ocr_mark)──> 可疑行报告（不改文件）
```

```powershell
# 两个项目是同级目录（例如都在 D:\Tools\ 下），按相对路径引用
$tools = "D:\Tools"                                   # 改成你自己的上层目录
$tool  = Join-Path $tools "paddleocr-vl-md"           # 本工具
$norm  = Join-Path $tools "md-math-normalizer"        # 规范化器
$normPy = Join-Path $norm ".venv\Scripts\python.exe"  # 用 -m 调用最稳

# 1. OCR
& "$tool\.venv\Scripts\python.exe" "$tool\pdf2md.py" "D:\试卷" --keep-images

# 2. 标记可疑行（人工确认，工具不改文件）
& "$tool\.venv\Scripts\python.exe" "$tool\ocr_mark.py" "D:\试卷"

# 3. 体检 + 规范化
& $normPy -m md_math_normalizer "D:\试卷\第一套.md" --check      # 0 已规范 / 1 可修复 / 2 需人工
& $normPy -m md_math_normalizer "D:\试卷\第一套.md" -o "D:\试卷\第一套.规范化.md"
& $normPy -m md_math_normalizer "D:\试卷\第一套.规范化.md" --check  # 应为 0
```

> 也可以直接跑 `run_pipeline.py` 或 `run_pipeline_parallel.py`，它们会自己按相对位置
> 找到规范化器（也支持用 `MD_MATH_NORMALIZER` 环境变量指定），无需手写这些路径。
> 单独调用规范化器时优先用 `python -m md_math_normalizer`：pip 生成的 `.exe` 启动器在
> 项目被移动后可能失效，重装（`pip install -e .`）即可恢复。

规范化器会：把中文标点全部换成英文标点、把公式外的裸变量与数字包进数学环境、给含大算符的
行内公式补 `\displaystyle`，并**原样保留** OCR 的宽松公式形态。若 `$` 不配对会报退出码 2 ——
按设计它不猜公式边界。

