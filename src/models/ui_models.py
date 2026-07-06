from typing import Optional, Dict, Any

from pydantic import BaseModel, Field


class IngestBody(BaseModel):
    content: str
    title: Optional[str] = None
    subdir: str = "inbox"
    is_code: bool = False
    language: Optional[str] = None
    async_mode: Optional[bool] = None

class IngestStatusBody(BaseModel):
    job_id: str


class TextUpdateRequest(BaseModel):
    doc_id: int
    new_content: str = Field(..., min_length=1)


class TextDeleteRequest(BaseModel):
    doc_id: int
    delete_file: bool = True


# -----------------------------
# NEW: Repo / Code-session UI helpers
# -----------------------------
class RepoListRequest(BaseModel):
    # intentionally empty; kept for symmetry if you later add filters
    pass


class RepoFilesRequest(BaseModel):
    repo_name: str
    # optional path prefix (folder) filter if your MCP tool supports it (safe to ignore)
    prefix: Optional[str] = None


class RepoFileReadRequest(BaseModel):
    repo_name: str
    path: str
    max_bytes: int = 5_000_000


class RepoApplyOneFileRequest(BaseModel):
    """
    UI: save edits immediately via a single-file commit.
    Uses MCP tool repo_apply_changes under the hood.
    """
    repo_name: str
    thread_id: str
    path: str
    new_content: str = Field(..., min_length=1)
    expected_blob_sha: Optional[str] = None
    commit_message: Optional[str] = None

class MarkdownToPdfRequest(BaseModel):
    markdown_string: str = Field(..., min_length=1)
    template: str = "Beeminds"
    properties: Optional[Dict[str, Any]] = None

class PdfByIdRequest(BaseModel):
    doc_id: str = Field(..., min_length=1)