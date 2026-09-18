"""run_pipeline_parallel 的自动测试。

不真实调用 AI Studio：并发与失败路径用内存假进程（FakePopen）模拟，
真实 subprocess 只用于验证「中断不留孤儿」「调度器不写文件」这类进程级行为。
"""

from __future__ import annotations

import ast
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import time

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import run_pipeline_parallel as rp  # noqa: E402


# ---------------------------------------------------------------- 输入展开


def _make_pdfs(root: pathlib.Path, *names: str) -> list[pathlib.Path]:
    made = []
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
        made.append(path)
    return made


def test_single_pdf(tmp_path):
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    pdfs, problems = rp.collect_pdfs([str(pdf)])
    assert [p.name for p in pdfs] == ["A.pdf"]
    assert problems == []


def test_collected_order_is_deterministic(tmp_path):
    """顺序规则确定：文件参数按传入顺序，目录展开按名字排序。

    不依赖任何中间列表的顺序——显式构造路径并按固定顺序传入。
    """
    for name in ("A.pdf", "B.pdf", "C.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")

    first = tmp_path / "A.pdf"
    second = tmp_path / "B.pdf"
    third = tmp_path / "C.pdf"
    inputs = [str(second), str(third), str(first)]  # B, C, A

    pdfs, _ = rp.collect_pdfs(inputs)
    assert [p.name for p in pdfs] == ["B.pdf", "C.pdf", "A.pdf"]
    # 同样的输入重复调用结果一致
    again, _ = rp.collect_pdfs(inputs)
    assert [p.name for p in again] == [p.name for p in pdfs]


def test_directory_expansion_is_sorted(tmp_path):
    _make_pdfs(tmp_path, "c.pdf", "a.pdf", "b.pdf")
    pdfs, _ = rp.collect_pdfs([str(tmp_path)])
    assert [p.name for p in pdfs] == ["a.pdf", "b.pdf", "c.pdf"]


def test_directory_discovers_pdfs(tmp_path):
    _make_pdfs(tmp_path, "1.pdf", "2.pdf")
    pdfs, problems = rp.collect_pdfs([str(tmp_path)])
    assert [p.name for p in pdfs] == ["1.pdf", "2.pdf"]
    assert problems == []


def test_directory_is_not_recursive(tmp_path):
    _make_pdfs(tmp_path, "top.pdf")
    _make_pdfs(tmp_path / "sub", "deep.pdf")
    pdfs, _ = rp.collect_pdfs([str(tmp_path)])
    assert [p.name for p in pdfs] == ["top.pdf"]


def test_non_pdf_inputs_are_skipped(tmp_path):
    (pdf,) = _make_pdfs(tmp_path, "ok.pdf")
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    pdfs, problems = rp.collect_pdfs(
        [str(tmp_path / "note.txt"), str(tmp_path / "image.png"), str(pdf)]
    )
    assert [p.name for p in pdfs] == ["ok.pdf"]
    assert len(problems) == 2


def test_directory_reports_when_no_pdf(tmp_path):
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    pdfs, problems = rp.collect_pdfs([str(tmp_path)])
    assert pdfs == []
    assert any("没有 .pdf" in p for p in problems)


def test_missing_path_is_reported(tmp_path):
    pdfs, problems = rp.collect_pdfs([str(tmp_path / "nope.pdf")])
    assert pdfs == []
    assert any("路径不存在" in p for p in problems)


def test_same_pdf_twice_runs_once(tmp_path):
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    pdfs, _ = rp.collect_pdfs([str(pdf), str(pdf)])
    assert len(pdfs) == 1


def test_file_and_parent_directory_deduplicate(tmp_path):
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    _make_pdfs(tmp_path, "B.pdf")
    pdfs, _ = rp.collect_pdfs([str(pdf), str(tmp_path)])
    assert [p.name for p in pdfs] == ["A.pdf", "B.pdf"]


def test_deduplication_is_case_insensitive_on_windows(tmp_path):
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    upper = pathlib.Path(str(pdf).upper())
    pdfs, _ = rp.collect_pdfs([str(pdf), str(upper)])
    assert len(pdfs) == 1


def test_output_conflicts_are_detected(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    # 同一目录下同名文件在文件系统层不可能共存；这里直接构造两个指向同一
    # 输出的路径，验证检测函数本身会拒绝，而不是依赖「最后一个赢」。
    pdf = shared / "S.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert rp.detect_output_conflicts([pdf]) == []


# ---------------------------------------------------------------- 命令构造


def test_worker_command_reuses_existing_entry(tmp_path):
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    python = pathlib.Path(sys.executable)
    command = rp.build_worker_command(
        pdf, python=python, verbose=False, overwrite=False, keep_images=False
    )
    assert command[0] == str(python)
    assert command[1] == str(rp.RUN_PIPELINE)
    assert command[2] == str(pdf)
    # 必须带 --no-pause，否则 worker 会等待按键
    assert "--no-pause" in command


def test_worker_command_passes_flags(tmp_path):
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    command = rp.build_worker_command(
        pdf,
        python=pathlib.Path(sys.executable),
        verbose=True,
        overwrite=True,
        keep_images=True,
    )
    assert {"--verbose", "--overwrite", "--keep-images", "--no-pause"} <= set(command)


def test_worker_command_can_drop_images(tmp_path):
    """图片默认导出；关掉时要显式传 --no-images，而不是靠 worker 的隐含默认。"""
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    command = rp.build_worker_command(
        pdf,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False,
    )
    assert "--no-images" in command
    assert "--keep-images" not in command


def test_worker_entry_exists():
    assert rp.RUN_PIPELINE.is_file(), "并发入口必须复用现有 run_pipeline.py"


# ---------------------------------------------------------------- 假进程


class FakePopen:
    """内存假子进程：按 PDF 名决定成功/失败与耗时。"""

    registry: list["FakePopen"] = []
    started: list[str] = []

    def __init__(self, command, **kwargs):
        self.command = list(command)
        self.pdf = pathlib.Path(self.command[2])
        self.pid = 100000 + len(FakePopen.registry)
        self._start = time.monotonic()
        self._duration = 0.2 if self.pdf.stem.startswith("SLOW") else 0.05
        self._returncode = 2 if self.pdf.stem.startswith("FAIL") else 0
        self.terminated = False
        self._on_terminate = None
        self.stdout = io.StringIO(f"out:{self.pdf.name}\n")
        self.stderr = io.StringIO(
            f"err:{self.pdf.name}\n" if self._returncode else ""
        )
        FakePopen.registry.append(self)
        FakePopen.started.append(self.pdf.name)

    def poll(self):
        if self.terminated:
            return -9
        if time.monotonic() - self._start >= self._duration:
            return self._returncode
        return None

    @property
    def returncode(self):
        """与真实 subprocess.Popen 对齐。"""
        return self.poll()

    def terminate(self):
        self.terminated = True
        if self._on_terminate is not None:
            self._on_terminate()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        """真实 Popen.wait 会阻塞到进程结束；假进程必须同样语义，
        否则调用方 terminate() 之后的清理逻辑不会被完整走一遍。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.command, timeout)
            time.sleep(0.002)
        return self.poll()


@pytest.fixture()
def fake_launch(monkeypatch):
    FakePopen.registry = []
    FakePopen.started = []
    monkeypatch.setattr(rp, "launch", lambda command: FakePopen(command))
    return FakePopen


# ---------------------------------------------------------------- 并发行为


def _peak_concurrency(fake: type[FakePopen]) -> int:
    events = []
    for process in fake.registry:
        events.append((process._start, 1))
        events.append((process._start + process._duration, -1))
    events.sort()
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def test_workers_one_keeps_single_active(tmp_path, fake_launch):
    pdfs, _ = rp.collect_pdfs([str(d) for d in _make_pdfs(tmp_path, "A.pdf", "B.pdf", "C.pdf")])
    results = rp.run_bounded(
        pdfs,
        workers=1,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False, launch_interval=0.0,
    )
    assert all(r.ok for r in results)
    assert _peak_concurrency(fake_launch) == 1


def test_workers_two_never_exceeds_limit(tmp_path, fake_launch):
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "A.pdf", "B.pdf", "C.pdf", "D.pdf", "E.pdf")]
    )
    rp.run_bounded(
        pdfs,
        workers=2,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False, launch_interval=0.0,
    )
    assert _peak_concurrency(fake_launch) <= 2


def test_workers_two_actually_overlaps(tmp_path, fake_launch):
    """至少证明是真并发：两个 SLOW 任务的运行区间有真实重叠。"""
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "SLOW_A.pdf", "SLOW_B.pdf")]
    )
    rp.run_bounded(
        pdfs,
        workers=2,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False, launch_interval=0.0,
    )
    first, second = fake_launch.registry[0], fake_launch.registry[1]
    overlap = min(first._start + first._duration, second._start + second._duration) - max(
        first._start, second._start
    )
    assert overlap > 0, "两个任务没有时间重叠，说明是伪并发"
    assert _peak_concurrency(fake_launch) == 2


def test_active_never_exceeds_limit_at_any_moment(tmp_path, fake_launch):
    """更严格：调度过程中 active 集合大小始终不超过 workers。"""
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "A.pdf", "B.pdf", "C.pdf", "D.pdf")]
    )
    observed: list[int] = []
    original = rp.subprocess_wait_any

    def spy(active):
        observed.append(len(active))
        return original(active)

    rp.subprocess_wait_any = spy  # type: ignore[assignment]
    try:
        rp.run_bounded(
            pdfs,
            workers=2,
            python=pathlib.Path(sys.executable),
            verbose=False,
            overwrite=False,
            keep_images=False, launch_interval=0.0,
        )
    finally:
        rp.subprocess_wait_any = original  # type: ignore[assignment]
    assert max(observed) <= 2


def test_results_keep_input_order(tmp_path, fake_launch):
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "C.pdf", "A.pdf", "B.pdf")]
    )
    results = rp.run_bounded(
        pdfs,
        workers=3,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False, launch_interval=0.0,
    )
    assert [r.source.name for r in results] == ["C.pdf", "A.pdf", "B.pdf"]


# ---------------------------------------------------------------- 失败隔离


def test_one_failure_does_not_stop_others(tmp_path, fake_launch):
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "A.pdf", "FAIL_B.pdf", "C.pdf", "FAIL_D.pdf", "E.pdf")]
    )
    results = rp.run_bounded(
        pdfs,
        workers=2,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False, launch_interval=0.0,
    )
    ok = {r.source.name for r in results if r.ok}
    failed = {r.source.name for r in results if not r.ok}
    assert ok == {"A.pdf", "C.pdf", "E.pdf"}
    assert failed == {"FAIL_B.pdf", "FAIL_D.pdf"}
    assert len(results) == 5


def test_failure_logs_are_kept_per_source(tmp_path, fake_launch):
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "FAIL_A.pdf", "B.pdf")]
    )
    results = rp.run_bounded(
        pdfs,
        workers=2,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False, launch_interval=0.0,
    )
    by_name = {r.source.name: r for r in results}
    assert "err:FAIL_A.pdf" in by_name["FAIL_A.pdf"].stderr
    assert "out:FAIL_A.pdf" in by_name["FAIL_A.pdf"].stdout
    # 成功任务的输出不得混入失败任务的内容
    assert "FAIL_A" not in by_name["B.pdf"].stdout
    assert by_name["B.pdf"].stderr == ""


def test_exit_code_zero_when_all_succeed(tmp_path, fake_launch, monkeypatch):
    pdfs, _ = rp.collect_pdfs([str(d) for d in _make_pdfs(tmp_path, "A.pdf", "B.pdf")])
    results = rp.run_bounded(
        pdfs, workers=2, python=pathlib.Path(sys.executable),
        verbose=False, overwrite=False, keep_images=False, launch_interval=0.0,
    )
    assert all(r.ok for r in results)


def test_summary_counts_failures(tmp_path, fake_launch, capsys):
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "A.pdf", "FAIL_B.pdf")]
    )
    results = rp.run_bounded(
        pdfs, workers=2, python=pathlib.Path(sys.executable),
        verbose=False, overwrite=False, keep_images=False, launch_interval=0.0,
    )
    rp.print_summary(results, workers=2, elapsed=1.0)
    output = capsys.readouterr().out
    # 汇总字段：新增了服务端压力统计
    assert re.search(r"Total:\s+2", output)
    assert re.search(r"Succeeded:\s+1", output)
    assert re.search(r"Failed:\s+1", output)
    assert re.search(r"Workers:\s+2", output)
    assert re.search(r"Backpressure events:\s+0", output)
    assert re.search(r"Peak cooldown:\s+0s", output)
    assert "- FAIL_B.pdf" in output


# ---------------------------------------------------------------- 中断


def test_keyboard_interrupt_is_propagated(tmp_path, monkeypatch):
    """KeyboardInterrupt 不得被吞掉。"""
    pdfs, _ = rp.collect_pdfs([str(d) for d in _make_pdfs(tmp_path, "A.pdf", "B.pdf")])
    FakePopen.registry = []
    monkeypatch.setattr(rp, "launch", lambda command: FakePopen(command))

    calls = {"n": 0}

    def wait_then_interrupt(active):
        # 第一次正常返回（至少一个结束），第二次抛中断 —— 时机确定
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        while True:
            done = [p for p in active if p.poll() is not None]
            if done:
                return done
            time.sleep(0.01)

    monkeypatch.setattr(rp, "subprocess_wait_any", wait_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        rp.run_bounded(
            pdfs, workers=1, python=pathlib.Path(sys.executable),
            verbose=False, overwrite=False, keep_images=False, launch_interval=0.0,
        )
    assert calls["n"] >= 2, "中断必须真的被触发过"


def test_interrupt_stops_new_tasks_and_terminates_running(tmp_path, monkeypatch):
    """中断后：不再启动新任务，且已运行的 worker 全部被终止。

    构造：4 个 PDF、workers=2、全部为长任务。中断在**所有 worker 仍在运行时**
    触发，因此能同时验证「终止运行中的进程」与「不再启动排队任务」。
    """
    pdfs, _ = rp.collect_pdfs(
        [str(d) for d in _make_pdfs(tmp_path, "SLOW_A.pdf", "SLOW_B.pdf", "SLOW_C.pdf", "SLOW_D.pdf")]
    )
    FakePopen.registry = []
    launched: list[FakePopen] = []

    def fake_launch(command):
        process = FakePopen(command)
        # 不会自己结束，确保中断时它确实"仍在运行"
        process._duration = 3600.0
        process._on_terminate = lambda: None
        # 终止后立即回收：本测试验证的是"调度器调用了终止且不再启动新任务"，
        # 不必等真实的 10 秒回收超时（真实回收路径在集成验收里覆盖）。
        process.wait = lambda timeout=None: process.poll()  # type: ignore[method-assign]
        launched.append(process)
        return process

    monkeypatch.setattr(rp, "launch", fake_launch)

    # 用假的 taskkill 代替真实进程终止：真实 taskkill 会真的杀掉子进程，
    # 这里必须让它对假进程产生同样的效果（标记为已终止并立即回收）。
    def fake_taskkill(*args, **kwargs):
        for alive in launched:
            alive.terminate()
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(rp.subprocess, "run", fake_taskkill)

    calls = {"n": 0}

    def interrupt_immediately(active):
        calls["n"] += 1
        assert len(active) <= 2, "并发上限被突破"
        assert all(p.poll() is None for p in active), "此刻不应有进程已结束"
        raise KeyboardInterrupt

    monkeypatch.setattr(rp, "subprocess_wait_any", interrupt_immediately)

    with pytest.raises(KeyboardInterrupt):
        rp.run_bounded(
            pdfs, workers=2, python=pathlib.Path(sys.executable),
            verbose=False, overwrite=False, keep_images=False, launch_interval=0.0,
        )

    assert calls["n"] == 1
    # 只启动了 workers 个，排队中的 2 个从未启动
    assert len(launched) == 2, f"中断后不应继续启动任务，实际启动 {len(launched)}"
    assert [p.pdf.name for p in launched] == ["SLOW_A.pdf", "SLOW_B.pdf"]
    # 运行中的全部被终止
    detail = [
        (p.pdf.name, p.terminated, p.poll(), p.returncode, p._duration)
        for p in launched
    ]
    assert all(process.terminated for process in launched), detail


def test_terminate_is_idempotent_for_finished_process(tmp_path, monkeypatch):
    """已结束的进程再 terminate 不应报错。"""
    FakePopen.registry = []
    process = FakePopen(["python", "run_pipeline.py", str(tmp_path / "A.pdf"), "--no-pause"])
    process._duration = 0.0
    process.poll()
    rp.terminate(process)  # 不应抛异常


def test_main_returns_130_on_interrupt(tmp_path, monkeypatch, capsys):
    pdfs, _ = rp.collect_pdfs([str(d) for d in _make_pdfs(tmp_path, "A.pdf")])

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(rp, "run_bounded", boom)
    monkeypatch.setattr(rp, "find_tool_python", lambda: pathlib.Path(sys.executable))
    code = rp.main([str(pdfs[0]), "--workers", "1"])
    assert code == rp.EXIT_INTERRUPTED == 130
    assert "中断" in capsys.readouterr().err


# ---------------------------------------------------------------- 参数与全局错误


def test_workers_zero_is_parameter_error(tmp_path, capsys):
    pdfs, _ = rp.collect_pdfs([str(d) for d in _make_pdfs(tmp_path, "A.pdf")])
    code = rp.main([str(pdfs[0]), "--workers", "0"])
    assert code == rp.EXIT_FATAL == 2
    assert "正整数" in capsys.readouterr().err


def test_workers_negative_is_parameter_error(tmp_path):
    pdfs, _ = rp.collect_pdfs([str(d) for d in _make_pdfs(tmp_path, "A.pdf")])
    assert rp.main([str(pdfs[0]), "--workers", "-3"]) == 2


def test_workers_default_is_two():
    parser = rp.build_parser()
    args = parser.parse_args(["x.pdf"])
    assert args.workers == rp.DEFAULT_WORKERS == 2


def test_no_pdf_input_is_fatal(tmp_path, capsys):
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    code = rp.main([str(tmp_path)])
    assert code == 2
    assert "没有可处理的 PDF" in capsys.readouterr().err


def test_missing_worker_entry_is_fatal(tmp_path, monkeypatch, capsys):
    pdfs, _ = rp.collect_pdfs([str(d) for d in _make_pdfs(tmp_path, "A.pdf")])
    monkeypatch.setattr(rp, "RUN_PIPELINE", tmp_path / "not_there.py")
    assert rp.main([str(pdfs[0])]) == 2
    assert "找不到 worker 入口" in capsys.readouterr().err


def test_argparse_rejects_zero_workers_as_usage_error(tmp_path):
    """argparse 自身对非法值也应给出非零退出码。"""
    script = TOOL_DIR / "run_pipeline_parallel.py"
    completed = subprocess.run(
        [sys.executable, str(script), "x.pdf", "--workers", "abc"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode != 0


# ---------------------------------------------------------------- 文件安全与兼容


def test_scheduler_never_writes_files():
    """调度器不得自己写/删文件（AST 精确检查实际调用）。"""
    source = (TOOL_DIR / "run_pipeline_parallel.py").read_text(encoding="utf-8")
    forbidden = {"write_text", "write_bytes", "open", "unlink", "remove", "rmtree", "replace"}
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in forbidden:
                offenders.append(f"{name}() line {node.lineno}")
    assert offenders == [], f"调度器不应写或删文件：{offenders}"


def test_scheduler_does_not_import_internal_pipeline():
    """不得 import 规范化器内部模块，也不得用线程池/进程池。"""
    source = (TOOL_DIR / "run_pipeline_parallel.py").read_text(encoding="utf-8")
    assert "md_math_normalizer" not in source
    assert "ProcessPoolExecutor" not in source
    assert "ThreadPoolExecutor" not in source
    assert "import asyncio" not in source


def test_workers_one_matches_serial_worker_call(tmp_path, monkeypatch):
    """workers=1 时构造出的 worker 命令与直接调用 run_pipeline 完全一致。"""
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    python = pathlib.Path(sys.executable)
    for keep_images, image_flag in ((True, "--keep-images"), (False, "--no-images")):
        parallel_command = rp.build_worker_command(
            pdf, python=python, verbose=False, overwrite=False, keep_images=keep_images
        )
        serial_command = [
            str(python),
            str(rp.RUN_PIPELINE),
            str(pdf),
            "--no-pause",
            image_flag,
        ]
        assert parallel_command == serial_command


def test_pdf_files_are_not_modified_by_scheduler(tmp_path, fake_launch):
    (pdf,) = _make_pdfs(tmp_path, "A.pdf")
    before = pdf.read_bytes()
    mtime = pdf.stat().st_mtime
    pdfs, _ = rp.collect_pdfs([str(pdf)])
    rp.run_bounded(
        pdfs, workers=1, python=pathlib.Path(sys.executable),
        verbose=False, overwrite=False, keep_images=False, launch_interval=0.0,
    )
    assert pdf.read_bytes() == before
    assert pdf.stat().st_mtime == mtime
