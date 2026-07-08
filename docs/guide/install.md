# Installing ND3X

Two ways to run ND3X: the **desktop app** (native installers) or **Docker**.

## Desktop app

Download the installer for your OS from the
[latest release](https://github.com/nickozz714/ND3X/releases/latest):

- **macOS (Apple Silicon):** `ND3X_*_aarch64.dmg` — Intel Macs run it via Rosetta.
- **Windows:** `ND3X_*_x64-setup.exe` (or the `.msi`).
- **Linux:** `ND3X_*_amd64.AppImage` or `.deb`.

### macOS: “‘ND3X’ is damaged and can’t be opened”

The macOS builds aren’t signed with an Apple Developer ID yet, so Gatekeeper flags
them as *damaged*. **The file is fine — it’s just unsigned.** To run it:

1. Drag **ND3X.app** into **Applications**.
2. In **Terminal**, clear the quarantine flag:
   ```bash
   xattr -cr /Applications/ND3X.app
   ```
3. Open it (double-click, or right-click → **Open**).

If it still won’t open, ad-hoc re-sign it and try again:
```bash
codesign --force --deep --sign - /Applications/ND3X.app
```

> This is only needed for the **downloaded** app. A future signed & notarized build
> will remove the step.

## Docker

```bash
curl -LO https://github.com/nickozz714/ND3X/releases/latest/download/docker-compose.release.yml
TAG=0.1.0 docker compose -f docker-compose.release.yml up -d
# then open http://localhost:8080
```

Images (GHCR): `ghcr.io/nickozz714/nd3x-backend` · `ghcr.io/nickozz714/nd3x-frontend`.
