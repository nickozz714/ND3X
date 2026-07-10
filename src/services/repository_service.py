"""
services/repository_service.py

The repository hub: registers GitHub repos, clones them into the managed
REPOS_DIR, tracks the active branch, and drives feature-branch / commit / push /
PR flows on top of GitService. Used by the repo__* agent tools and the repo
router (GUI). PATs are resolved from SecretService and never returned to callers.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.repository import Repository
from services.git_service import GitService, repos_root

log = get_logger(__name__)

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(name: str) -> str:
    # Also collapse any leading dots so a name can't become '..' / a traversal.
    n = _NAME_RE.sub("-", (name or "").strip()).strip("-.")
    return n or "repo"


def _owner_repo(remote_url: str) -> Optional[tuple[str, str]]:
    """Parse owner/repo from a GitHub URL (https or ssh)."""
    u = (remote_url or "").strip()
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", u)
    if not m:
        return None
    return m.group(1), m.group(2)


class RepositoryService:
    def __init__(self, db: Session):
        self.db = db
        self.git = GitService()

    # ------------------------------------------------------------- registry

    def list(self) -> List[Repository]:
        return self.db.query(Repository).order_by(Repository.name.asc()).all()

    def get(self, repo_id: int) -> Optional[Repository]:
        return self.db.query(Repository).filter(Repository.id == repo_id).first()

    def get_by_name(self, name: str) -> Optional[Repository]:
        return self.db.query(Repository).filter(Repository.name == name).first()

    def register(self, *, name: str, remote_url: str,
                 credential_secret: Optional[str] = None) -> Repository:
        name = _safe_name(name)
        if self.get_by_name(name):
            raise ValueError(f"A repository named '{name}' is already registered.")
        repo = Repository(name=name, remote_url=remote_url.strip(),
                          credential_secret=(credential_secret or None),
                          clone_status="registered")
        self.db.add(repo)
        self.db.commit()
        self.db.refresh(repo)
        log.infox("Repository geregistreerd", repo_id=repo.id, name=name)
        return repo

    def delete(self, repo_id: int, *, remove_files: bool = False) -> bool:
        repo = self.get(repo_id)
        if repo is None:
            return False
        path = repo.local_path
        self.db.delete(repo)
        self.db.commit()
        if remove_files and path and os.path.isdir(path) and os.path.abspath(path).startswith(repos_root()):
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        return True

    def _token(self, repo: Repository) -> Optional[str]:
        if not repo.credential_secret:
            return None
        try:
            from services.secret_service import SecretService
            return SecretService(self.db).get_value(repo.credential_secret)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ git

    async def clone(self, repo_id: int) -> Repository:
        repo = self.get(repo_id)
        if repo is None:
            raise ValueError(f"No repository with id={repo_id}")
        repo.clone_status = "cloning"
        repo.last_error = None
        self.db.commit()
        try:
            path = await self.git.clone(remote_url=repo.remote_url, name=repo.name,
                                        token=self._token(repo))
            repo.local_path = path
            repo.default_branch = await self.git.default_branch(path)
            repo.active_branch = await self.git.active_branch(path)
            repo.clone_status = "ready"
            repo.last_synced_at = datetime.now(timezone.utc)
        except Exception as exc:  # noqa: BLE001
            repo.clone_status = "error"
            repo.last_error = str(exc)[:1000]
            self.db.commit()
            raise
        self.db.commit()
        self.db.refresh(repo)
        return repo

    def _require_ready(self, repo: Repository) -> str:
        if not repo.local_path or not os.path.isdir(repo.local_path):
            raise ValueError(f"Repository '{repo.name}' is not cloned yet — clone it first.")
        return repo.local_path

    async def branches(self, repo_id: int) -> Dict[str, Any]:
        repo = self.get(repo_id)
        if repo is None:
            raise ValueError(f"No repository with id={repo_id}")
        return await self.git.branches(self._require_ready(repo))

    async def checkout(self, repo_id: int, branch: str) -> Repository:
        repo = self.get(repo_id)
        path = self._require_ready(repo)
        await self.git.checkout(path, branch)
        repo.active_branch = await self.git.active_branch(path)
        self.db.commit(); self.db.refresh(repo)
        return repo

    async def create_branch(self, repo_id: int, branch: str, *, from_ref: Optional[str] = None) -> Repository:
        repo = self.get(repo_id)
        path = self._require_ready(repo)
        await self.git.create_branch(path, branch, from_ref=from_ref)
        repo.active_branch = await self.git.active_branch(path)
        self.db.commit(); self.db.refresh(repo)
        return repo

    async def status(self, repo_id: int) -> Dict[str, Any]:
        repo = self.get(repo_id)
        return await self.git.status(self._require_ready(repo))

    async def diff(self, repo_id: int, *, staged: bool = False) -> str:
        repo = self.get(repo_id)
        return await self.git.diff(self._require_ready(repo), staged=staged)

    async def commit(self, repo_id: int, message: str) -> Dict[str, Any]:
        repo = self.get(repo_id)
        return await self.git.commit_all(self._require_ready(repo), message)

    async def push(self, repo_id: int, *, branch: Optional[str] = None) -> Dict[str, Any]:
        repo = self.get(repo_id)
        path = self._require_ready(repo)
        return await self.git.push(path, branch=branch, token=self._token(repo))

    async def open_pull_request(self, repo_id: int, *, title: str, head: Optional[str] = None,
                                base: Optional[str] = None, body: str = "") -> Dict[str, Any]:
        """Open a PR via the GitHub REST API using the repo's PAT."""
        repo = self.get(repo_id)
        if repo is None:
            raise ValueError(f"No repository with id={repo_id}")
        token = self._token(repo)
        if not token:
            raise ValueError("No credential (GitHub PAT) set for this repository — a token is required to open a PR.")
        owner_repo = _owner_repo(repo.remote_url)
        if not owner_repo:
            raise ValueError("Could not parse owner/repo from the remote URL.")
        owner, name = owner_repo
        head = head or repo.active_branch or await self.git.active_branch(self._require_ready(repo))
        base = base or repo.default_branch or "main"
        import httpx
        url = f"https://api.github.com/repos/{owner}/{name}/pulls"
        payload = {"title": title, "head": head, "base": base, "body": body or ""}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, json=payload, headers=headers)
        if r.status_code >= 300:
            raise ValueError(f"GitHub PR failed ({r.status_code}): {r.text[:400]}")
        data = r.json()
        return {"number": data.get("number"), "url": data.get("html_url"),
                "state": data.get("state"), "head": head, "base": base}

    # ------------------------------------------------------------- presenters

    def to_dict(self, repo: Repository) -> Dict[str, Any]:
        return {
            "id": repo.id,
            "name": repo.name,
            "remote_url": repo.remote_url,
            "local_path": repo.local_path,
            "default_branch": repo.default_branch,
            "active_branch": repo.active_branch,
            "has_credential": bool(repo.credential_secret),
            "credential_secret": repo.credential_secret,
            "clone_status": repo.clone_status,
            "last_error": repo.last_error,
            "last_synced_at": repo.last_synced_at.isoformat() if repo.last_synced_at else None,
        }
