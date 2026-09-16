"""调度器对远端服务压力的自适应节流：cooldown、恢复、启动间隔、统计。

全部使用**虚拟时钟**（不真 sleep）与假子进程，不访问 Paddle 服务。

关键区分：
  * 临时压力（429 / 503 / 队列满）→ 触发 cooldown，只延迟**新任务提交**
  * 额度耗尽（今日已达上限）      → 不触发 cooldown（重试无用）
  * 普通失败（OCR/规范化/参数）    → 不影响 backoff
"""

from __future__ import annotations

import io
import pathlib
import re
import sys
import time

import pytest

TOOL_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import remote_pressure as rpv  # noqa: E402
import run_pipeline_parallel as rp  # noqa: E402

# 实测到的真实错误文本
TEXT_QUEUE_FULL = "  [失败] InvalidRequestError: Bad request: 任务提交队列已满，请稍后重试"
TEXT_HTTP_429 = "Execution failed: HTTP 429: rate limit exceeded"
TEXT_HTTP_503 = "Service unavailable: HTTP 503: service busy"
TEXT_CODE_10010 = "HTTP 400: code 10010 任务提交队列已满"
TEXT_QUOTA = "Bad request: 今日提交任务已达上限，请明日再试"
TEXT_NORMAL_FAIL = "  [失败] 未识别到内容：扫描件.pdf"


# ---------------------------------------------------------------- 虚拟时钟


class FakeClock:
    """虚拟单调时钟：sleep 只推进时间，不真正等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


class FakeProcess:
    """按 PDF 名决定成败与失败文本的假子进程（耗时用虚拟时间）。"""

    registry: list["FakeProcess"] = []

    def __init__(self, command, *, clock=None, duration=2.0, returncode=0, stderr=""):
        self.command = list(command)
        self.pdf = pathlib.Path(self.command[2])
        self.pid = 200000 + len(FakeProcess.registry)
        self._clock = clock
        self._duration = duration
        self._returncode = returncode
        self._stderr = stderr
        self.terminated = False
        self._on_terminate = None
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO(stderr)
        FakeProcess.registry.append(self)

    # --- 供调度器使用
    def poll(self):
        if self.terminated:
            return -9
        # 虚拟时间没走完就还没结束（与真实 Popen.poll 语义一致）
        if self._clock is not None and self._clock.now < self._start + self._duration:
            return None
        return self._returncode

    @property
    def returncode(self):
        return self.poll()

    def wait(self, timeout=None):
        return self.poll()

    def terminate(self):
        self.terminated = True
        if self._on_terminate is not None:
            self._on_terminate()

    def kill(self):
        self.terminate()


def make_spec(
    *,
    duration=2.0,
    returncode=0,
    backpressure: str | None = None,
    fail_text: str | None = None,
):
    """构造一个 worker 规格：正常 / 压力失败 / 普通失败。"""
    if backpressure is not None:
        return {"duration": duration, "returncode": 2, "stderr": backpressure}
    if fail_text is not None:
        return {"duration": duration, "returncode": 2, "stderr": fail_text}
    return {"duration": duration, "returncode": returncode, "stderr": ""}


def run_schedule(
    tmp_path,
    specs: list[dict],
    *,
    workers: int = 2,
    launch_interval: float = 0.0,
    clock: FakeClock | None = None,
    collect_stats: bool = True,
):
    """用假进程 + 虚拟时钟跑一遍调度器，返回 (results, stats, clock, launches, events)。"""
    pdfs = []
    for index in range(len(specs)):
        pdf = tmp_path / f"doc{index:02d}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        pdfs.append(pdf)

    plan = dict(enumerate(specs))
    clock = clock or FakeClock()
    launches: list[tuple[str, float]] = []
    events: list[tuple[str, str, float]] = []
    stats = rp.ScheduleStats()

    def fake_launch(command):
        index = int(pathlib.Path(command[2]).stem.replace("doc", ""))
        spec = plan[index]
        process = FakeProcess(
            command,
            clock=clock,
            duration=spec["duration"],
            returncode=spec["returncode"],
            stderr=spec.get("stderr", ""),
        )
        process._start = clock.now
        process._done_at = clock.now + spec["duration"]
        launches.append((process.pdf.name, clock.now))
        return process

    def fake_wait(active):
        """推进虚拟时间到最早完成的进程。"""
        pending = [p for p in active if p.poll() is None]
        if not pending:
            return list(active)
        earliest = min(p._done_at for p in pending)
        clock.now = max(clock.now, earliest)
        return [p for p in active if p.poll() is not None] or list(pending)

    def on_backpressure(result, verdict, cooldown, running):
        events.append((result.source.name, verdict.reason, cooldown))

    results = rp.run_bounded(
        pdfs,
        workers=workers,
        python=pathlib.Path(sys.executable),
        verbose=False,
        overwrite=False,
        keep_images=False,
        launch_interval=launch_interval,
        clock=clock,
        wait_any=fake_wait,
        launch_worker=fake_launch,
        stats=stats,
        on_backpressure=on_backpressure,
    )
    # 用规格顺序返回，便于断言
    by_name = {r.source.name: r for r in results}
    with open(TOOL_DIR / "_cooldown_diag.txt", "a", encoding="utf-8") as handle:
        handle.write(f"launches={launches}\n")
        handle.write(f"results={[(r.source.name, r.returncode) for r in results]}\n")
        handle.write(f"sleeps={clock.sleeps[:10]} events={events}\n")
    ordered = [by_name[pdf.name] for pdf in pdfs]
    return ordered, stats, clock, launches, events


# ---------------------------------------------------------------- 纯函数：退避曲线


def test_cooldown_curve_matches_spec():
    assert [rp.cooldown_for_level(n) for n in range(0, 7)] == [0.0, 5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


def test_cooldown_never_exceeds_cap():
    assert max(rp.cooldown_for_level(n) for n in range(0, 50)) == 60.0


def test_backoff_level_rises_and_is_capped():
    backoff = rp.Backoff(FakeClock())
    cooldowns = [backoff.note_backpressure() for _ in range(8)]
    assert cooldowns[:6] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    assert backoff.level == rp.MAX_BACKOFF_LEVEL


def test_success_lowers_backoff_level():
    backoff = rp.Backoff(FakeClock())
    for _ in range(3):
        backoff.note_backpressure()
    assert backoff.level == 3
    backoff.note_success()
    assert backoff.level == 2
    assert rp.cooldown_for_level(backoff.level) == 10.0


def test_backoff_level_never_below_zero():
    backoff = rp.Backoff(FakeClock())
    for _ in range(5):
        backoff.note_success()
    assert backoff.level == 0
    assert backoff.cooldown_remaining() == 0.0


def test_wait_before_launch_enforces_launch_interval():
    clock = FakeClock()
    backoff = rp.Backoff(clock)
    assert backoff.wait_before_launch(0.5) == 0.0     # 首次不等待
    waited = backoff.wait_before_launch(0.5)          # 紧接着第二次必须等
    assert waited == pytest.approx(0.5)
    assert clock.total_slept == pytest.approx(0.5)


def test_wait_before_launch_honours_cooldown():
    clock = FakeClock()
    backoff = rp.Backoff(clock)
    backoff.note_backpressure()                       # 5s 冷却
    waited = backoff.wait_before_launch(0.0)
    assert waited == pytest.approx(5.0)
    assert not backoff.in_cooldown()


# ---------------------------------------------------------------- 调度行为


def test_backpressure_triggers_cooldown_and_delays_next_launch(tmp_path):
    """cooldown 期间不启动新任务，冷却结束后继续启动。"""
    specs = [
        make_spec(duration=1.0, backpressure=TEXT_HTTP_429),   # 触发冷却
        make_spec(duration=10.0),                              # 仍在跑
        make_spec(duration=1.0),                               # 排队
        make_spec(duration=1.0),                               # 排队
    ]
    results, stats, clock, launches, events = run_schedule(tmp_path, specs, workers=2)

    assert len(results) == 4
    # 第一次 backpressure → 5s 冷却
    assert stats.backpressure_events == 1
    assert stats.peak_cooldown == 5.0
    assert events and events[0][2] == 5.0

    # 冷却生效：doc02/doc03 不得在前两个任务失败的那一刻立即启动
    first_completion = 1.0
    later = [t for name, t in launches if name in ("doc02.pdf", "doc03.pdf")]
    assert later, "冷却结束后应继续启动排队的任务"
    assert min(later) >= first_completion + 5.0 - 1e-9, (
        f"冷却期内启动了新任务：{launches}"
    )
    # 冷却确实以时间形式被消耗过（要么 sleep，要么被后续运行时间覆盖）
    assert stats.peak_cooldown == 5.0


def test_running_worker_is_not_touched_by_cooldown(tmp_path):
    """已有 worker 不受影响：一个失败触发冷却时，另一个正常跑完。"""
    specs = [
        make_spec(duration=1.0, backpressure=TEXT_QUEUE_FULL),  # 先失败
        make_spec(duration=10.0),                               # 仍在运行
    ]
    results, stats, clock, launches, events = run_schedule(tmp_path, specs, workers=2)
    by_name = {r.source.name: r for r in results}
    assert by_name["doc01.pdf"].ok is True, "运行中的 worker 不应被冷却影响"
    assert by_name["doc01.pdf"].elapsed == pytest.approx(10.0)
    assert stats.backpressure_events == 1


def test_normal_failure_does_not_trigger_cooldown(tmp_path):
    specs = [
        make_spec(duration=1.0, fail_text=TEXT_NORMAL_FAIL),
        make_spec(duration=1.0),
    ]
    results, stats, clock, launches, events = run_schedule(tmp_path, specs, workers=1)
    assert stats.backpressure_events == 0
    assert stats.peak_cooldown == 0.0
    assert clock.total_slept == pytest.approx(0.0)
    assert events == []
    assert results[0].failure_kind == rpv.NONE
    assert results[1].ok is True


def test_quota_exhausted_does_not_trigger_cooldown(tmp_path):
    """额度耗尽重试无用：不得进入 cooldown 反复撞墙。"""
    specs = [
        make_spec(duration=1.0, fail_text=TEXT_QUOTA),
        make_spec(duration=1.0, fail_text=TEXT_QUOTA),
    ]
    results, stats, clock, launches, events = run_schedule(tmp_path, specs, workers=1)
    assert stats.backpressure_events == 0
    assert stats.quota_events == 2
    assert clock.total_slept == pytest.approx(0.0)
    assert all(r.failure_kind == rpv.QUOTA_EXHAUSTED for r in results)


def test_multiple_backpressure_events_grow_peak_cooldown(tmp_path):
    specs = [
        make_spec(duration=1.0, backpressure=TEXT_HTTP_429),
        make_spec(duration=1.0, backpressure=TEXT_HTTP_503),
        make_spec(duration=1.0, backpressure=TEXT_CODE_10010),
    ]
    results, stats, clock, launches, events = run_schedule(tmp_path, specs, workers=1)
    assert stats.backpressure_events == 3
    assert [c for _, _, c in events] == [5.0, 10.0, 20.0]
    assert stats.peak_cooldown == 20.0
    assert stats.peak_backoff_level == 3
    reasons = {reason for _, reason, _ in events}
    assert reasons == {rpv.REASON_HTTP_429, rpv.REASON_HTTP_503, rpv.REASON_QUEUE_FULL}


def test_success_between_events_shrinks_cooldown(tmp_path):
    specs = [
        make_spec(duration=1.0, backpressure=TEXT_HTTP_429),   # level 1 → 5s
        make_spec(duration=1.0, backpressure=TEXT_HTTP_429),   # level 2 → 10s
        make_spec(duration=1.0),                               # 成功 → level 1
        make_spec(duration=1.0, backpressure=TEXT_HTTP_429),   # level 2 → 10s（而非 20s）
    ]
    results, stats, clock, launches, events = run_schedule(tmp_path, specs, workers=1)
    assert [c for _, _, c in events] == [5.0, 10.0, 10.0]
    assert stats.peak_backoff_level == 2
    assert stats.peak_cooldown == 10.0
    assert results[2].ok is True


def test_launch_interval_is_enforced_between_launches(tmp_path):
    specs = [make_spec(duration=0.0) for _ in range(3)]
    results, stats, clock, launches, events = run_schedule(
        tmp_path, specs, workers=1, launch_interval=0.5
    )
    assert all(r.ok for r in results)
    times = [t for _, t in launches]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(gaps) == 2
    for gap in gaps:
        assert gap >= 0.5 - 1e-9, f"启动间隔未生效：{gaps}"


def test_launch_interval_zero_disables_spacing(tmp_path):
    specs = [make_spec(duration=0.0) for _ in range(3)]
    _, _, clock, launches, _ = run_schedule(
        tmp_path, specs, workers=1, launch_interval=0.0
    )
    assert clock.total_slept == pytest.approx(0.0)


def test_active_workers_never_exceed_limit_with_cooldown(tmp_path):
    specs = [make_spec(duration=1.0) for _ in range(5)]
    specs[0] = make_spec(duration=1.0, backpressure=TEXT_HTTP_503)
    observed: list[int] = []

    pdfs = []
    for index in range(len(specs)):
        pdf = tmp_path / f"doc{index:02d}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        pdfs.append(pdf)

    clock = FakeClock()
    plan = dict(enumerate(specs))

    def fake_launch(command):
        index = int(pathlib.Path(command[2]).stem.replace("doc", ""))
        spec = plan[index]
        process = FakeProcess(
            command,
            clock=clock,
            duration=spec["duration"],
            returncode=spec["returncode"],
            stderr=spec.get("stderr", ""),
        )
        process._start = clock.now
        process._done_at = clock.now + spec["duration"]
        return process

    def fake_wait(active):
        observed.append(len(active))
        pending = [p for p in active if p.poll() is None]
        if not pending:
            return list(active)
        clock.now = max(clock.now, min(p._done_at for p in pending))
        return [p for p in active if p.poll() is not None] or list(pending)

    rp.run_bounded(
        pdfs, workers=2, python=pathlib.Path(sys.executable),
        verbose=False, overwrite=False, keep_images=False,
        launch_interval=0.0, clock=clock, wait_any=fake_wait,
        launch_worker=fake_launch,
    )
    assert observed, "应当观察到 active 集合"
    assert max(observed) <= 2, f"并发上限被突破：{observed}"


def test_each_pdf_is_launched_exactly_once_even_with_backpressure(tmp_path):
    """压力失败也不得让调度器重跑整个 PDF pipeline。"""
    specs = [
        make_spec(duration=1.0, backpressure=TEXT_HTTP_429),
        make_spec(duration=1.0, backpressure=TEXT_HTTP_503),
        make_spec(duration=1.0, backpressure=TEXT_QUEUE_FULL),
    ]
    results, stats, clock, launches, events = run_schedule(tmp_path, specs, workers=1)
    names = [name for name, _ in launches]
    assert names == ["doc00.pdf", "doc01.pdf", "doc02.pdf"], f"出现重复提交：{names}"
    assert len(results) == 3
    assert all(not r.ok for r in results)


def test_backpressure_log_is_concise_per_event(tmp_path, capsys):
    """每次压力只输出一段说明，不刷屏。"""
    result = rp.TaskResult(
        source=pathlib.Path("B.pdf"),
        returncode=2,
        failure_kind=rpv.REMOTE_BACKPRESSURE,
        failure_reason=rpv.REASON_HTTP_429,
    )
    verdict = rpv.classify_text(TEXT_HTTP_429)
    rp.print_backpressure(result, verdict, 10.0, running=2)
    output = capsys.readouterr().out
    assert "Paddle 服务端繁忙" in output
    assert "429" in output
    assert "10" in output
    assert "继续执行" in output
    assert "不会被自动重跑" in output
    assert len([line for line in output.splitlines() if line.strip()]) <= 4


def test_summary_reports_backpressure_stats(tmp_path, capsys):
    results = [
        rp.TaskResult(source=pathlib.Path("A.pdf"), returncode=0),
        rp.TaskResult(
            source=pathlib.Path("B.pdf"),
            returncode=2,
            failure_kind=rpv.REMOTE_BACKPRESSURE,
            failure_reason=rpv.REASON_QUEUE_FULL,
        ),
        rp.TaskResult(
            source=pathlib.Path("C.pdf"),
            returncode=1,
            failure_kind=rpv.NONE,
        ),
    ]
    stats = rp.ScheduleStats()
    stats.record(rpv.classify_text(TEXT_CODE_10010), 10.0)
    stats.record(rpv.classify_text(TEXT_HTTP_429), 20.0)
    stats.peak_backoff_level = 3

    rp.print_summary(results, workers=2, elapsed=12.5, stats=stats)
    output = capsys.readouterr().out

    assert "Total:                3" in output
    assert "Succeeded:            1" in output
    assert "Failed:               2" in output
    assert "Backpressure events:  2" in output
    assert "Peak cooldown:        20s" in output
    assert "Paddle queue full: 1" in output
    assert "HTTP 429: 1" in output
    # 普通失败与压力失败要能区分
    assert "- B.pdf  [REMOTE_BACKPRESSURE]" in output
    assert "- C.pdf  [FAILED]" in output


def test_summary_warns_about_quota_exhaustion(tmp_path, capsys):
    results = [
        rp.TaskResult(
            source=pathlib.Path("D.pdf"),
            returncode=2,
            failure_kind=rpv.QUOTA_EXHAUSTED,
            failure_reason=rpv.REASON_QUOTA,
        )
    ]
    stats = rp.ScheduleStats()
    stats.record(rpv.classify_text(TEXT_QUOTA), 0.0)
    rp.print_summary(results, workers=1, elapsed=1.0, stats=stats)
    output = capsys.readouterr().out
    assert "额度耗尽" in output
    assert "重试无用" in output
    assert "Backpressure events:  0" in output


def test_launch_interval_negative_is_parameter_error(tmp_path, capsys):
    (tmp_path / "A.pdf").write_bytes(b"%PDF-1.4")
    code = rp.main([str(tmp_path / "A.pdf"), "--launch-interval", "-1"])
    assert code == rp.EXIT_FATAL == 2
    assert "launch-interval" in capsys.readouterr().err


def test_workers_above_threshold_prints_warning(tmp_path, capsys, monkeypatch):
    for name in ("A.pdf", "B.pdf", "C.pdf", "D.pdf", "E.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(rp, "find_tool_python", lambda: pathlib.Path(sys.executable))
    monkeypatch.setattr(rp, "run_bounded", lambda *a, **k: [])
    code = rp.main([str(tmp_path), "--workers", "5"])
    captured = capsys.readouterr()
    assert code == rp.EXIT_OK
    assert "警告" in captured.out
    assert "建议从 2 开始测试" in captured.out


def test_workers_at_or_below_threshold_has_no_warning(tmp_path, capsys, monkeypatch):
    (tmp_path / "A.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(rp, "find_tool_python", lambda: pathlib.Path(sys.executable))
    monkeypatch.setattr(rp, "run_bounded", lambda *a, **k: [])
    rp.main([str(tmp_path / "A.pdf"), "--workers", "2"])
    assert "警告" not in capsys.readouterr().out


def test_launch_interval_default_is_conservative():
    args = rp.build_parser().parse_args(["x.pdf"])
    assert args.launch_interval == rp.DEFAULT_LAUNCH_INTERVAL == 0.5


def test_workers_default_still_two():
    args = rp.build_parser().parse_args(["x.pdf"])
    assert args.workers == rp.DEFAULT_WORKERS == 2


def test_warning_reports_requested_workers_not_effective(tmp_path, capsys, monkeypatch):
    """--workers 5 但只有 1 个 PDF 时，告警要报请求值 5，而不是被截断后的 1。"""
    (tmp_path / "A.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(rp, "find_tool_python", lambda: pathlib.Path(sys.executable))
    monkeypatch.setattr(rp, "run_bounded", lambda *a, **k: [])
    rp.main([str(tmp_path / "A.pdf"), "--workers", "5"])
    out = capsys.readouterr().out
    assert "警告" in out
    assert "--workers=5" in out
    assert "workers=1" not in out


def test_summary_reports_elapsed_quota_and_failure_reason(capsys):
    results = [
        rp.TaskResult(
            source=pathlib.Path("BUSY.pdf"),
            returncode=2,
            failure_kind=rpv.REMOTE_BACKPRESSURE,
            failure_reason=rpv.REASON_HTTP_429,
        ),
        rp.TaskResult(
            source=pathlib.Path("QUOTA.pdf"),
            returncode=2,
            failure_kind=rpv.QUOTA_EXHAUSTED,
            failure_reason=rpv.REASON_QUOTA,
        ),
    ]
    stats = rp.ScheduleStats()
    stats.record(rpv.classify_text(TEXT_HTTP_429), 5.0)
    stats.record(rpv.classify_text(TEXT_QUOTA), 0.0)  # 额度耗尽不产生 cooldown

    rp.print_summary(results, workers=2, elapsed=78.34, stats=stats)
    out = capsys.readouterr().out

    assert "Elapsed:              78.3s" in out
    assert "Backpressure events:  1" in out
    assert "Quota events:         1" in out
    # 背压失败带上可读原因，配额失败只标注类别（不是临时限流）
    assert "- BUSY.pdf  [REMOTE_BACKPRESSURE]  http_429" in out
    assert "- QUOTA.pdf  [QUOTA_EXHAUSTED]" in out


def test_summary_omits_reason_when_unknown(capsys):
    results = [
        rp.TaskResult(
            source=pathlib.Path("X.pdf"),
            returncode=2,
            failure_kind=rpv.REMOTE_BACKPRESSURE,
            failure_reason=rpv.REASON_OTHER,
        )
    ]
    rp.print_summary(results, workers=1, elapsed=3.0)
    out = capsys.readouterr().out
    assert "- X.pdf  [REMOTE_BACKPRESSURE]\n" in out


def test_skipped_worker_reported_as_skip_not_ok():
    """产物已存在的 worker 退出码 0，但状态应显示 SKIP 而非 OK。"""
    result = rp.TaskResult(source=pathlib.Path("A.pdf"), returncode=0, skipped=True)
    assert rp.format_status(1, 2, result).startswith("[1/2] SKIP")


def test_summary_counts_skipped_separately(capsys):
    results = [
        rp.TaskResult(source=pathlib.Path("A.pdf"), returncode=0, skipped=True),
        rp.TaskResult(source=pathlib.Path("B.pdf"), returncode=0),
    ]
    rp.print_summary(results, workers=2, elapsed=1.0)
    out = capsys.readouterr().out
    assert "Succeeded:            2" in out
    assert "Skipped:              1" in out
