"""
services/builtin/tools/repo_tools.py

GitHub repository hub as always-on builtin tools. The agent uses these to see
registered repos, clone them, list/switch/create branches, inspect status/diff,
commit, push a feature branch, and open a PR. Actual code editing happens with
the agent's file tools (or the Claude Code CLI running in the repo as its
workdir); these tools manage the repo + git lifecycle around those edits.

Registered on import (see ask_job_callbacks). Engine-agnostic: the orchestrator
calls these directly; the Claude Code engine reaches them via the mcp__nd3x
gateway.
"""
from __future__ import annotations

from typing import Any, Dict

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)


def _svc(db):
    from services.repository_service import RepositoryService
    return RepositoryService(db)


async def _resolve(db, args: Dict[str, Any]):
    """Resolve a repo by id or name from tool args."""
    svc = _svc(db)
    rid = args.get("id") or args.get("repo_id")
    if rid is not None:
        return svc, svc.get(int(rid))
    name = args.get("name") or args.get("repo")
    if name:
        return svc, svc.get_by_name(str(name))
    return svc, None


@internal_tool_registry.register(
    name="repo__list",
    title="List Repositories",
    description="List the GitHub repositories registered in ND3X — id, name, remote URL, active branch, clone status and local path.",
    input_schema={"type": "object", "properties": {}},
    tags=["internal", "repo"],
)
async def repo_list(_args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    with SessionLocal() as db:
        svc = _svc(db)
        return {"status": "success", "repositories": [svc.to_dict(r) for r in svc.list()]}


@internal_tool_registry.register(
    name="repo__register",
    title="Register Repository",
    description=(
        "Register a GitHub repository so ND3X tracks it. Give a name and the "
        "remote URL. Optionally set credential_secret — the name of a stored "
        "secret holding a GitHub PAT — needed to clone private repos, push, or "
        "open PRs. Registering does not clone; call repo__clone next."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "remote_url": {"type": "string", "description": "https://github.com/owner/repo(.git)"},
            "credential_secret": {"type": "string", "description": "Name of the stored secret with the GitHub PAT."},
        },
        "required": ["name", "remote_url"],
    },
    tags=["internal", "repo"],
)
async def repo_register(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc = _svc(db)
        try:
            r = svc.register(name=a["name"], remote_url=a["remote_url"],
                             credential_secret=a.get("credential_secret"))
        except (ValueError, KeyError) as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "success", "repository": svc.to_dict(r)}


@internal_tool_registry.register(
    name="repo__clone",
    title="Clone Repository",
    description="Clone a registered repository into ND3X's managed repos directory. Uses its credential secret for private repos. Identify by id or name.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    },
    tags=["internal", "repo"],
)
async def repo_clone(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    with SessionLocal() as db:
        svc, repo = await _resolve(db, args or {})
        if repo is None:
            return {"status": "error", "error": "Repository not found (pass id or name)."}
        try:
            r = await svc.clone(repo.id)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}
        return {"status": "success", "repository": svc.to_dict(r)}


@internal_tool_registry.register(
    name="repo__branches",
    title="List Branches",
    description="List local and remote branches of a cloned repository, and which is active.",
    input_schema={"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}},
    tags=["internal", "repo"],
)
async def repo_branches(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    with SessionLocal() as db:
        svc, repo = await _resolve(db, args or {})
        if repo is None:
            return {"status": "error", "error": "Repository not found."}
        try:
            return {"status": "success", **(await svc.branches(repo.id))}
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}


@internal_tool_registry.register(
    name="repo__create_branch",
    title="Create Feature Branch",
    description="Create and switch to a new branch in a cloned repo (e.g. a feature branch). Optionally branch from a specific ref (default: current HEAD).",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"},
            "branch": {"type": "string"}, "from_ref": {"type": "string"},
        },
        "required": ["branch"],
    },
    tags=["internal", "repo"],
)
async def repo_create_branch(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc, repo = await _resolve(db, a)
        if repo is None:
            return {"status": "error", "error": "Repository not found."}
        try:
            r = await svc.create_branch(repo.id, str(a["branch"]), from_ref=a.get("from_ref"))
        except (ValueError, KeyError) as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "success", "repository": svc.to_dict(r)}


@internal_tool_registry.register(
    name="repo__checkout",
    title="Switch Branch",
    description="Check out (switch to) an existing branch in a cloned repository.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "branch": {"type": "string"}},
        "required": ["branch"],
    },
    tags=["internal", "repo"],
)
async def repo_checkout(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc, repo = await _resolve(db, a)
        if repo is None:
            return {"status": "error", "error": "Repository not found."}
        try:
            r = await svc.checkout(repo.id, str(a["branch"]))
        except (ValueError, KeyError) as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "success", "repository": svc.to_dict(r)}


@internal_tool_registry.register(
    name="repo__status",
    title="Repository Status",
    description="Show git status (dirty/clean + changed files) and, with diff=true, the unified diff of the working tree.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "diff": {"type": "boolean"}},
    },
    tags=["internal", "repo"],
)
async def repo_status(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc, repo = await _resolve(db, a)
        if repo is None:
            return {"status": "error", "error": "Repository not found."}
        try:
            out = {"status": "success", **(await svc.status(repo.id))}
            if a.get("diff"):
                out["diff"] = (await svc.diff(repo.id))[:20000]
            return out
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}


@internal_tool_registry.register(
    name="repo__commit",
    title="Commit Changes",
    description="Stage all changes and commit them in a cloned repo with the given message. Make your code edits first (with your file tools).",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "message": {"type": "string"}},
        "required": ["message"],
    },
    tags=["internal", "repo"],
)
async def repo_commit(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc, repo = await _resolve(db, a)
        if repo is None:
            return {"status": "error", "error": "Repository not found."}
        try:
            return {"status": "success", **(await svc.commit(repo.id, str(a["message"])))}
        except (ValueError, KeyError) as exc:
            return {"status": "error", "error": str(exc)}


@internal_tool_registry.register(
    name="repo__push",
    title="Push Branch",
    description="Push the current (or given) branch to origin, setting upstream. Uses the repo's credential secret. Push a feature branch before opening a PR.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "branch": {"type": "string"}},
    },
    tags=["internal", "repo"],
)
async def repo_push(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc, repo = await _resolve(db, a)
        if repo is None:
            return {"status": "error", "error": "Repository not found."}
        try:
            return {"status": "success", **(await svc.push(repo.id, branch=a.get("branch")))}
        except (ValueError, Exception) as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}


@internal_tool_registry.register(
    name="repo__open_pr",
    title="Open Pull Request",
    description=(
        "Open a GitHub Pull Request from a pushed branch. Needs the repo's "
        "credential secret (PAT). head defaults to the active branch, base to "
        "the default branch. Push the branch first (repo__push)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"},
            "title": {"type": "string"}, "body": {"type": "string"},
            "head": {"type": "string"}, "base": {"type": "string"},
        },
        "required": ["title"],
    },
    tags=["internal", "repo"],
)
async def repo_open_pr(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc, repo = await _resolve(db, a)
        if repo is None:
            return {"status": "error", "error": "Repository not found."}
        try:
            pr = await svc.open_pull_request(repo.id, title=str(a["title"]), head=a.get("head"),
                                             base=a.get("base"), body=a.get("body") or "")
            return {"status": "success", "pull_request": pr}
        except (ValueError, KeyError) as exc:
            return {"status": "error", "error": str(exc)}
