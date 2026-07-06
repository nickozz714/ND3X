#!/usr/bin/env bash
# Build the ND3X desktop backend: build the frontend, stage it into src/web, then
# freeze the backend with PyInstaller. Output: ND3X/dist/nd3x-backend/ (onedir).
#
# Prereqs: the backend venv (ND3X/.venv) with deps + pyinstaller, and Node/npm for
# the frontend. Run from anywhere.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ND3X="$(cd "$HERE/.." && pwd)"
ROOT="$(cd "$ND3X/.." && pwd)"
FE="$ROOT/lovely-landing-project"
PY="$ND3X/.venv/bin/python"

echo "==> 1/3 Build frontend (vite)"
( cd "$FE" && npm run build )

echo "==> 2/3 Stage built frontend into src/web"
rm -rf "$ND3X/src/web"
cp -r "$FE/dist" "$ND3X/src/web"

echo "==> 3/4 Freeze backend (PyInstaller)"
cd "$ND3X"
"$PY" -m PyInstaller --noconfirm --clean packaging/nd3x.spec

echo "==> 4/4 Stage bundled external binaries (optional)"
# Drop platform binaries (ffmpeg, pandoc, pdftoppm/pdftotext, …) in
# packaging/bin/<os>-<arch>/ to ship them; they get put on PATH at startup.
# Missing binaries degrade gracefully (features report "not installed").
OSARCH="$(uname -s | tr 'A-Z' 'a-z')-$(uname -m)"
BIN_SRC="$ND3X/packaging/bin/$OSARCH"
BIN_DST="$ND3X/dist/nd3x-backend/bin"
if [ -d "$BIN_SRC" ]; then
  mkdir -p "$BIN_DST" && cp -R "$BIN_SRC"/* "$BIN_DST"/ && chmod +x "$BIN_DST"/* 2>/dev/null || true
  echo "    bundled binaries from $BIN_SRC -> $BIN_DST"
else
  echo "    (no packaging/bin/$OSARCH — relying on system PATH; heavy features degrade if missing)"
fi

echo "==> Done: $ND3X/dist/nd3x-backend/nd3x-backend"
echo "    Smoke test:  ND3X_PORT=8090 \"$ND3X/dist/nd3x-backend/nd3x-backend\"  then open http://127.0.0.1:8090/"
