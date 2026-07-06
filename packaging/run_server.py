"""Frozen-app entry point for the ND3X desktop build.

Runs the FastAPI app under uvicorn WITHOUT reload/import-string (neither works in a
PyInstaller bundle) and with a single worker (no multiprocessing fork). The Tauri
shell sets HOST/PORT/ND3X_HOME via env before launching this binary.
"""
from __future__ import annotations

import multiprocessing
import os


def main() -> None:
    multiprocessing.freeze_support()  # required for frozen apps that touch multiprocessing
    import uvicorn
    from component.config import settings
    from server import app  # building create_app() mounts API + the bundled web/ UI

    host = os.environ.get("ND3X_HOST") or getattr(settings, "HOST", "127.0.0.1") or "127.0.0.1"
    port = int(os.environ.get("ND3X_PORT") or getattr(settings, "PORT", 8088) or 8088)
    uvicorn.run(app, host=host, port=port, reload=False, workers=1, log_level="info")


if __name__ == "__main__":
    main()
