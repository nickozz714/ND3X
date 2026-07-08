#!/usr/bin/env bash
# Build the ND3X native desktop app (Tauri shell + bundled backend sidecar).
#
# Prereqs (install once):
#   - Rust:        https://rustup.rs   (rustup → cargo, rustc)
#   - Tauri CLI:   cargo install tauri-cli --version "^2"
#   - Node/npm     (frontend build) and the backend venv (ND3X/.venv with deps + pyinstaller)
#   - Icons:       cargo tauri icon path/to/logo.png   (generates src-tauri/icons/, required by `build`)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # ND3X/desktop
ND3X="$(cd "$HERE/.." && pwd)"                          # ND3X
ROOT="$(cd "$ND3X/.." && pwd)"                          # workspace root
FE="$ROOT/lovely-landing-project"
# venv python: POSIX (.venv/bin) or Windows (.venv/Scripts)
PY="$ND3X/.venv/bin/python"; [ -x "$PY" ] || PY="$ND3X/.venv/Scripts/python.exe"

command -v rustc >/dev/null || { echo "Rust not installed — see https://rustup.rs"; exit 1; }

echo "==> 1/4 Build frontend"
( cd "$FE" && npm run build )
rm -rf "$ND3X/src/web" && cp -r "$FE/dist" "$ND3X/src/web"

echo "==> 2/4 Freeze backend (PyInstaller, onefile for the sidecar)"
( cd "$ND3X" && ND3X_ONEFILE=1 "$PY" -m PyInstaller --noconfirm --clean packaging/nd3x.spec )

echo "==> 3/4 Stage sidecar with the Rust target-triple suffix Tauri expects"
TRIPLE="$(rustc -Vv | sed -n 's/^host: //p')"
EXT=""; case "$TRIPLE" in *windows*) EXT=".exe";; esac
mkdir -p "$HERE/src-tauri/binaries"
cp "$ND3X/dist/nd3x-backend$EXT" "$HERE/src-tauri/binaries/nd3x-backend-$TRIPLE$EXT"
chmod +x "$HERE/src-tauri/binaries/nd3x-backend-$TRIPLE$EXT" 2>/dev/null || true
echo "    sidecar: src-tauri/binaries/nd3x-backend-$TRIPLE$EXT"

echo "==> 4/4 Build Tauri app"
# CI=true makes Tauri's DMG step skip the Finder/AppleScript window styling, which
# fails in headless/non-GUI sessions. Override with CI= for the prettier layout in
# an interactive session.
( cd "$HERE/src-tauri" && CI="${CI:-true}" cargo tauri build )

# macOS: the bundled backend is a PyInstaller binary that, at launch, extracts and
# loads its own Python.framework (a different Team ID). Under Hardened Runtime,
# library validation rejects that and the backend is killed on start. Re-sign the
# sidecar + app with an entitlement that disables library validation, then rebuild
# the dmg from the fixed app so the installer carries it. (Belt-and-suspenders on
# top of tauri.conf's entitlements, which may not reach the sidecar.)
if [[ "$TRIPLE" == *apple-darwin* ]]; then
  APP="$(ls -d "$HERE"/src-tauri/target/release/bundle/macos/*.app 2>/dev/null | head -1)"
  ENT="$HERE/src-tauri/entitlements.plist"
  if [ -n "$APP" ] && [ -f "$ENT" ]; then
    echo "==> macOS: re-sign sidecar + app with library-validation entitlement"
    codesign --force --sign - --entitlements "$ENT" --options runtime "$APP/Contents/MacOS/nd3x-backend"
    codesign --force --sign - --entitlements "$ENT" --options runtime "$APP"
    DMG="$(ls "$HERE"/src-tauri/target/release/bundle/dmg/*.dmg 2>/dev/null | head -1)"
    if [ -n "$DMG" ]; then
      echo "==> macOS: rebuild dmg from the re-signed app ($(basename "$DMG"))"
      STAGING="$(mktemp -d)"
      cp -R "$APP" "$STAGING/"
      ln -s /Applications "$STAGING/Applications"
      rm -f "$DMG"
      hdiutil create -volname "ND3X" -srcfolder "$STAGING" -ov -format UDZO "$DMG" >/dev/null
      rm -rf "$STAGING"
    fi
  fi
fi
echo "==> Done — installers in desktop/src-tauri/target/release/bundle/"
