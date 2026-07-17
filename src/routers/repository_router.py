"""
routers/repository_router.py

User-facing repository hub API (the repo GUI tile): register/list/delete repos,
clone, list/switch/create branches, status/diff, commit, push, open PR, and a
small file browser + read/write so the user can make code changes manually. The
agent uses the same operations via the repo__* tools.

File access is confined to the repo's own working copy (path traversal blocked).
"""
from __future__ import annotations

import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from services.repository_service import RepositoryService

router = APIRouter(prefix="/repos", tags=["Repositories"])


def _svc(db: Session) -> RepositoryService:
    return RepositoryService(db)


def _repo_or_404(svc: RepositoryService, repo_id: int):
    repo = svc.get(repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo


def _safe_join(root: str, rel: str) -> str:
    """Resolve rel under root, rejecting traversal outside the working copy."""
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, rel or ""))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        raise HTTPException(status_code=400, detail="Path escapes the repository")
    return target


class RegisterRepo(BaseModel):
    name: str
    remote_url: str
    credential_secret: Optional[str] = None


class BranchBody(BaseModel):
    branch: str
    from_ref: Optional[str] = None


class CommitBody(BaseModel):
    message: str


class PushBody(BaseModel):
    branch: Optional[str] = None


class PRBody(BaseModel):
    title: str
    body: str = ""
    head: Optional[str] = None
    base: Optional[str] = None


class WriteFileBody(BaseModel):
    path: str
    content: str


# ------------------------------------------------------------------- registry


@router.get("")
def list_repos(db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    return [svc.to_dict(r) for r in svc.list()]


@router.post("")
def register_repo(data: RegisterRepo, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    try:
        r = svc.register(name=data.name, remote_url=data.remote_url,
                         credential_secret=data.credential_secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return svc.to_dict(r)


@router.delete("/{repo_id}")
def delete_repo(repo_id: int, remove_files: bool = Query(False),
                db: Session = Depends(get_db), user=Depends(require_user)):
    if not _svc(db).delete(repo_id, remove_files=remove_files):
        raise HTTPException(status_code=404, detail="Repository not found")
    return {"ok": True}


# ------------------------------------------------------------------------ git


@router.post("/{repo_id}/clone")
async def clone_repo(repo_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        return svc.to_dict(await svc.clone(repo_id))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{repo_id}/branches")
async def repo_branches(repo_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        return await svc.branches(repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{repo_id}/branch")
async def create_branch(repo_id: int, body: BranchBody, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        return svc.to_dict(await svc.create_branch(repo_id, body.branch, from_ref=body.from_ref))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{repo_id}/checkout")
async def checkout(repo_id: int, body: BranchBody, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        return svc.to_dict(await svc.checkout(repo_id, body.branch))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{repo_id}/status")
async def repo_status(repo_id: int, diff: bool = Query(False),
                      db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        out = await svc.status(repo_id)
        if diff:
            out["diff"] = (await svc.diff(repo_id))[:100000]
        return out
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{repo_id}/commit")
async def commit(repo_id: int, body: CommitBody, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        return await svc.commit(repo_id, body.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{repo_id}/push")
async def push(repo_id: int, body: PushBody, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        return await svc.push(repo_id, branch=body.branch)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{repo_id}/pull-request")
async def open_pr(repo_id: int, body: PRBody, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    _repo_or_404(svc, repo_id)
    try:
        return await svc.open_pull_request(repo_id, title=body.title, head=body.head,
                                           base=body.base, body=body.body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ------------------------------------------------------ file browser + editor


@router.get("/{repo_id}/files")
def list_files(repo_id: int, path: str = Query(""),
               db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    repo = _repo_or_404(svc, repo_id)
    if not repo.local_path or not os.path.isdir(repo.local_path):
        raise HTTPException(status_code=400, detail="Repository is not cloned yet")
    target = _safe_join(repo.local_path, path)
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail="Not a directory")
    entries = []
    for name in sorted(os.listdir(target)):
        if name == ".git":
            continue
        full = os.path.join(target, name)
        entries.append({"name": name, "is_dir": os.path.isdir(full),
                        "rel": os.path.relpath(full, repo.local_path)})
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"path": path, "entries": entries}


@router.get("/{repo_id}/file")
def read_file(repo_id: int, path: str = Query(...),
              db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    repo = _repo_or_404(svc, repo_id)
    if not repo.local_path:
        raise HTTPException(status_code=400, detail="Repository is not cloned yet")
    target = _safe_join(repo.local_path, path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")
    if os.path.getsize(target) > 2_000_000:
        raise HTTPException(status_code=413, detail="File too large to edit here (>2MB)")
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Binary file — cannot edit as text")
    return {"path": path, "content": content}


@router.put("/{repo_id}/file")
def write_file(repo_id: int, body: WriteFileBody,
               db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    repo = _repo_or_404(svc, repo_id)
    if not repo.local_path:
        raise HTTPException(status_code=400, detail="Repository is not cloned yet")
    target = _safe_join(repo.local_path, body.path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"ok": True, "path": body.path}
