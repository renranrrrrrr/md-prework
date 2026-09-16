"""Reliable double-click launcher for the Markdown math normalizer GUI."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def _show_startup_error(details: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        window = tk.Tk()
        window.withdraw()
        messagebox.showerror("数学规范化工具启动失败", details)
        window.destroy()
    except Exception:
        error_file = ROOT / "数学规范化工具_启动错误.txt"
        error_file.write_text(details, encoding="utf-8")


try:
    from md_math_normalizer.gui import main

    main()
except Exception:
    _show_startup_error(traceback.format_exc())
