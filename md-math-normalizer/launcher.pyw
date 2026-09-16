"""ASCII-path launcher for the Markdown math normalizer GUI."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def show_startup_error(details: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        window = tk.Tk()
        window.withdraw()
        messagebox.showerror("Markdown normalizer startup failed", details)
        window.destroy()
    except Exception:
        (ROOT / "normalizer_startup_error.txt").write_text(details, encoding="utf-8")


try:
    from md_math_normalizer.gui import main

    main()
except Exception:
    show_startup_error(traceback.format_exc())
