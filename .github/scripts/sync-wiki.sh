#!/usr/bin/env bash
# Mirror docs/guide/*.md into a GitHub wiki working copy.
#
#   bash .github/scripts/sync-wiki.sh <wiki-dir>
#
# Run from the repository root. Converts the guide's relative .md links into
# wiki page links (README.md -> Home, ../PLATFORM.md -> repo URL) and writes a
# curated _Sidebar.md. Used by .github/workflows/wiki-sync.yml.
set -euo pipefail

GUIDE="docs/guide"
WIKI="${1:?usage: sync-wiki.sh <wiki-dir>}"
REPO_DOCS="https://github.com/nickozz714/ND3X/blob/main/docs"

shopt -s nullglob
for f in "$GUIDE"/*.md; do
  base="$(basename "$f")"
  case "$base" in _*) continue ;; esac      # skip _Sidebar.md etc.
  page="$base"; [ "$base" = "README.md" ] && page="Home.md"
  sed -E \
    -e "s#\]\(\.\./PLATFORM\.md\)#](${REPO_DOCS}/PLATFORM.md)#g" \
    -e 's#\]\(README\.md\)#](Home)#g' \
    -e 's#\]\(([a-z][a-z-]+)\.md\)#](\1)#g' \
    "$f" > "$WIKI/$page"
done

cat > "$WIKI/_Sidebar.md" <<'EOF'
### ND3X Guide

- [Home](Home)
- [How ND3X works](how-it-works)

**AI Workbench**
- [Agent Settings](agent)
- [Skills](skills)
- [Tools](tools)
- [MCP Servers](mcp-servers)
- [Workflows](workflows)
- [AI Models](ai-models)
- [Fabric Data Agents](fabric-data-agents)
- [Builtins & System](builtins-system)
- [Meeting Profiles](meeting-profiles)
- [Slash Commands](slash-commands)
- [Usage](usage)
- [Users](users)

**Platform**
- [Tiles](platform-tiles)
EOF
