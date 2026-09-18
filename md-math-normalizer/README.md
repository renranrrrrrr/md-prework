# md-math-normalizer

中文数学 Markdown 的确定性数学环境规范化程序（OCR 后处理）

> 完整的工程流程、规则执行顺序与决策记录见 [`docs/全流程设计文档.md`](docs/全流程设计文档.md)；
> 技术细节、关键实现与实测数据见 [`docs/技术报告.md`](docs/技术报告.md)。
> OCR 侧工具（PDF → Markdown、可疑行标记、批处理与并发编排）在 `../paddleocr-vl-md/`，
> 与本项目通过文件契约衔接，两者是 `D:\Tools\` 下的同级目录。

## 项目用途

本项目将 OCR 输出的中文数学 Markdown 进行**格式级别**的规范化，不做数学语义纠错。目标是：

- 保护代码、HTML、URL、链接等非正文区域
- 规范化可确定的 Unicode 与全角符号
- 识别现有数学环境并补齐裸露数学字符
- 合并可确定合并的内联数学环境
- 为行内大运算符补充 `\displaystyle`
- 严格验证并保证幂等

## 明确不处理的内容

- 不修复 OCR 数学语义
- 不修复 OCR 中文文本
- 不猜测缺失符号、修复公式内容

## 安装

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"   # Windows
# python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"   # POSIX
```

运行时只依赖 Python 标准库；`pytest` 与 `pytest-timeout` 仅用于开发。

## CLI 使用方式

```bash
md-math-normalizer input.md -o output.md
md-math-normalizer input.md --in-place
md-math-normalizer input.md --check
```

参数校验：

- `-o/--output` 与 `--in-place` 不能同时出现
- `--check` 不能与 `-o` 或 `--in-place` 同时出现
- 冲突时打印参数错误并返回退出码 2

退出码：

| 退出码 | 含义 |
| --- | --- |
| 0 | 文件已符合规范（`--check`），或处理成功（`-o` / `--in-place`） |
| 1 | 文件可以由程序安全自动规范化，但当前尚未规范化 |
| 2 | 参数错误、输入非法，或文件存在无法安全自动处理的问题 |

## Python API

```python
from md_math_normalizer import (
    normalize_markdown_text,
    normalize_markdown_file,
    check_markdown_file,
)
```

`check_markdown_file` 返回 `CheckResult`，字段含义：

- `is_valid`：文本已经是规范形式（固定点）
- `needs_normalization`：需要规范化，且可以安全自动完成
- `conforming`：同 `is_valid`，供 CLI/GUI 判断“无需修改”
- `has_fatal_error`：存在无法安全自动处理的问题（未闭合定界符、非法嵌套等）
- `diagnostics`：诊断列表；`WARNING` 表示可自动修复，`ERROR` 表示无法安全处理

## 转换规则简表

1. 非 protected 区域执行字符标准化：只替换 `config.py` 中显式列出的 ASCII 等价字符
   （全角 ASCII 变体、中文标点、顿号转 `,`），数学符号与数字变体保持不变
2. 识别 `$...$` 和 `$$...$$`；未闭合或非法嵌套一律报错，不做猜测修复
3. 仅在正文 Text Span 中识别裸露数学候选并加数学环境；下标、上标、花括号组和
   LaTeX 命令（`x_1`、`a_{n+1}`、`\frac{a}{b}`、`sum_{i=1}^n a_i`）作为整体处理
4. 合并可确定合并的相邻内联数学环境；汉字、换行、Markdown 结构字符和 protected
   区域阻止合并。间隙内容（含空格与标点）原样保留
5. 为包含大算符的内联环境在内容开头补 `\displaystyle`；块级数学与已有标记不动
6. 严格复检并做幂等性检查：第二步结果必须与第一步完全一致

题号（行首 `1.`）与分值（`16分`、`（16分）`）中的数字不进入数学环境；**孤立的纯数字
字面量**（`10`、`3.14`、`50%`）同样不自动数学化——数学意义上的数字不等于需要数学排版。
只有与变量、运算符、上下标或 LaTeX 命令共同出现时（`x=2`、`2^n`、`a_2`、`3m+4`），
整个表达式才作为数学候选。

## 错误策略

- 对不安全问题直接报错（error）
- 不进行启发式猜测修复
- 无法确定时保持原样
- 正式输出只有 `.md`，不生成任何 sidecar 文件

## 示例

输入：

```text
1. 设 a、b、c 为正实数，且 a+b=10.
9.（16分）求 sum_{i=1}^n a_i.
```

输出：

```text
1. 设 $a, b, c$ 为正实数, 且 $a+b=10$.
9.(16分)求 $sum_{i=1}^n a_i$.
```

其中 `sum` 保持原样：程序只做格式规范化，不把它改成 `\sum`。

## 运行测试

```bash
pytest
```

## 规格外扩展说明

`src/md_math_normalizer/gui.py`、`launcher.pyw`、`启动数学规范化工具.pyw/.bat` 以及
CLI 的无参数交互模式，都是**规格书之外的便利扩展**（规格第一版明确不包含 GUI 与批量
文件夹处理）。它们**不包含任何独立处理逻辑**，只调用与 CLI 相同的核心实现：

- GUI 调用 `check_markdown_file` / `normalize_markdown_file`
- 交互模式调用同样的两个 API，并以 `xxx改版.md` 作为输出名
- `python -m md_math_normalizer`、`md-math-normalizer` 与控制台脚本走同一入口

因此这些入口的规范化结果与 CLI 完全一致；如需严格的单文件契约，只使用
`md-math-normalizer input.md -o output.md` 即可。
