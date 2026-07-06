"""
routers/image_routes.py

Serve generated images by filename — mirrors the generated-PDF pattern
(/pdf/file/{filename}): filenames are unguessable (timestamp + random hex), and
markdown <img> tags cannot send Authorization headers, so the file route itself
is unauthenticated just like the PDF one.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Response

router = APIRouter(prefix="/images", tags=["Images"])

_SAFE_NAME = re.compile(r"^img_[0-9T]+_[a-f0-9]{8}\.png$")


@router.get("/file/{filename}")
def get_image_file(filename: str) -> Response:
    if not _SAFE_NAME.fullmatch(filename or ""):
        raise HTTPException(status_code=404, detail="Image not found")
    from services.builtin.tools.image_tools import generated_images_dir
    path = generated_images_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(content=path.read_bytes(), media_type="image/png")
