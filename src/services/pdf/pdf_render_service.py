"""
services/pdf/pdf_render_service.py
Geporteerd van MCP project. templates_root en output_dir komen uit FILES_DIR.
PDF bestandsnaam gebruikt de markdown titel als slug.
"""
from __future__ import annotations
import base64, os, re, shutil, subprocess, tempfile, uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml


@dataclass
class RenderRequest:
    markdown: str
    template: str = "beeminds"
    properties: Optional[Dict[str, Any]] = None
    logo_bytes_b64: Optional[str] = None
    logo_filename: Optional[str] = None
    pdf_engine: str = "xelatex"
    timeout_sec: int = 180

@dataclass
class RenderResult:
    pdf_bytes: bytes
    pdf_path: Path
    filename: str
    warnings: List[str]
    meta: Dict[str, Any]


def _slugify(s: str, max_len: int = 60) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "document")[:max_len]


def _extract_title_from_markdown(md: str) -> Optional[str]:
    if md.startswith("---\n"):
        end = md.find("\n---\n", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(md[4:end])
                if isinstance(fm, dict) and fm.get("title"):
                    return str(fm["title"]).strip()
            except Exception:
                pass
    for line in md.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return None


def validate_properties(properties: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(properties, dict):
        return ["Properties must be a dictionary."], []
    if not properties.get("title"):
        warnings.append("Missing 'title'. Cover page will be generated without a title.")
    if not properties.get("author"):
        warnings.append("Missing 'author'. Header/footer will not show an author.")
    for key in ["toc", "number-sections"]:
        if key in properties and not isinstance(properties[key], bool):
            errors.append(f"'{key}' must be a boolean (true/false).")
    def _check_numeric(name: str, default: int):
        if name not in properties: return
        val = properties[name]
        if isinstance(val, str) and val.lower().strip() == "none":
            errors.append(f"'{name}: none' is invalid. Must be numeric (e.g. {default}).")
            return
        if not isinstance(val, int):
            errors.append(f"'{name}' must be an integer (e.g. {default}).")
    _check_numeric("secnumdepth", 3)
    _check_numeric("toc-depth", 3)
    _check_numeric("tocdepth", 3)
    if "logo" in properties:
        logo = properties["logo"]
        if not isinstance(logo, str):
            errors.append("'logo' must be a string path.")
        elif not logo.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
            warnings.append(f"Logo format '{logo}' may not be supported. Recommended: PDF or PNG.")
    if "status" in properties and not isinstance(properties["status"], str):
        errors.append("'status' must be a string.")
    if properties.get("number-sections") is True:
        warnings.append("Ensure headings do NOT contain manual numbers. Pandoc will auto-number.")
    if "lang" in properties and not isinstance(properties["lang"], str):
        errors.append("'lang' must be a string (e.g. 'nl-NL').")
    return errors, warnings


class PdfRenderService:
    def __init__(self, templates_root: Path, output_dir: Path):
        self.templates_root = Path(templates_root)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def list_templates(self) -> List[str]:
        if not self.templates_root.exists():
            return []
        return sorted([p.name for p in self.templates_root.iterdir() if p.is_dir()])

    def get_template_files(self, template: str) -> Dict[str, Any]:
        tdir = self._resolve_template_dir(template)
        tex = (tdir / "template.tex").read_text(encoding="utf-8")
        assets = []
        assets_dir = tdir / "assets"
        if assets_dir.exists():
            for p in assets_dir.rglob("*"):
                if p.is_file():
                    assets.append(str(p.relative_to(tdir)))
        return {"template": template, "template_tex": tex, "assets": assets}

    def render(self, req: RenderRequest) -> RenderResult:
        self._check_dependencies(req.pdf_engine)
        tdir = self._resolve_template_dir(req.template)
        template_tex_path = tdir / "template.tex"
        if not template_tex_path.exists():
            raise FileNotFoundError(f"Template missing: {template_tex_path}")

        properties = req.properties or {}
        existing_fm, md_body = self._extract_front_matter(req.markdown)
        merged_fm = self._build_front_matter(existing_fm, properties)
        merged_fm.setdefault("date", datetime.now().strftime("%Y-%m-%d %H:%M"))
        merged_fm.setdefault("status", "Draft")
        merged_fm.setdefault("toc", True)

        warnings: List[str] = []
        if self._detect_manual_heading_numbers(md_body) and bool(merged_fm.get("number-sections", True)):
            warnings.append("Detected headings with manual numbers while numbering is enabled.")

        title = merged_fm.get("title") or _extract_title_from_markdown(req.markdown) or "document"
        title_slug = _slugify(str(title))
        pdf_filename = f"{title_slug}_{uuid.uuid4().hex[:8]}.pdf"
        pdf_output_path = self.output_dir / pdf_filename

        with tempfile.TemporaryDirectory(prefix="pdf-render-") as td:
            wd = Path(td)
            workspace_template = wd / "template.tex"
            workspace_template.write_text(template_tex_path.read_text(encoding="utf-8"), encoding="utf-8")

            html_filter_src = tdir / "html-tables-to-pandoc.lua"
            if html_filter_src.exists():
                (wd / "html-tables-to-pandoc.lua").write_text(html_filter_src.read_text(encoding="utf-8"), encoding="utf-8")

            workspace_assets = wd / "assets"
            workspace_assets.mkdir(exist_ok=True)
            if (tdir / "assets").exists():
                self._copy_tree(tdir / "assets", workspace_assets)

            if req.logo_bytes_b64:
                logo_fn = req.logo_filename or "logo.pdf"
                (workspace_assets / logo_fn).write_bytes(base64.b64decode(req.logo_bytes_b64))
                merged_fm["logo"] = f"assets/{logo_fn}"
            elif "logo" not in merged_fm:
                default_logo = self._find_default_logo(workspace_assets)
                if default_logo:
                    merged_fm["logo"] = f"assets/{default_logo.name}"

            final_md = self._prepend_front_matter(md_body, merged_fm)
            input_md = wd / "input.md"
            input_md.write_text(final_md, encoding="utf-8")

            mmdc_quiet = wd / "mmdc-quiet"
            self._write_mmdc_quiet(mmdc_quiet)
            output_pdf = wd / "output.pdf"

            try:
                self._run_pandoc(workdir=wd, input_md=input_md, output_pdf=output_pdf,
                                 template_tex=workspace_template, resource_paths=[wd, workspace_assets],
                                 pdf_engine=req.pdf_engine, timeout_sec=req.timeout_sec,
                                 env={"MERMAID_BIN": str(mmdc_quiet)})
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
                stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
                raise RuntimeError(f"Pandoc render failed.\nSTDERR:\n{stderr}\n\nSTDOUT:\n{stdout}") from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"Pandoc render timed out after {req.timeout_sec}s.") from e

            pdf_bytes = output_pdf.read_bytes()
            pages = self._count_pages(output_pdf)

        pdf_output_path.write_bytes(pdf_bytes)
        return RenderResult(pdf_bytes=pdf_bytes, pdf_path=pdf_output_path, filename=pdf_filename,
                            warnings=warnings,
                            meta={"template": req.template, "pdf_engine": req.pdf_engine,
                                  "pages": pages, "title": title, "filename": pdf_filename})

    def _resolve_template_dir(self, template: str) -> Path:
        name = template.strip().lower()
        tdir = self.templates_root / name
        if not tdir.exists() or not tdir.is_dir():
            raise FileNotFoundError(f"Unknown template '{template}'. Available: [{', '.join(self.list_templates())}]")
        return tdir

    def _find_default_logo(self, assets_dir: Path) -> Optional[Path]:
        for fn in ["beeminds-logo.pdf", "logo.pdf", "beeminds-logo.png", "logo.png"]:
            p = assets_dir / fn
            if p.exists():
                return p
        return None

    def _extract_front_matter(self, md: str) -> Tuple[Optional[Dict[str, Any]], str]:
        if not md.startswith("---\n"):
            return None, md
        end = md.find("\n---\n", 4)
        if end == -1:
            return None, md
        try:
            fm = yaml.safe_load(md[4:end])
            return (fm if isinstance(fm, dict) else None), md[end + 5:]
        except yaml.YAMLError:
            return None, md

    def _build_front_matter(self, existing: Optional[Dict[str, Any]], override: Dict[str, Any]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        if existing:
            merged.update(existing)
        merged.update(override)
        def _to_int(x: Any, default: int) -> int:
            try:
                return default if (isinstance(x, str) and x.strip().lower() == "none") else int(x)
            except Exception:
                return default
        merged["toc-depth"] = _to_int(merged.get("toc-depth", merged.get("tocdepth", 3)), 3)
        merged["secnumdepth"] = _to_int(merged.get("secnumdepth", 3), 3)
        merged.pop("tocdepth", None)
        return merged

    def _prepend_front_matter(self, md_body: str, front_matter: Dict[str, Any]) -> str:
        return f"---\n{yaml.safe_dump(front_matter, sort_keys=False).strip()}\n---\n\n{md_body.lstrip()}"

    def _detect_manual_heading_numbers(self, md_body: str) -> bool:
        return bool(re.compile(r"^#{1,6}\s+\d+(\.\d+)*\s+\S", re.MULTILINE).search(md_body))

    def _check_dependencies(self, pdf_engine: str) -> None:
        missing = [dep for dep in ["pandoc", "mmdc"] if shutil.which(dep) is None]
        if shutil.which(pdf_engine) is None:
            missing.append(pdf_engine)
        if missing:
            raise RuntimeError(f"Missing dependencies: {', '.join(missing)}.")
        if shutil.which("pandoc-mermaid") is None:
            raise RuntimeError("Missing: pandoc-mermaid (pip install pandoc-mermaid-filter)")

    def _write_mmdc_quiet(self, wrapper_path: Path) -> None:
        wrapper_path.write_text("#!/usr/bin/env bash\nexec mmdc \"$@\" 1>/dev/null\n", encoding="utf-8")
        wrapper_path.chmod(0o755)

    def _run_pandoc(self, *, workdir, input_md, output_pdf, template_tex,
                    resource_paths, pdf_engine, timeout_sec, env) -> None:
        html_filter = template_tex.parent / "html-tables-to-pandoc.lua"
        cmd = ["pandoc", str(input_md), "--from", "markdown+raw_html", "--to", "pdf",
               "--pdf-engine", pdf_engine, "--template", str(template_tex),
               "--lua-filter", str(html_filter), "--filter", "pandoc-mermaid",
               "--resource-path", ":".join(str(p) for p in resource_paths),
               "--output", str(output_pdf)]
        subprocess.run(cmd, cwd=str(workdir), env={**os.environ, **env},
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=timeout_sec)

    def _copy_tree(self, src: Path, dst: Path) -> None:
        for p in src.rglob("*"):
            rel = p.relative_to(src)
            target = dst / rel
            if p.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(p.read_bytes())

    def _count_pages(self, pdf_path: Path) -> Optional[int]:
        pdfinfo = shutil.which("pdfinfo")
        if not pdfinfo:
            return None
        try:
            out = subprocess.check_output([pdfinfo, str(pdf_path)], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if line.lower().startswith("pages:"):
                    return int(line.split(":")[1].strip())
        except Exception:
            return None
        return None