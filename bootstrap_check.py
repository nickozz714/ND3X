"""
bootstrap_check.py — Non-fatal startup bootstrap check voor PDF rendering.
Bugs gefixed: TEMPLATES_ROOT was str, nu Path.
"""
from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path
from typing import List
from component.config import settings

TEMPLATES_ROOT = Path(str(settings.FILES_DIR)) / "templates"
DEFAULT_TEMPLATE = os.getenv("DEFAULT_TEMPLATE", "beeminds").lower()

REQUIRED_BINS = ["pandoc", "xelatex", "mmdc", "pandoc-mermaid"]
REQUIRED_FONTS = ["Open Sans", "Roboto Slab"]


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)

def ok(msg: str) -> None:
    log(f"OK     | {msg}")

def warn(msg: str) -> None:
    log(f"WARNING| {msg}")

def err(msg: str) -> None:
    log(f"ERROR  | {msg}")


def has_bin(name: str) -> bool:
    return shutil.which(name) is not None


def has_font(font_name: str) -> bool:
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return False
    try:
        out = subprocess.check_output([fc_list], text=True, stderr=subprocess.DEVNULL)
        return font_name.lower() in out.lower()
    except Exception:
        return False


def check_binaries() -> None:
    log("Checking required binaries...")
    for b in REQUIRED_BINS:
        if has_bin(b):
            ok(f"binary found: {b}")
        else:
            warn(f"binary missing: {b}")


def check_fonts() -> None:
    log("Checking required fonts...")
    for f in REQUIRED_FONTS:
        if has_font(f):
            ok(f"font available: {f}")
        else:
            warn(f"font missing: {f} (XeLaTeX may fall back)")


def check_templates() -> None:
    log("Checking templates...")

    if not TEMPLATES_ROOT.exists():
        warn(f"templates root does not exist yet: {TEMPLATES_ROOT} (will be created on first use)")
        return

    template_dir = TEMPLATES_ROOT / DEFAULT_TEMPLATE
    if not template_dir.exists():
        available = [p.name for p in TEMPLATES_ROOT.iterdir() if p.is_dir()]
        warn(f"default template '{DEFAULT_TEMPLATE}' not found. Available: {available}")
        return

    template_tex = template_dir / "template.tex"
    if not template_tex.exists():
        err(f"template.tex missing in {template_dir}")
    else:
        ok(f"template found: {template_tex}")

    assets_dir = template_dir / "assets"
    if assets_dir.exists():
        ok(f"template assets dir present ({len(list(assets_dir.iterdir()))} files)")
    else:
        warn("template has no assets directory (logo optional)")


def check_mermaid_filter() -> None:
    log("Checking pandoc-mermaid filter...")
    if not has_bin("pandoc-mermaid"):
        warn("pandoc-mermaid filter not found in PATH")
        return
    try:
        subprocess.run(["pandoc-mermaid", "--help"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        ok("pandoc-mermaid filter executable")
    except Exception as e:
        warn(f"pandoc-mermaid present but failed to run: {e}")


def check_pdfinfo() -> None:
    log("Checking optional pdfinfo (page counting)...")
    if has_bin("pdfinfo"):
        ok("pdfinfo available (page counts enabled)")
    else:
        warn("pdfinfo missing (page count metadata disabled)")


def main() -> int:
    log("--------------------------------------------------")
    log("PDF Render Bootstrap Check (non-fatal)")
    log("--------------------------------------------------")
    try:
        check_binaries()
        check_mermaid_filter()
        check_fonts()
        check_templates()
        check_pdfinfo()
    except Exception as e:
        err(f"unexpected bootstrap error (ignored): {e}")
    log("--------------------------------------------------")
    log("Bootstrap check completed — continuing startup")
    log("--------------------------------------------------")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as fatal:
        print(f"[bootstrap] FATAL error ignored: {fatal}", flush=True)
        raise SystemExit(0)