# 语义组装无静默丢块修复与离线审计

检查日期：2026-10-23。基线为 `main` 提交 `6d1166a830a476032ce7c9f64ece8c1f355b8894`。

## 实际复现与修复

使用合成输入分别执行原提交和修复后的代码，确认以下两条共享块遗漏路径：

| 输入路径 | 原实现 | 修复后 |
| --- | --- | --- |
| `PROBLEM_START → SHARED_CONTEXT → EOF` | 共享块没有引用、排除或诊断；`run_document` 报 `silent_loss=1`，质量门失败 | `silent_loss=0`；共享块登记为 `orphan_shared_context`，保留人工核查入口 |
| `PROBLEM_START → SHARED_CONTEXT → PROBLEM_CONTINUATION`，最后一块含有效 SPLIT | 事后 SPLIT 直接创建 Candidate，不消费待处理共享块，仍遗漏 1 块 | SPLIT 在文档流中创建新题并消费共享块；共享块被新题引用 |

这里的 1 块是合成用例的实测值，与历史报告的 15 块没有数量上的推断关系。

修复内容：

- 文档结束时为未分配的共享块登记完整块 ID 与诊断，不推测其属于上一题。
- SPLIT 改为按文档顺序即时切分，候选顺序及 ID 稳定；切分前的解析保留原归属，切分后的续写与解析只挂到新题，不重复复制解析。新题题号从切分后的原文抽取。
- 对零起点、越界、反向、空 anchor 区间及非整数 offset 保留原引用并登记 `invalid_split`。零起点在旧实现中会因遍历时追加 Candidate 而循环；回归测试以带超时的子进程验证。
- 非题目块、孤立续写等不能应用 SPLIT 时登记 `unapplied_split`，不存在的块登记 `split_unknown_block`。
- 跨窗口及同块的不同切分起点不再采用第一次提议；保留 `conflict` 与全部提议。相同起点的长短 anchor 可以合并。当前单块多个不同起点交人工处理。
- 保留原有 `diagnostics: list[str]`，新增 `diagnostic_records`，逐条保存 `code` 与完整 `block_ref`；孤立解析、孤立续写、未知角色、缺失角色均可追踪。
- 原始块缺失或重复身份时明确抛错，避免集合去重掩盖不同原始块；不修改 Evidence 补造身份。
- 新增 `audit_block_coverage`：精确核对块 ID 和字符区间并集，部分引用不算整块覆盖。读取旧产物时只识别已知诊断格式，避免 `b0001` 与 `b00010` 的前缀误匹配。
- baseline 输出具体遗漏 ID、部分引用及非法引用；增加 `assembler_reference_integrity` 门，聚合实际 `silent_loss`。同时修复显式 `dump_dir` 不写出语义快照的问题，并区分汇集目录内不同文档的文件名。

保留 `problem-candidates/v1` 原有字段；新增字段为附加信息。OCR Producer、Evidence 与正文内容均未改写。

## 离线验证

| 检查 | 结果 |
| --- | --- |
| 原提交 `paddleocr-vl-md/tests` | 195 passed |
| 修复后 `paddleocr-vl-md/tests` | 254 passed；比原提交增加 59 项 |
| 未修改的 `md-math-normalizer/tests` | 154 passed |
| 原版与新版 `run_document` 最小共享块用例 | 质量门由失败变为通过，遗漏由 1 变为 0 |
| 共享块后由 SPLIT 创建新题 | 遗漏由 1 变为 0，共享块有实际题目引用 |
| Evidence 不变性 | 文件字节比较与合成用例 SHA-256 比较均不变 |

修复后的完整组装测试还在禁止当前测试进程建立 socket 连接的条件下通过。测试只使用已有 fixture、合成 Evidence、假 provider 或缓存预测，没有调用真实 OCR 或 LLM API。

回归覆盖尾部共享块、全共享文档、孤立解析与续写、跨块跨页题目、多块 SPLIT、切分后解析归属、映射顺序无关性、切分端点、重复及冲突 anchor、旧诊断前缀碰撞、部分区间遗漏、重复身份和离线重放。另穷举了长度 0–4 的角色序列，共 4681 种组合，独立检查每块都有引用、排除或结构化诊断。

在相应项目目录执行，命令各占一行：

```powershell
python -m pytest tests -q
```

格式模块在 `md-math-normalizer` 目录执行：

```powershell
$env:PYTHONPATH="src"
python -m pytest tests -q -o addopts=""
```

## 历史 15 块：本次不能确认复现

交接文档记载的历史真实 baseline 为 18 份试卷、2245 块、742 窗口，语义静默遗漏 15 块。本次环境没有挂载其 `D:\Documents\ProblemBank\storage\...\blocks_20260918` 数据，也未在提供的工作区中找到对应真实 Evidence 与 semantic-run。仓库只有代码和测试 fixture。

因此，本次确认的是代码中的可复现遗漏路径，不能断言历史 15 块均由这些路径导致，也不能把当前历史数据的遗漏数量写成 0。旧报告中的数量未作为本次实测结果。

已提供 `tools/replay_semantic_assembly.py`，从原 Evidence 与缓存语义记录重新核查，不需要格式视图、OCR 或 LLM provider，也不会覆盖原产物。它分别输出：

1. `before`：原语义快照中的候选覆盖情况及实际遗漏 ID。
2. `with_recorded_splits`：固定旧 `reconciled/splits`，只更换 assembler 的覆盖情况，用于区分组装器影响。
3. `after`：保留原角色决策，从缓存 predictions 重新汇总 SPLIT 后的覆盖情况；没有缓存 predictions 则使用原 splits。
4. `historical_loss_roles`、修复后的候选引用和结构化诊断，供逐块复核。

在 `paddleocr-vl-md` 目录对真实本机数据执行：

```powershell
python tools/replay_semantic_assembly.py --evidence-dir "D:\Documents\ProblemBank\storage\imports\math_olympic_2011_zk1_batch\blocks_20260918" --output "D:\Documents\ProblemBank\tmp\assembly-audit-20261023.json"
```

如果语义快照在另外的汇集目录，可增加 `--run-dir "<快照目录>"`。输出必须是新文件。工具会校验 `document_id` 和 `evidence_hash`；缺失快照、身份不匹配或空样本不会报告为成功。`complete=false` 时，汇总数字只涉及成功重放的文档，不能当作完整 baseline。

原 baseline 命令默认跳过已存在的语义产物；不要把跳过旧产物当成已验证此次修复。核实历史结果应使用上述离线重放。

## 仍需人工核查

- `orphan_shared_context` 的实际作用范围；本次只消除静默遗漏，不自动判断尾部材料属于哪道题。
- `orphan_solution`、`orphan_continuation` 是否来自漏判题首、独立答案区或文档缺页。
- `UNCERTAIN`、重叠角色或 boundary 冲突，以及 `unresolved_split`、`invalid_split`、`unapplied_split` 对应的原文。
- 单块有多个不同题目起点时，本版登记冲突并保留整块，尚未自动进行任意多题切分。
- 共享材料沿用“归给其后第一道题”的现有规则；适用多道题的真实范围、独立答案区与题目的匹配、单独 boundary 的语义约束仍需复核。
- `assembler_no_silent_loss` 通过说明每个原始块可追踪，不等价于所有题目边界与解析配对都正确。诊断未处理完不应视为语义质量已经验收。
