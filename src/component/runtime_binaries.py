"""Make bundled external binaries (ffmpeg, pandoc, poppler, …) discoverable.

The desktop package can ship platform binaries in a `bin/` dir next to the frozen
executable. Prepending that dir (and an optional ND3X_BIN_DIR override) to PATH at
startup means every `shutil.which(...)` / `subprocess(["ffmpeg", ...])` call across
the app finds the bundled binary — no per-call rewiring. When a binary is absent,
the existing feature checks (e.g. pdf_render_service uses shutil.which) degrade
gracefully with a clear "not installed" message.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List


def _bundled_bin_dirs() -> List[Path]:
    dirs: List[Path] = []
    extra = os.environ.get("ND3X_BIN_DIR")
    if extra:
        dirs.append(Path(extra).expanduser())
    if getattr(sys, "frozen", False):  # PyInstaller onedir: binaries sit beside the exe
        dirs.append(Path(sys.executable).resolve().parent / "bin")
    seen, out = set(), []
    for d in dirs:
        s = str(d)
        if d.is_dir() and s not in seen:
            seen.add(s)
            out.append(d)
    return out


def prepend_bundled_bin_to_path() -> None:
    """Idempotently put the bundled bin dir(s) at the front of PATH."""
    dirs = _bundled_bin_dirs()
    if not dirs:
        return
    cur = os.environ.get("PATH", "")
    cur_parts = cur.split(os.pathsep) if cur else []
    new = [str(d) for d in dirs if str(d) not in cur_parts]
    if new:
        os.environ["PATH"] = os.pathsep.join(new + cur_parts)
