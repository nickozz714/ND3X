"""
services/pdf/template_service.py
CRUD voor PDF templates op het filesystem.
"""
from __future__ import annotations
import base64
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


class TemplateService:
    def __init__(self, templates_root: Path):
        self.root = Path(templates_root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ── Listing ───────────────────────────────────────────────────────────────

    def list_templates(self) -> List[Dict[str, Any]]:
        if not self.root.exists():
            return []
        result = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            result.append(self._describe(d))
        return result

    def get_template(self, name: str) -> Dict[str, Any]:
        tdir = self._resolve(name)
        return self._describe(tdir, include_tex=True)

    # ── Create / copy ─────────────────────────────────────────────────────────

    def create_template(self, name: str, copy_from: Optional[str] = None) -> Dict[str, Any]:
        slug = name.strip().lower()
        tdir = self.root / slug
        if tdir.exists():
            raise ValueError(f"Template '{slug}' bestaat al")

        if copy_from:
            src = self._resolve(copy_from)
            shutil.copytree(src, tdir)
        else:
            tdir.mkdir(parents=True)
            (tdir / "assets").mkdir()
            (tdir / "template.tex").write_text(
                _DEFAULT_TEX.format(name=name), encoding="utf-8"
            )

        return self._describe(tdir, include_tex=True)

    # ── Update tex ────────────────────────────────────────────────────────────

    def update_tex(self, name: str, tex_content: str) -> Dict[str, Any]:
        tdir = self._resolve(name)
        (tdir / "template.tex").write_text(tex_content, encoding="utf-8")
        return self._describe(tdir, include_tex=True)

    # ── Assets ────────────────────────────────────────────────────────────────

    def upload_asset(self, name: str, filename: str, data_b64: str) -> Dict[str, Any]:
        tdir = self._resolve(name)
        assets_dir = tdir / "assets"
        assets_dir.mkdir(exist_ok=True)
        (assets_dir / filename).write_bytes(base64.b64decode(data_b64))
        return self._describe(tdir)

    def delete_asset(self, name: str, filename: str) -> Dict[str, Any]:
        tdir = self._resolve(name)
        asset_path = tdir / "assets" / filename
        if not asset_path.exists():
            raise FileNotFoundError(f"Asset '{filename}' niet gevonden in template '{name}'")
        asset_path.unlink()
        return self._describe(tdir)

    def get_asset(self, name: str, filename: str) -> bytes:
        tdir = self._resolve(name)
        asset_path = tdir / "assets" / filename
        if not asset_path.exists():
            raise FileNotFoundError(f"Asset '{filename}' niet gevonden")
        return asset_path.read_bytes()

    # ── Delete template ───────────────────────────────────────────────────────

    def delete_template(self, name: str) -> None:
        tdir = self._resolve(name)
        shutil.rmtree(tdir)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _resolve(self, name: str) -> Path:
        slug = name.strip().lower()
        tdir = self.root / slug
        if not tdir.exists() or not tdir.is_dir():
            available = [d.name for d in self.root.iterdir() if d.is_dir()]
            raise FileNotFoundError(f"Template '{slug}' niet gevonden. Beschikbaar: {available}")
        return tdir

    def _describe(self, tdir: Path, include_tex: bool = False) -> Dict[str, Any]:
        assets_dir = tdir / "assets"
        assets = []
        if assets_dir.exists():
            for p in sorted(assets_dir.rglob("*")):
                if p.is_file():
                    assets.append({
                        "filename": p.name,
                        "path": str(p.relative_to(tdir)),
                        "size": p.stat().st_size,
                    })

        result: Dict[str, Any] = {
            "name": tdir.name,
            "assets": assets,
            "has_tex": (tdir / "template.tex").exists(),
            "has_lua_filter": (tdir / "html-tables-to-pandoc.lua").exists(),
        }

        if include_tex and (tdir / "template.tex").exists():
            result["template_tex"] = (tdir / "template.tex").read_text(encoding="utf-8")

        return result


_DEFAULT_TEX = """\
% Template: {name}
% Minimaal LaTeX template voor Pandoc PDF rendering
\\documentclass[12pt]{{article}}
\\usepackage{{fontspec}}
\\usepackage{{geometry}}
\\geometry{{a4paper, margin=2.5cm}}

$if(title)$
\\title{{$title$}}
$endif$
$if(author)$
\\author{{$author$}}
$endif$
$if(date)$
\\date{{$date$}}
$endif$

\\begin{{document}}
$if(title)$
\\maketitle
$endif$
$body$
\\end{{document}}
"""