#!/usr/bin/env bash
# Cut a desktop release: bump the app version, commit, tag `desktop-v<version>`,
# and push — which triggers the Desktop build CI to produce installers for all OSes.
#
#   ND3X/desktop/release.sh 0.1.1
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # ND3X/desktop
ND3X="$(cd "$HERE/.." && pwd)"
CONF="$HERE/src-tauri/tauri.conf.json"

VERSION="${1:?usage: release.sh <version>   e.g. 0.1.1}"

python3 - "$CONF" "$VERSION" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
data = json.load(open(path))
data["version"] = version
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"tauri.conf.json version -> {version}")
PY

cd "$ND3X"
git add "$CONF"
git commit -m "desktop v$VERSION"
git tag "desktop-v$VERSION"
echo "Committed + tagged desktop-v$VERSION."
echo "Push to trigger the build:  git push && git push origin desktop-v$VERSION"
