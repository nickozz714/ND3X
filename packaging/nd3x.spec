# PyInstaller spec for the ND3X backend (desktop build).
# Build via packaging/build_desktop.sh (which first builds the FE into src/web).
#
# Run from the ND3X repo root:  pyinstaller packaging/nd3x.spec
import os
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

# SPECPATH = the directory containing this spec (…/ND3X/packaging).
ROOT = os.path.dirname(SPECPATH)          # …/ND3X
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)                   # so collect_submodules/collect_data_files resolve app packages

# The app does a LOT of dynamic/lazy imports (routers/__init__, lifespan, the
# internal tool registry, models in init_db, services). Pull whole packages in so
# PyInstaller doesn't miss them.
hiddenimports = []
for pkg in ("routers", "services", "models", "schemas", "repository",
            "component", "db", "authentication", "utils"):
    hiddenimports += collect_submodules(pkg)
# passlib loads its hash handlers dynamically (argon2/bcrypt) — pull them all in.
hiddenimports += collect_submodules("passlib")
# Native/edge deps that hooks sometimes miss.
hiddenimports += ["faiss", "pymysql", "argon2", "email_validator",
                  "uvicorn.logging", "uvicorn.loops.auto",
                  "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
                  "uvicorn.lifespan.on", "anthropic", "google.genai"]

# Runtime data files (read by the app via __file__-relative paths) — destinations
# mirror the package layout so those reads resolve inside the bundle.
datas = [
    (os.path.join(SRC, "web"), "web"),  # built frontend (staged by build_desktop.sh)
    (os.path.join(SRC, "services", "assistants", "runtime", "system_specs"),
     os.path.join("services", "assistants", "runtime", "system_specs")),
    (os.path.join(SRC, "templates"), "templates"),
    (os.path.join(SRC, "server.py"), "."),  # setup_router reads server.py
]
# Best-effort: also collect any package data files (.md/.json) the app ships.
for pkg in ("services", "component"):
    datas += collect_data_files(pkg, includes=["**/*.md", "**/*.json", "**/*.j2", "**/*.txt", "**/*.html"])
# Packages that read their own dist metadata via importlib.metadata at import time.
for pkg in ("fastmcp", "openai", "anthropic", "mcp", "google-genai", "faiss-cpu"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(SPECPATH, "run_server.py")],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# ND3X_ONEFILE=1 → a single-file binary (used as the Tauri sidecar). Default is
# onedir (faster cold start; good for `dist/nd3x-backend/nd3x-backend` direct use).
if os.environ.get("ND3X_ONEFILE"):
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="nd3x-backend", console=True,
    )
else:
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True,
        name="nd3x-backend", console=True,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        name="nd3x-backend",  # → dist/nd3x-backend/ (onedir)
    )
