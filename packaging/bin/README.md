# Bundled external binaries (optional)

Drop platform binaries here to ship them with the desktop app:

    packaging/bin/<os>-<arch>/   e.g. darwin-arm64/, linux-x86_64/, windows-amd64/
      ffmpeg, pandoc, pdftoppm, pdftotext, ...

`build_desktop.sh` copies the matching `<os>-<arch>/` into the bundle's `bin/`,
and the backend prepends that dir to PATH at startup (see
`component/runtime_binaries.py`), so all `shutil.which()` / subprocess calls find
them. Anything not bundled falls back to the system PATH; if still missing, the
dependent feature degrades with a clear "not installed" message rather than
crashing (e.g. PDF/LaTeX, audio, mermaid). chromium/texlive are large — leave
them to the system or a system-Chrome path unless you specifically need them.
