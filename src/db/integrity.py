"""Referential-integrity checks for the skill/tool association tables.

A real data-integrity bug once left dangling link rows in `assistant_skill` /
`skill_tool` / `assistant_tool` after Skills/Tools were deleted (assistants then
pointed at non-existent skills and the router presented them empty). The delete
paths now cascade-remove their link rows (see `ToolRepository.delete` /
`SkillRepository.delete`), and this module provides a lightweight check — the
same `LEFT JOIN ... WHERE child IS NULL` pattern used to clean the dev DB — so a
regression is caught by a test and/or surfaced as a startup log line.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)

# (link table, child column, parent table) — every link row must reference an
# existing parent on both sides; a NULL parent id after a LEFT JOIN is dangling.
_DANGLING_CHECKS: list[tuple[str, str, str]] = [
    ("assistant_skill", "assistant_id", "assistant"),
    ("assistant_skill", "skill_id", "skills"),
    ("skill_tool", "skill_id", "skills"),
    ("skill_tool", "tool_id", "tool"),
    ("assistant_tool", "assistant_id", "assistant"),
    ("assistant_tool", "tool_id", "tool"),
]


def find_dangling_links(db: Session) -> dict[str, int]:
    """Return a mapping of "<link_table>.<column>" -> count of dangling rows.

    Only keys with a non-zero count are included; an empty dict means the
    association tables are clean.
    """

    dangling: dict[str, int] = {}
    parent_pk = {"skills": "id", "tool": "id", "assistant": "id"}

    for link_table, child_col, parent_table in _DANGLING_CHECKS:
        pk = parent_pk[parent_table]
        sql = text(
            f"SELECT COUNT(*) FROM {link_table} AS link "
            f"LEFT JOIN {parent_table} AS parent "
            f"ON link.{child_col} = parent.{pk} "
            f"WHERE parent.{pk} IS NULL"
        )
        count = db.execute(sql).scalar() or 0
        if count:
            dangling[f"{link_table}.{child_col}"] = int(count)

    return dangling


def log_dangling_links(db: Session) -> dict[str, int]:
    """Run the dangling-link check and log the outcome. Returns the findings."""

    try:
        dangling = find_dangling_links(db)
    except Exception:  # pragma: no cover - never fail startup on a check
        log.warningx("Dangling-link integrity check failed to run")
        return {}

    if dangling:
        log.warningx("Dangling association rows detected", **dangling)
    else:
        log.infox("Association tables clean (no dangling links)")
    return dangling
