"""Simple Windows GUI entry point for the Markdown math normalizer."""

from __future__ import annotations

import threading
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .api import check_markdown_file, normalize_markdown_file
from .validator import validate_markdown_text


class NormalizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("中文数学 Markdown 规范化工具")
        self.geometry("820x560")
        self.minsize(700, 460)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="请选择一个 Markdown 文件")

        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)

        title = ttk.Label(
            self,
            text="中文数学 Markdown 规范化工具",
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        title.grid(row=0, column=0, columnspan=3, sticky="w", padx=18, pady=(16, 4))

        subtitle = ttk.Label(
            self,
            text="使用项目完整流程进行检查或生成规范化副本，不修改原文件。",
        )
        subtitle.grid(row=1, column=0, columnspan=3, sticky="w", padx=18, pady=(0, 14))

        ttk.Label(self, text="待检查文件").grid(row=2, column=0, sticky="w", padx=(18, 8), pady=6)
        ttk.Entry(self, textvariable=self.input_var).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Button(self, text="选择文件", command=self._choose_input).grid(
            row=2, column=2, sticky="e", padx=(8, 18), pady=6
        )

        ttk.Label(self, text="输出文件").grid(row=3, column=0, sticky="w", padx=(18, 8), pady=6)
        ttk.Entry(self, textvariable=self.output_var).grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Button(self, text="选择位置", command=self._choose_output).grid(
            row=3, column=2, sticky="e", padx=(8, 18), pady=6
        )

        self.log = tk.Text(self, height=16, wrap="word", state="disabled")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew", padx=18, pady=(14, 8))

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", padx=18, pady=(0, 8))

        buttons = ttk.Frame(self)
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", padx=18, pady=(0, 8))
        buttons.columnconfigure(0, weight=1)
        self.check_button = ttk.Button(buttons, text="只检查", command=self._check)
        self.check_button.grid(row=0, column=0, sticky="w")
        self.normalize_button = ttk.Button(
            buttons,
            text="规范化并生成文件",
            command=self._normalize,
        )
        self.normalize_button.grid(row=0, column=1, sticky="e")

        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").grid(
            row=7, column=0, columnspan=3, sticky="ew", padx=18, pady=(0, 14)
        )

    def _choose_input(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择待检查的 Markdown 文件",
            filetypes=[("Markdown 文件", "*.md"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        path = Path(selected)
        self.input_var.set(str(path))
        self.output_var.set(str(self._default_output(path)))
        self._write_log(f"已选择：{path}")
        self.status_var.set("文件已选择，可以开始检查或规范化")

    def _choose_output(self) -> None:
        initial = Path(self.output_var.get()) if self.output_var.get() else None
        selected = filedialog.asksaveasfilename(
            title="选择规范化结果保存位置",
            initialdir=str(initial.parent) if initial else None,
            initialfile=initial.name if initial else "规范化结果.md",
            defaultextension=".md",
            filetypes=[("Markdown 文件", "*.md"), ("所有文件", "*.*")],
        )
        if selected:
            self.output_var.set(selected)

    @staticmethod
    def _default_output(source: Path) -> Path:
        candidate = source.with_name(f"{source.stem}_规范化{source.suffix}")
        index = 2
        while candidate.exists():
            candidate = source.with_name(f"{source.stem}_规范化_{index}{source.suffix}")
            index += 1
        return candidate

    def _get_input(self) -> Path | None:
        value = self.input_var.get().strip()
        if not value:
            messagebox.showwarning("尚未选择文件", "请先点击“选择文件”。")
            return None
        path = Path(value)
        if not path.is_file():
            messagebox.showerror("文件不存在", f"找不到文件：\n{path}")
            return None
        if path.suffix.lower() != ".md":
            messagebox.showwarning("文件类型提示", "建议选择扩展名为 .md 的 Markdown 文件。")
        return path

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.check_button.configure(state=state)
        self.normalize_button.configure(state=state)
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    def _check(self) -> None:
        source = self._get_input()
        if source is None:
            return
        self._set_busy(True)
        self.status_var.set("正在检查，请稍候……")
        threading.Thread(target=self._check_worker, args=(source,), daemon=True).start()

    def _check_worker(self, source: Path) -> None:
        try:
            result = check_markdown_file(source)
            self.after(0, self._show_check_result, source, result)
        except Exception as exc:
            self.after(0, self._show_error, exc)

    def _show_check_result(self, source: Path, result) -> None:
        self._set_busy(False)
        self._write_log(f"检查文件：{source}")
        self._write_log(f"is_valid = {result.is_valid}")
        self._write_log(f"needs_normalization = {result.needs_normalization}")
        self._write_diagnostics(result.diagnostics)
        if result.has_fatal_error:
            self.status_var.set("检查完成：文件存在无法自动修复的问题")
        elif result.conforming:
            self.status_var.set("检查通过：文件已经符合规范")
        else:
            self.status_var.set("检查完成：文件可规范化，但当前仍需要处理")

    def _normalize(self) -> None:
        source = self._get_input()
        if source is None:
            return
        output_value = self.output_var.get().strip()
        if not output_value:
            self.output_var.set(str(self._default_output(source)))
            output_value = self.output_var.get()
        output = Path(output_value)
        if output.exists() and not messagebox.askyesno(
            "确认覆盖", f"输出文件已存在，是否覆盖？\n{output}"
        ):
            return

        self._set_busy(True)
        self.status_var.set("正在规范化，请稍候……")
        threading.Thread(target=self._normalize_worker, args=(source, output), daemon=True).start()

    def _normalize_worker(self, source: Path, output: Path) -> None:
        try:
            written = normalize_markdown_file(source, output)
            # The normalize API has already run the complete idempotent
            # pipeline. Validate the written result directly instead of
            # calling check_markdown_file, which would normalize it again.
            result = validate_markdown_text(written.read_text(encoding="utf-8"))
            self.after(0, self._show_normalize_result, written, result)
        except Exception as exc:
            self.after(0, self._show_error, exc)

    def _show_normalize_result(self, output: Path, result) -> None:
        self._set_busy(False)
        self._write_log(f"已生成：{output}")
        self._write_log(f"is_valid = {result.is_valid}")
        self._write_log(f"needs_normalization = {result.needs_normalization}")
        self._write_diagnostics(result.diagnostics)
        if result.is_valid and not result.needs_normalization:
            self.status_var.set("完成：规范化文件已生成并通过检查")
            try:
                os.startfile(output.parent)
            except OSError:
                pass
            messagebox.showinfo("处理完成", f"规范化文件已生成：\n{output}")
        else:
            self.status_var.set("已生成文件，但最终检查未通过")
            messagebox.showwarning("需要处理", "文件已生成，请查看窗口中的诊断信息。")

    def _write_diagnostics(self, diagnostics) -> None:
        if not diagnostics:
            self._write_log("diagnostics = 0")
            return
        for diagnostic in diagnostics:
            self._write_log(
                f"{diagnostic.code} [{diagnostic.line}:{diagnostic.column}] "
                f"{diagnostic.message}"
            )

    def _show_error(self, exc: Exception) -> None:
        self._set_busy(False)
        self.status_var.set("处理失败")
        self._write_log(f"{type(exc).__name__}: {exc}")
        messagebox.showerror("处理失败", str(exc))

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    app = NormalizerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
