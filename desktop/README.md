# ND3X desktop app (Tauri shell + bundled backend)

A native window that launches the PyInstaller-frozen backend as a **sidecar**,
waits for it to come up, and points the window at the local UI it serves. The
backend stores its data under the OS app-data dir (`ND3X_HOME`).

```
desktop/
  dist/index.html          loading screen (Tauri frontendDist) shown until the backend is ready
  build_app.sh             one-shot build: FE → freeze backend (onefile) → stage sidecar → tauri build
  src-tauri/
    Cargo.toml, build.rs
    tauri.conf.json        window + bundle config; externalBin = the backend sidecar
    capabilities/default.json  permission to spawn the sidecar
    src/main.rs            spawn backend, wait for port 8765, navigate window, kill on exit
    binaries/              (generated) nd3x-backend-<target-triple>  ← staged by build_app.sh
    icons/                 (generated) app icons  ← create with `cargo tauri icon`
```

> ✅ **Built in CI** — the GitHub Actions *Desktop build* workflow
> (`.github/workflows/desktop-build.yml`) builds installers for macOS (arm64 +
> intel), Windows and Linux on `desktop-v*` tags (latest: **v0.5.4**) and attaches
> them to a GitHub Release. Use that as the source of truth for a known-good build;
> local `cargo tauri build` still needs the Rust toolchain + platform deps below.

## Prerequisites (install once)
- **Rust** — https://rustup.rs  (gives `cargo`, `rustc`)
- **Tauri CLI** — `cargo install tauri-cli --version "^2"`  (provides `cargo tauri …`)
- **Node/npm** — for the frontend build
- **Backend venv** — `ND3X/.venv` with runtime deps **and** PyInstaller
  (`ND3X/.venv/bin/pip install -r ND3X/packaging/requirements-build.txt`)
- Platform toolchain Tauri needs: Xcode CLT (macOS), WebView2 + MSVC (Windows),
  webkit2gtk/libsoup (Linux).

## Build
```bash
# 1. App icons (once) — any square PNG works
cd ND3X/desktop/src-tauri && cargo tauri icon /path/to/logo.png && cd -

# 2. Build everything (FE + backend sidecar + native bundle)
ND3X/desktop/build_app.sh
# → installers in ND3X/desktop/src-tauri/target/release/bundle/
```

## Dev loop
`cargo tauri dev` (from `desktop/src-tauri`) runs the shell, but it still needs the
sidecar staged first — run steps 1–3 of `build_app.sh` once (FE build, freeze
backend, copy sidecar), then `cargo tauri dev`.

## How it works
- `main.rs` spawns `nd3x-backend` with `ND3X_HOST=127.0.0.1`, `ND3X_PORT=8765`,
  `ND3X_HOME=<app-data>/nd3x`, streams its logs, waits for the port, then navigates
  the window to `http://127.0.0.1:8765/`. The child is killed when the window closes.
- The backend serves the built UI + `/api` from one origin (see
  `ND3X/src/server.py` `_mount_frontend`), so no separate frontend is bundled here.
- Bundled external binaries (ffmpeg/pandoc/…) — drop them in
  `ND3X/packaging/bin/<os>-<arch>/`; they're put on PATH at backend startup. See
  `ND3X/packaging/bin/README.md`. Missing ones degrade gracefully.

## Notes / likely tweaks
- **onefile cold start**: the sidecar is a PyInstaller *onefile* (`ND3X_ONEFILE=1`)
  so Tauri can treat it as a single external binary. It self-extracts on each
  launch (a few seconds). If that's too slow, switch to bundling the onedir folder
  as a Tauri resource and spawn it instead.
- **Port 8765** is hardcoded in `main.rs`; pick a free one / make it dynamic if it
  clashes.
- If the WebView refuses to navigate to the local http origin, relax it via the
  window/security config in `tauri.conf.json`.

## CI & releases (all OSes, fresh build on new versions)
`.github/workflows/desktop-build.yml` builds installers for macOS (arm64 + intel),
Windows (.exe/.msi) and Linux (AppImage/.deb) on GitHub-hosted runners — it checks
out this repo + the frontend repo as siblings and runs `build_app.sh` per OS.

**One-time setup (repo secrets):**
- `FE_REPO_TOKEN` on **ND3X** — PAT with read access to the frontend repo (to check it out).
- `DESKTOP_DISPATCH_TOKEN` on **lovely-landing-project** — PAT that can dispatch
  workflows on `nickozz714/ND3X` (for the FE→build trigger).

**Get a fresh build when the version changes:**
- **Backend (ND3X)** new version → `ND3X/desktop/release.sh 0.1.1` then
  `git push && git push origin desktop-v0.1.1`. The tag triggers the matrix build and
  attaches the installers to a GitHub Release.
- **Frontend** new version → tag it `v*`; `notify-desktop-build.yml` pings ND3X
  (`repository_dispatch: fe-release`) and the same matrix build runs with that FE ref.
- **Any time** → Actions ▸ *Desktop build* ▸ *Run workflow* (optional `fe_ref`).

Installers land as workflow **artifacts** (every run) and as **Release assets** (on a
`desktop-v*` tag).
