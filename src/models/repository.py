"""
models/repository.py

Registry of GitHub repositories ND3X knows about: which are registered/cloned,
where they live locally, and which branch is active. Branch listings are read
LIVE from git (git is the source of truth); this table holds the registration,
the clone location, the active branch, and the credential reference.

Both the agent (Claude Code / orchestrator, via repo__* tools + the repo as a
Claude Code workdir) and the user (repo GUI tile) work with these.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from db.database import Base


# clone_status: registered (known, not cloned) | cloning | ready | error
REPO_STATUSES = ("registered", "cloning", "ready", "error")


class Repository(Base):
    __tablename__ = "repository"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    remote_url = Column(String(1024), nullable=False)
    # Absolute local path of the working copy (under settings.REPOS_DIR).
    local_path = Column(String(1024), nullable=True)
    default_branch = Column(String(255), nullable=True)
    # The branch currently checked out (updated on checkout/create_branch).
    active_branch = Column(String(255), nullable=True)
    # Name of the Secret (SecretService) holding the GitHub PAT for clone/push.
    credential_secret = Column(String(255), nullable=True)
    clone_status = Column(String(32), nullable=False, default="registered")
    last_error = Column(Text, nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
