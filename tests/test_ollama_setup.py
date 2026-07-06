"""Unit tests for OllamaSetupService (detect / start / install) — injected
which/run/spawn, no real processes."""
from __future__ import annotations

from services.local_models.ollama_setup import OllamaSetupService


def make(system, *, ollama=True, brew=False, run_rc=0, run_out="ollama version 0.5.0"):
    spawned = []

    def which(name):
        if name == "ollama" and ollama:
            return "/usr/local/bin/ollama"
        if name == "brew" and brew:
            return "/opt/homebrew/bin/brew"
        return None

    def run(cmd, timeout=30):
        return (run_rc, run_out, "" if run_rc == 0 else "boom error")

    def spawn(cmd):
        spawned.append(cmd)

    return OllamaSetupService(system=system, which=which, run=run, spawn=spawn), spawned


def test_detect_installed_with_version():
    svc, _ = make("Darwin", run_out="ollama version 0.5.0")
    d = svc.detect()
    assert d["installed"] is True and d["version"] == "ollama version 0.5.0"
    assert d["binary_path"].endswith("ollama")


def test_detect_not_installed():
    svc, _ = make("Darwin", ollama=False)
    d = svc.detect()
    assert d["installed"] is False and d["binary_path"] is None and d["version"] is None


def test_can_manage_local_only():
    assert OllamaSetupService.can_manage("http://localhost:11434") is True
    assert OllamaSetupService.can_manage("http://127.0.0.1:11434") is True
    assert OllamaSetupService.can_manage("http://host.docker.internal:11434") is False
    assert OllamaSetupService.can_manage("http://10.0.0.5:11434") is False


def test_start_spawns_serve_when_installed():
    svc, spawned = make("Darwin", ollama=True)
    r = svc.start()
    assert r["ok"] is True and spawned == [["ollama", "serve"]]


def test_start_refuses_when_not_installed():
    svc, spawned = make("Darwin", ollama=False)
    r = svc.start()
    assert r["ok"] is False and spawned == []


def test_install_uses_brew_on_mac_when_present():
    svc, _ = make("Darwin", brew=True, run_rc=0)
    r = svc.install()
    assert r["ok"] is True and r["command"] == "brew install ollama"


def test_install_falls_back_to_curl_script():
    svc, _ = make("Darwin", brew=False, run_rc=0)
    r = svc.install()
    assert r["ok"] is True and "install.sh" in r["command"]


def test_install_linux_uses_curl_script():
    svc, _ = make("Linux", run_rc=0)
    r = svc.install()
    assert "install.sh" in r["command"]


def test_install_failure_surfaces_output():
    svc, _ = make("Linux", run_rc=1, run_out="partial log")
    r = svc.install()
    assert r["ok"] is False and "boom error" in r["output"]


def test_install_unsupported_os():
    svc, _ = make("Windows")
    r = svc.install()
    assert r["ok"] is False and "not supported" in r["message"]
