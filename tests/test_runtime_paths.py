"""Tests for the first-time-setup bootstrap layer (component/runtime_paths).

Config + secrets live under <base>/.nd3x/; only a pointer sits in ND3X_HOME.
"""
from __future__ import annotations

import json
import os
import stat

from component import runtime_paths


def test_unconfigured_when_no_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("ND3X_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ND3X_BASE_DIR", raising=False)
    assert runtime_paths.active_base_dir() is None
    assert runtime_paths.load_bootstrap() is None
    assert runtime_paths.is_configured() is False


def test_write_puts_pointer_in_home_and_config_under_base(tmp_path, monkeypatch):
    home = tmp_path / "home"
    base = tmp_path / "base"
    monkeypatch.setenv("ND3X_HOME", str(home))
    monkeypatch.delenv("ND3X_BASE_DIR", raising=False)

    runtime_paths.write_bootstrap(base_dir=str(base), database={"dialect": "sqlite"})

    assert (home / "pointer.json").is_file()
    assert (base / ".nd3x" / "bootstrap.json").is_file()
    assert runtime_paths.active_base_dir() == str(base)

    loaded = runtime_paths.load_bootstrap()
    assert loaded is not None
    assert loaded["base_dir"] == str(base)
    assert loaded["database"]["dialect"] == "sqlite"
    assert runtime_paths.is_configured() is True


def test_env_base_dir_overrides_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("ND3X_HOME", str(tmp_path / "home"))
    base = tmp_path / "envbase"
    monkeypatch.setenv("ND3X_BASE_DIR", str(base))
    assert runtime_paths.active_base_dir() == str(base)


def test_clear_bootstrap_reverts_to_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("ND3X_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ND3X_BASE_DIR", raising=False)
    runtime_paths.write_bootstrap(base_dir=str(tmp_path / "base"), database={"dialect": "sqlite"})
    assert runtime_paths.is_configured() is True
    runtime_paths.clear_bootstrap()  # removes the pointer
    assert runtime_paths.active_base_dir() is None
    assert runtime_paths.is_configured() is False


def test_secrets_generated_under_base_and_chmod_600(tmp_path, monkeypatch):
    monkeypatch.setenv("ND3X_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ND3X_BASE_DIR", raising=False)
    base = tmp_path / "base"
    runtime_paths.write_bootstrap(base_dir=str(base), database={"dialect": "sqlite"})

    secrets = runtime_paths.load_or_create_secrets(generate=True)
    assert secrets["JWT_SECRET"]
    assert secrets["MAIL_SECRET_KEY"]
    assert secrets["SETTINGS_ENCRYPTION_KEY"]

    from cryptography.fernet import Fernet
    Fernet(secrets["MAIL_SECRET_KEY"].encode())  # valid Fernet key

    secrets_file = base / ".nd3x" / "secrets.json"
    assert secrets_file.is_file()
    assert stat.S_IMODE(os.stat(secrets_file).st_mode) == 0o600


def test_secrets_stable_across_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("ND3X_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ND3X_BASE_DIR", raising=False)
    base = str(tmp_path / "base")
    runtime_paths.write_bootstrap(base_dir=base, database={"dialect": "sqlite"})
    first = runtime_paths.load_or_create_secrets(generate=True)
    second = runtime_paths.load_or_create_secrets(generate=True)
    assert first == second
    assert runtime_paths.load_secrets()["JWT_SECRET"] == first["JWT_SECRET"]


def test_load_secrets_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("ND3X_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ND3X_BASE_DIR", raising=False)
    # No base dir at all → no config dir → empty.
    assert runtime_paths.load_secrets() == {}


def test_resolve_roots_default_db_name_is_ND3X(tmp_path):
    base = str(tmp_path / "data" / "nd3x")
    roots = runtime_paths.resolve_roots(base)
    assert roots["base_dir"] == base
    assert roots["db_path"] == os.path.join(base, "db", "ND3X.db")
    assert roots["log_file"] == os.path.join(base, "logs", "app.log")
    assert roots["files_dir"] == os.path.join(base, "files")
    assert roots["ask_root"] == os.path.join(base, "ask")
    assert roots["voice_root"] == os.path.join(base, "voice")


def test_bootstrap_ignored_without_base_dir_field(tmp_path, monkeypatch):
    home = tmp_path / "home"
    base = tmp_path / "base"
    monkeypatch.setenv("ND3X_HOME", str(home))
    monkeypatch.delenv("ND3X_BASE_DIR", raising=False)
    # Point at base, but write a malformed bootstrap with no base_dir.
    (home).mkdir(parents=True, exist_ok=True)
    (home / "pointer.json").write_text(json.dumps({"base_dir": str(base)}), encoding="utf-8")
    cfg = base / ".nd3x"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "bootstrap.json").write_text(json.dumps({"database": {}}), encoding="utf-8")
    assert runtime_paths.load_bootstrap() is None
