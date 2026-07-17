"""
services/git_service.py

Shell-git wrapper for the repository hub. Runs the real git CLI (via the shell
exec service) so behaviour matches a terminal exactly — no extra dependency.

Auth: a GitHub PAT is passed per network command as an HTTP extra header
(`AUTHORIZATION: basic base64(x-access-token:<pat>)`), so the token is never
written to `.git/config` or the URL (no reflog/history leak). Local operations
(branch, checkout, status, diff, commit) need no token.

Every interpolated value is shlex-quoted — the exec service runs commands
through a shell, and branch names / messages may come from the agent or user.
"""
from __future__ import annotations

import base64
import os
import shlex
from typing import Any, Dict, List, Optional

from component.logging import get_logger

log = get_logger(__name__)


class GitError(RuntimeError):
    pass


def repos_root() -> str:
    from component.config import settings
    root = os.path.abspath(getattr(settings, "REPOS_DIR", "./repos") or "./repos")
    os.makedirs(root, exist_ok=True)
    return root


def _auth_header_args(token: Optional[str]) -> List[str]:
    """`-c http.extraHeader=...` args for an authenticated network call."""
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # Quoted as one -c value; the header itself carries no shell metacharacters.
    return ["-c", f"http.extraHeader=AUTHORIZATION: basic {basic}"]


async def _run(args: List[str], *, cwd: Optional[str] = None, timeout: float = 120.0,
               token: Optional[str] = None, redact: Optional[str] = None) -> Dict[str, Any]:
    """Run one git command. `args` is the git argv AFTER any auth/-C flags.
    Returns {returncode, stdout, stderr}; raises GitError on non-zero."""
    from services.shell.shell_exec_service import exec_command

    full: List[str] = ["git"]
    if cwd:
        full += ["-C", cwd]
    full += _auth_header_args(token)
    full += args
    cmd = " ".join(shlex.quote(a) for a in full)
    result = await exec_command(cmd, timeout=timeout)
    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()
    rc = result.get("returncode", result.get("exit_code", 0))
    if redact:
        # Belt and suspenders: never surface the token if git echoes it.
        out = out.replace(redact, "***")
        err = err.replace(redact, "***")
    if rc not in (0, None):
        raise GitError(f"git {args[0] if args else ''} failed (exit {rc}): {err or out}")
    return {"returncode": rc, "stdout": out, "stderr": err}


class GitService:
    """Stateless git operations on a local working copy."""

    # ---------------------------------------------------------------- clone

    async def clone(self, *, remote_url: str, name: str, token: Optional[str] = None,
                    dest: Optional[str] = None) -> str:
        """Clone into <REPOS_DIR>/<name> (or `dest`). Returns the local path."""
        path = dest or os.path.join(repos_root(), name)
        if os.path.exists(os.path.join(path, ".git")):
            raise GitError(f"A git working copy already exists at {path}")
        await _run(["clone", remote_url, path], token=token, timeout=600.0, redact=token)
        return path

    # ------------------------------------------------------------- inspect

    async def default_branch(self, path: str) -> Optional[str]:
        try:
            r = await _run(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=path)
            ref = r["stdout"].strip()
            return ref.split("/", 1)[1] if "/" in ref else (ref or None)
        except GitError:
            return None

    async def active_branch(self, path: str) -> Optional[str]:
        try:
            r = await _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
            return r["stdout"].strip() or None
        except GitError:
            return None

    async def branches(self, path: str) -> Dict[str, Any]:
        """Local + remote branch names and the active branch."""
        active = await self.active_branch(path)
        local = await _run(["branch", "--format=%(refname:short)"], cwd=path)
        remote = await _run(["branch", "-r", "--format=%(refname:short)"], cwd=path)
        local_list = [b.strip() for b in local["stdout"].splitlines() if b.strip()]
        remote_list = [b.strip() for b in remote["stdout"].splitlines()
                       if b.strip() and "->" not in b]
        return {"active": active, "local": local_list, "remote": remote_list}

    async def status(self, path: str) -> Dict[str, Any]:
        r = await _run(["status", "--porcelain=v1", "--branch"], cwd=path)
        lines = r["stdout"].splitlines()
        branch_line = lines[0] if lines and lines[0].startswith("##") else ""
        changes = [ln for ln in lines if ln and not ln.startswith("##")]
        return {"branch_line": branch_line, "dirty": bool(changes), "changes": changes}

    async def diff(self, path: str, *, staged: bool = False) -> str:
        args = ["diff"] + (["--staged"] if staged else [])
        return (await _run(args, cwd=path))["stdout"]

    # -------------------------------------------------------------- mutate

    async def checkout(self, path: str, branch: str) -> None:
        await _run(["checkout", branch], cwd=path)

    async def create_branch(self, path: str, branch: str, *, from_ref: Optional[str] = None) -> None:
        args = ["checkout", "-b", branch] + ([from_ref] if from_ref else [])
        await _run(args, cwd=path)

    async def commit_all(self, path: str, message: str) -> Dict[str, Any]:
        await _run(["add", "-A"], cwd=path)
        r = await _run(["commit", "-m", message], cwd=path)
        return {"output": r["stdout"]}

    async def push(self, path: str, *, branch: Optional[str] = None, token: Optional[str] = None,
                   set_upstream: bool = True) -> Dict[str, Any]:
        br = branch or await self.active_branch(path)
        args = ["push"]
        if set_upstream:
            args += ["--set-upstream", "origin", br or "HEAD"]
        else:
            args += ["origin", br or "HEAD"]
        r = await _run(args, cwd=path, token=token, timeout=300.0, redact=token)
        return {"output": r["stdout"] or r["stderr"]}

    async def fetch(self, path: str, *, token: Optional[str] = None) -> None:
        await _run(["fetch", "--all", "--prune"], cwd=path, token=token, timeout=300.0, redact=token)
