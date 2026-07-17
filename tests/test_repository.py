"""Tests for the repository hub: GitService against a real local git repo (no
network), URL parsing, and RepositoryService registry logic."""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.repository as rm
from services.git_service import GitService
from services.repository_service import RepositoryService, _owner_repo, _safe_name


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    rm.Repository.__table__.create(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def local_repo(tmp_path):
    """A real local git repo with one commit on the default branch."""
    path = str(tmp_path / "work")
    os.makedirs(path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=path, check=True, env=env)
    with open(os.path.join(path, "README.md"), "w") as f:
        f.write("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)
    return path


# ----------------------------------------------------------------- helpers


def test_safe_name():
    assert _safe_name("my repo!") == "my-repo"
    assert _safe_name("../etc/passwd") == "etc-passwd"
    assert _safe_name("") == "repo"


def test_owner_repo_parsing():
    assert _owner_repo("https://github.com/nickozz714/ND3X.git") == ("nickozz714", "ND3X")
    assert _owner_repo("https://github.com/nickozz714/ND3X") == ("nickozz714", "ND3X")
    assert _owner_repo("git@github.com:nickozz714/ND3X.git") == ("nickozz714", "ND3X")
    assert _owner_repo("https://example.com/x/y") is None


# ------------------------------------------------------------- GitService


def test_git_branches_active_status_diff(local_repo):
    g = GitService()
    assert asyncio.run(g.active_branch(local_repo)) == "main"
    br = asyncio.run(g.branches(local_repo))
    assert br["active"] == "main" and "main" in br["local"]
    st = asyncio.run(g.status(local_repo))
    assert st["dirty"] is False
    # Make a change → dirty + diff shows it.
    with open(os.path.join(local_repo, "README.md"), "a") as f:
        f.write("more\n")
    st2 = asyncio.run(g.status(local_repo))
    assert st2["dirty"] is True
    assert "README.md" in asyncio.run(g.diff(local_repo))


def test_git_create_branch_checkout_commit(local_repo):
    g = GitService()
    asyncio.run(g.create_branch(local_repo, "feature/x"))
    assert asyncio.run(g.active_branch(local_repo)) == "feature/x"
    with open(os.path.join(local_repo, "f.txt"), "w") as f:
        f.write("x\n")
    asyncio.run(g.commit_all(local_repo, "add f"))
    assert asyncio.run(g.status(local_repo))["dirty"] is False
    asyncio.run(g.checkout(local_repo, "main"))
    assert asyncio.run(g.active_branch(local_repo)) == "main"
    assert not os.path.exists(os.path.join(local_repo, "f.txt"))  # only on feature branch


# --------------------------------------------------------- RepositoryService


def test_register_and_dedupe(db):
    svc = RepositoryService(db)
    r = svc.register(name="ND3X", remote_url="https://github.com/nickozz714/ND3X.git")
    assert r.clone_status == "registered" and r.name == "ND3X"
    with pytest.raises(ValueError, match="already registered"):
        svc.register(name="ND3X", remote_url="https://github.com/x/y")


def test_service_branch_ops_on_registered_repo(db, local_repo):
    svc = RepositoryService(db)
    r = svc.register(name="local", remote_url="https://github.com/o/local.git")
    # Simulate a completed clone by pointing local_path at the real repo.
    r.local_path = local_repo
    r.clone_status = "ready"
    db.commit()
    br = asyncio.run(svc.create_branch(r.id, "feature/y"))
    assert br.active_branch == "feature/y"
    assert "feature/y" in asyncio.run(svc.branches(r.id))["local"]
    # Not-cloned repo raises a clear error.
    r2 = svc.register(name="notcloned", remote_url="https://github.com/o/n.git")
    with pytest.raises(ValueError, match="not cloned"):
        asyncio.run(svc.status(r2.id))
