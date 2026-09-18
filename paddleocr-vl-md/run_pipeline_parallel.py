"""run_pipeline_parallel —— 多 PDF 受限并发入口。

一个 PDF 从输入到产出「已规范化并通过检查的 Markdown」是一个**不可拆分的
worker 任务**；并发只发生在不同 PDF 的完整任务之间。单个 PDF 内部仍由
`run_pipeline.py` 串行执行，本脚本不复制也不改写它的任何处理逻辑。

    PDF A ─┐
    PDF B ─┼─► 最多 N 个 run_pipeline.py 子进程同时运行
    PDF C ─┘

用法：

    run_pipeline_parallel.py A.pdf B.pdf "D:\\试卷"
    run_pipeline_parallel.py "D:\\试卷" --workers 2

只接受 `.pdf` 输入（PDF → 最终规范化 Markdown）。目录只取直接子级、不递归。

退出码沿用项目约定：
    0   全部 PDF 成功
    1   至少一个 PDF 任务失败
    2   参数 / 环境 / 入口等全局性错误
    130 用户中断
"""

from __future__ import annotations

import argparse
import collections
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import remote_pressure  # noqa: E402  远端压力分类的唯一真源
from remote_pressure import should_cool_down  # noqa: E402

TOOL_DIR = pathlib.Path(__file__).resolve().parent
RUN_PIPELINE = TOOL_DIR / "run_pipeline.py"

DEFAULT_WORKERS = 2
OUTPUT_TAG = ".规范化"

# 服务端压力时的冷却策略：指数退避，上限 60s
COOLDOWN_STEPS = (5.0, 10.0, 20.0, 40.0, 60.0)
MAX_BACKOFF_LEVEL = len(COOLDOWN_STEPS)

# 连续启动两个 worker 之间的最小间隔，避免同一毫秒内向远端突发提交
DEFAULT_LAUNCH_INTERVAL = 0.5

# workers 超过该值即提示（不限制用户）
WORKERS_WARN_THRESHOLD = 4

# worker 因产物已存在而跳过时打印的标记（与 pdf2md.py 的 [跳过] 输出对应）
_SKIP_MARKER = "[跳过]"

# 调度器自身的致命问题（区别于「某个 PDF 处理失败」）
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_FATAL = 2
EXIT_INTERRUPTED = 130


# ---------------------------------------------------------------- 工具定位


def find_tool_python() -> pathlib.Path:
    """worker 用工具自己的虚拟环境解释器。"""
    for candidate in (
        TOOL_DIR / ".venv" / "Scripts" / "python.exe",
        TOOL_DIR / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return pathlib.Path(sys.executable)


def _force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- 数据结构


@dataclass
class TaskResult:
    """一个 PDF worker 的完整结果。"""

    source: pathlib.Path
    returncode: int
    stdout: str = ""
    stderr: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    failure_kind: str = remote_pressure.NONE
    # 失败细分（queue_full / rate_limit / http_429 / http_503 / quota_exhausted…）
    failure_reason: str = remote_pressure.REASON_OTHER
    # worker 因产物已存在而主动跳过（退出码 0，但没做任何 OCR/规范化）
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def elapsed(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


@dataclass
class ScheduleStats:
    """调度过程的压力统计（用于最终汇总）。"""

    backpressure_events: int = 0
    quota_events: int = 0
    peak_backoff_level: int = 0
    peak_cooldown: float = 0.0
    # 细分计数，只统计真实检测到的原因
    reasons: dict[str, int] = field(default_factory=dict)

    def record(self, verdict, cooldown: float) -> None:
        """记录一次远端压力事件。

        只有**临时**压力计入 backpressure_events；额度耗尽单独计数——它不触发冷却，
        也不该被当成"服务端繁忙"。
        """
        if should_cool_down(verdict):
            self.backpressure_events += 1
        elif verdict.kind == remote_pressure.QUOTA_EXHAUSTED:
            self.quota_events += 1
        self.reasons[verdict.reason] = self.reasons.get(verdict.reason, 0) + 1
        self.peak_cooldown = max(self.peak_cooldown, cooldown)

    @property
    def quota_exhausted_events(self) -> int:
        return self.reasons.get(remote_pressure.REASON_QUOTA, 0)


@dataclass
class SourceChecks:
    """一个来源（文件或目录）检查结果的容器。"""

    pdfs: list[pathlib.Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 输入展开


def collect_pdfs(raw_inputs: list[str]) -> tuple[list[pathlib.Path], list[str]]:
    """展开输入为 PDF 列表，并做 resolve + 大小写归一 + 去重。

    目录只扫描直接子级（`iterdir`，不递归），只挑 `.pdf`，忽略其他文件。
    """
    result = SourceChecks()
    seen: set[str] = set()

    def _add(path: pathlib.Path) -> None:
        # resolve() → canonical path；Windows 路径大小写不敏感，统一小写做键
        key = os.path.normcase(str(path.resolve()))
        if key in seen:
            return
        seen.add(key)
        result.pdfs.append(path)

    for raw in raw_inputs:
        path = pathlib.Path(raw).expanduser()
        if not path.exists():
            result.problems.append(f"路径不存在：{path}")
            continue
        if path.is_dir():
            found = sorted(
                entry
                for entry in path.iterdir()
                if entry.is_file() and entry.suffix.lower() == ".pdf"
            )
            if not found:
                result.problems.append(f"目录下没有 .pdf（已忽略子目录）：{path}")
                continue
            for entry in found:
                _add(entry)
            continue
        if path.suffix.lower() != ".pdf":
            result.problems.append(f"只接受 .pdf，已跳过：{path}")
            continue
        _add(path)

    return result.pdfs, result.problems


def detect_output_conflicts(pdfs: list[pathlib.Path]) -> list[str]:
    """检测两个不同 PDF 是否映射到同一输出路径。

    输出命名规则来自 run_pipeline.py：<stem>.md 与 <stem>.规范化.md。
    同一 PDF 已去重，剩下的冲突只可能来自不同目录却同名同级的文件——
    实际上不同目录不会冲突，真正会撞车的是「同一目录下同名」，
    而同一目录不可能有两个同名文件；这里仍显式校验，避免依赖假设。
    """
    conflicts: list[str] = []
    owners: dict[str, pathlib.Path] = {}
    for pdf in pdfs:
        for suffix in (".md", f"{OUTPUT_TAG}.md"):
            target = pdf.with_name(f"{pdf.stem}{suffix}")
            key = os.path.normcase(str(target.resolve()))
            other = owners.get(key)
            if other is None:
                owners[key] = pdf
            elif os.path.normcase(str(other.resolve())) != os.path.normcase(
                str(pdf.resolve())
            ):
                conflicts.append(
                    f"输出路径冲突：{other} 与 {pdf} 都会写 {target}"
                )
    return conflicts


# ---------------------------------------------------------------- worker


def build_worker_command(
    pdf: pathlib.Path,
    *,
    python: pathlib.Path,
    verbose: bool,
    overwrite: bool,
    keep_images: bool,
) -> list[str]:
    """构造 worker 命令：复用现有单文件入口，不复制其逻辑。"""
    command = [str(python), str(RUN_PIPELINE), str(pdf), "--no-pause"]
    if verbose:
        command.append("--verbose")
    if overwrite:
        command.append("--overwrite")
    # 图片默认导出；这里始终显式传一个开关，命令本身就能看出本次是存图还是丢图。
    command.append("--keep-images" if keep_images else "--no-images")
    return command


def launch(command: list[str]) -> subprocess.Popen[str]:
    """启动一个 worker 子进程，stdout/stderr 各自独立管道。"""
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(TOOL_DIR),
        # 独立进程组，便于中断时整组终止
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if os.name == "nt"
        else 0,
    )


def terminate(process: subprocess.Popen[str]) -> None:
    """终止子进程（含其子进程），并回收。"""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------- 冷却策略


def cooldown_for_level(level: int) -> float:
    """退避等级 → 冷却秒数：5 → 10 → 20 → 40 → 60（上限 60）。"""
    if level <= 0:
        return 0.0
    index = min(level, MAX_BACKOFF_LEVEL) - 1
    return COOLDOWN_STEPS[index]


class Backoff:
    """调度器的自适应提交节流状态。

    只控制「下一个 PDF 什么时候提交」，不碰任何正在运行的 worker：
      * 检测到临时服务端压力 → level +1，冷却 = cooldown_for_level(level)
      * 一个任务正常完成     → level -1（最低 0）
      * 普通失败（非压力）   → level 不变
    """

    def __init__(self, clock=time) -> None:
        self._clock = clock
        self.level = 0
        self.until = 0.0            # 冷却结束的单调时刻
        self.last_launch: float | None = None

    # ---- 状态 ----------------------------------------------------------
    def cooldown_remaining(self) -> float:
        return max(0.0, self.until - self._clock.monotonic())

    def in_cooldown(self) -> bool:
        return self.cooldown_remaining() > 0.0

    def note_backpressure(self) -> float:
        """记录一次服务端压力，返回本次冷却秒数。"""
        self.level = min(self.level + 1, MAX_BACKOFF_LEVEL)
        cooldown = cooldown_for_level(self.level)
        self.until = self._clock.monotonic() + cooldown
        return cooldown

    def note_success(self) -> None:
        """一个任务正常完成后逐步恢复。"""
        self.level = max(0, self.level - 1)

    # ---- 提交前的等待 ---------------------------------------------------
    def wait_before_launch(self, launch_interval: float) -> float:
        """计算并消耗本次提交前必须等待的时间，返回等待秒数（便于测试断言）。"""
        waited = 0.0

        remaining = self.cooldown_remaining()
        if remaining > 0:
            self._clock.sleep(remaining)
            waited += remaining

        if launch_interval > 0 and self.last_launch is not None:
            gap = self._clock.monotonic() - self.last_launch
            if gap < launch_interval:
                self._clock.sleep(launch_interval - gap)
                waited += launch_interval - gap

        self.last_launch = self._clock.monotonic()
        return waited


# ---------------------------------------------------------------- 调度核心


def run_bounded(
    pdfs: list[pathlib.Path],
    *,
    workers: int,
    python: pathlib.Path,
    verbose: bool,
    overwrite: bool,
    keep_images: bool,
    launch_interval: float = DEFAULT_LAUNCH_INTERVAL,
    clock=time,
    wait_any=None,
    launch_worker=None,
    stats: ScheduleStats | None = None,
    on_start=None,
    on_finish=None,
    on_backpressure=None,
) -> list[TaskResult]:
    """受限并发执行：任意时刻 active 子进程数 <= workers，并按远端压力自适应减速。

    结构上直接可见「最多 N 个 subprocess」：一个待处理队列 + 一个 `active` 字典
    （长度永不超过 workers）。服务端出现临时压力时，只是**延迟下一个任务的提交**，
    绝不终止或暂停任何正在运行的 worker。

    每个 worker 都是独立操作系统进程，不调用内部 pipeline 函数。
    ``clock`` 用于注入虚拟时钟（测试不真 sleep）；生产使用真实 time 模块。
    """
    if workers < 1:
        raise ValueError("workers 必须 >= 1")

    stats = stats if stats is not None else ScheduleStats()
    backoff = Backoff(clock)
    # 等待/启动函数可注入，便于测试替换（默认是模块级实现，测试可 monkeypatch）
    waiter = wait_any if wait_any is not None else subprocess_wait_any
    launcher = launch_worker if launch_worker is not None else launch
    pending: collections.deque[pathlib.Path] = collections.deque(pdfs)
    active: dict[subprocess.Popen[str], tuple[pathlib.Path, float]] = {}
    results: dict[pathlib.Path, TaskResult] = {}
    order = {pdf: index for index, pdf in enumerate(pdfs)}

    def _admission_open() -> bool:
        return len(active) < workers and not backoff.in_cooldown()

    def _fill() -> None:
        """在有名额且不在冷却时尽量补位；冷却只影响这里。"""
        while pending and _admission_open():
            pdf = pending.popleft()
            backoff.wait_before_launch(launch_interval)
            command = build_worker_command(
                pdf,
                python=python,
                verbose=verbose,
                overwrite=overwrite,
                keep_images=keep_images,
            )
            process = launcher(command)
            active[process] = (pdf, clock.monotonic())
            if on_start is not None:
                on_start(pdf)

    try:
        _fill()
        # 只要还有在跑的、或还有排队的（可能正在冷却），就继续调度。
        # 冷却期间 active 可能为空，此时必须先等冷却结束再补位，否则会漏掉排队任务。
        while pending or active:
            if active:
                finished = waiter(active)
            else:
                backoff.wait_before_launch(0.0)
                _fill()
                continue

            for process in finished:
                pdf, started = active.pop(process)
                stdout = process.stdout.read() if process.stdout else ""
                stderr = process.stderr.read() if process.stderr else ""
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

                # 失败时按**真实输出**分类；成功任务不受影响
                verdict = remote_pressure.classify_failure(
                    f"{stdout}\n{stderr}"
                ) if process.returncode != 0 else remote_pressure.BackpressureVerdict(
                    remote_pressure.NONE, remote_pressure.REASON_OTHER, ""
                )

                result = TaskResult(
                    source=pdf,
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    started_at=started,
                    finished_at=clock.monotonic(),
                    failure_kind=verdict.kind,
                    failure_reason=verdict.reason,
                    skipped=(
                        process.returncode == 0 and _SKIP_MARKER in f"{stdout}\n{stderr}"
                    ),
                )
                results[pdf] = result

                if result.ok:
                    backoff.note_success()
                elif remote_pressure.should_cool_down(verdict):
                    cooldown = backoff.note_backpressure()
                    stats.record(verdict, cooldown)
                    stats.peak_backoff_level = max(stats.peak_backoff_level, backoff.level)
                    if on_backpressure is not None:
                        on_backpressure(result, verdict, cooldown, len(active))
                elif verdict.kind == remote_pressure.QUOTA_EXHAUSTED:
                    stats.record(verdict, 0.0)
                # 普通失败：记录，但不影响 backoff

                if on_finish is not None:
                    on_finish(result, len(results), len(pdfs))
            _fill()
    except KeyboardInterrupt:
        # 1) 不再启动新任务（清空队列）2) 终止所有在跑的子进程 3) 回收
        pending.clear()
        for process in list(active):
            terminate(process)
            active.pop(process)
        raise

    return [results[pdf] for pdf in sorted(results, key=lambda p: order[p])]


def subprocess_wait_any(
    active: dict[subprocess.Popen[str], object],
    *,
    clock=time,
) -> list[subprocess.Popen[str]]:
    """等待任意数量的子进程结束，返回本次已结束的列表。

    标准库没有跨平台的 wait-any，这里用短超时轮询实现；超时很短且只做
    `poll()`，不消耗 CPU 也不影响子进程。
    """
    while True:
        done = [process for process in active if process.poll() is not None]
        if done:
            return done
        clock.sleep(0.05)


# ---------------------------------------------------------------- 输出


def format_status(index: int, total: int, result: TaskResult) -> str:
    if not result.ok:
        status = "FAILED"
    elif result.skipped:
        status = "SKIP"
    else:
        status = "OK"
    return f"[{index}/{total}] {status:<6}  {result.source.name}"


def print_failure(result: TaskResult) -> None:
    print(f"\n[{result.source.name}] 退出码 {result.returncode}")
    for label, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        lines = [line for line in (text or "").splitlines() if line.strip()]
        if not lines:
            continue
        print(f"  --- {label} ---")
        for line in lines:
            print(f"  {line}")


_REASON_LABELS = {
    remote_pressure.REASON_QUEUE_FULL: "Paddle queue full",
    remote_pressure.REASON_RATE_LIMIT: "Paddle rate limit",
    remote_pressure.REASON_HTTP_429: "HTTP 429",
    remote_pressure.REASON_HTTP_503: "HTTP 503/504",
    remote_pressure.REASON_QUOTA: "quota exhausted",
}


def print_backpressure(
    result: TaskResult,
    verdict,
    cooldown: float,
    running: int,
) -> None:
    """遇到服务端压力时输出一次清晰说明（一个冷却周期只打这一段）。"""
    label = _REASON_LABELS.get(verdict.reason, verdict.reason)
    print(
        f"\n[{result.source.name}] Paddle 服务端繁忙：检测到 {label}"
        f"（{verdict.evidence}）"
    )
    print(f"暂停提交新任务 {cooldown:.0f} 秒；当前运行中的 {running} 个任务继续执行")
    print("（这是远端限流，不是代码错误；已失败的 PDF 不会被自动重跑）")


def print_summary(
    results: list[TaskResult],
    workers: int,
    elapsed: float,
    stats: ScheduleStats | None = None,
) -> None:
    stats = stats if stats is not None else ScheduleStats()
    succeeded = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    skipped = [r for r in results if r.ok and r.skipped]
    backpressure_failed = [
        r for r in failed if r.failure_kind == remote_pressure.REMOTE_BACKPRESSURE
    ]
    quota_failed = [
        r for r in failed if r.failure_kind == remote_pressure.QUOTA_EXHAUSTED
    ]

    print("\n" + "=" * 60)
    print("处理完成")
    print(f"Total:                {len(results)}")
    print(f"Succeeded:            {len(succeeded)}")
    print(f"Failed:               {len(failed)}")
    print(f"Skipped:              {len(skipped)}")
    print(f"Workers:              {workers}")
    print(f"Elapsed:              {elapsed:.1f}s")
    print(f"Backpressure events:  {stats.backpressure_events}")
    print(f"Quota events:         {stats.quota_events}")
    print(f"Peak cooldown:        {stats.peak_cooldown:.0f}s")
    print(f"Peak backoff level:   {stats.peak_backoff_level}")

    for reason, label in _REASON_LABELS.items():
        if reason == remote_pressure.REASON_QUOTA:
            continue
        count = stats.reasons.get(reason, 0)
        if count:
            print(f"  {label}: {count}")

    if quota_failed:
        print(
            f"注意：{len(quota_failed)} 个任务因**当日额度耗尽**失败，"
            "这不是临时限流，重试无用，请等待次日额度恢复。"
        )
    if backpressure_failed and not quota_failed:
        print(
            f"说明：{len(backpressure_failed)} 个失败来自远端限流，"
            "可稍后用相同命令重跑这些文件（调度器本身不会自动重跑）。"
        )

    if failed:
        print("Failed:")
        for result in failed:
            label = result.failure_kind if result.failure_kind != remote_pressure.NONE else "FAILED"
            reason = (
                f"  {result.failure_reason}"
                if result.failure_kind == remote_pressure.REMOTE_BACKPRESSURE
                and result.failure_reason != remote_pressure.REASON_OTHER
                else ""
            )
            print(f"- {result.source.name}  [{label}]{reason}")
    print("=" * 60)


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pipeline_parallel",
        description=(
            "多 PDF 受限并发：每个 PDF 由独立的 run_pipeline.py 子进程完整处理，"
            "不同 PDF 之间并行（默认 2 个）。只接受 .pdf 输入。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  run_pipeline_parallel.py "D:\\试卷\\1.pdf" "D:\\试卷\\2.pdf"\n'
            '  run_pipeline_parallel.py "D:\\试卷" --workers 3\n'
        ),
    )
    parser.add_argument("inputs", nargs="+", help="一个或多个 .pdf 文件，或包含 .pdf 的文件夹")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"最大并发 worker 数，正整数（默认 {DEFAULT_WORKERS}）",
    )
    parser.add_argument(
        "--launch-interval",
        type=float,
        default=DEFAULT_LAUNCH_INTERVAL,
        help=(
            "连续启动两个 worker 之间的最小间隔秒数"
            f"（默认 {DEFAULT_LAUNCH_INTERVAL}；0 表示不限制）"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="worker 打印全部诊断")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的产物")
    # 与 pdf2md.py / run_pipeline.py 保持同一套语义：默认导出，两个开关互斥。
    images = parser.add_mutually_exclusive_group()
    images.add_argument(
        "--keep-images",
        dest="keep_images",
        action="store_true",
        default=True,
        help="OCR 时导出文档内图片（默认开启，保留此参数只为兼容旧命令）",
    )
    images.add_argument(
        "--no-images",
        dest="keep_images",
        action="store_false",
        help="OCR 时不导出图片，只写 Markdown 文本",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()

    # 参数错误统一走 argparse 的退出码 2
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.workers < 1:
        print(f"错误：--workers 必须是正整数，收到 {args.workers}", file=sys.stderr)
        return EXIT_FATAL
    if args.launch_interval < 0:
        print(
            f"错误：--launch-interval 不能为负数，收到 {args.launch_interval}",
            file=sys.stderr,
        )
        return EXIT_FATAL

    # 调度器级致命检查：入口必须存在
    if not RUN_PIPELINE.is_file():
        print(f"错误：找不到 worker 入口 {RUN_PIPELINE}", file=sys.stderr)
        return EXIT_FATAL
    python = find_tool_python()
    if not python.exists():
        print(f"错误：找不到 Python 解释器 {python}", file=sys.stderr)
        return EXIT_FATAL

    pdfs, problems = collect_pdfs(args.inputs)
    for problem in problems:
        print(f"[输入] {problem}", file=sys.stderr)
    if not pdfs:
        print("没有可处理的 PDF。", file=sys.stderr)
        return EXIT_FATAL

    conflicts = detect_output_conflicts(pdfs)
    if conflicts:
        for conflict in conflicts:
            print(f"错误：{conflict}", file=sys.stderr)
        return EXIT_FATAL

    workers = min(args.workers, len(pdfs))
    print(f"worker 解释器：{python}")
    print(f"待处理 PDF：{len(pdfs)} 个；最大并发：{workers}")
    if args.workers > WORKERS_WARN_THRESHOLD:
        print(
            f"警告：请求的 --workers={args.workers} 偏大（实际并发受 PDF 数限制为 {workers}）。\n"
            "Paddle 免费共享服务可能出现队列或频率限制。\n"
            "建议从 2 开始测试；若频繁出现服务端繁忙，调度器会自动降低提交速度。"
        )
    print("=" * 60)

    started = time.monotonic()
    stats = ScheduleStats()

    def on_start(pdf: pathlib.Path) -> None:
        print(f"START   {pdf.name}")

    def on_finish(result: TaskResult, done: int, total: int) -> None:
        print(format_status(done, total, result))

    def on_backpressure(result: TaskResult, verdict, cooldown: float, running: int) -> None:
        print_backpressure(result, verdict, cooldown, running)

    try:
        results = run_bounded(
            pdfs,
            workers=workers,
            python=python,
            verbose=args.verbose,
            overwrite=args.overwrite,
            keep_images=args.keep_images,
            launch_interval=args.launch_interval,
            stats=stats,
            on_start=on_start,
            on_finish=on_finish,
            on_backpressure=on_backpressure,
        )
    except KeyboardInterrupt:
        print("\n已中断：不再启动新任务，正在终止运行中的 worker……", file=sys.stderr)
        return EXIT_INTERRUPTED

    elapsed = time.monotonic() - started

    for result in results:
        if not result.ok:
            print_failure(result)
    print_summary(results, workers, elapsed, stats)

    return EXIT_OK if all(r.ok for r in results) else EXIT_TASK_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
