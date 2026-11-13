import sys
from types import SimpleNamespace

import pytest

from backend import __main__ as entry


def test_public_server_uses_isolated_application_without_admin_login(monkeypatch):
    monkeypatch.setenv("FRAMEKEEP_HOST", "0.0.0.0")
    monkeypatch.setenv("FRAMEKEEP_PUBLIC_MODE", "1")
    monkeypatch.delenv("FRAMEKEEP_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(entry, "load_dotenv", lambda: None)
    public = object()
    monkeypatch.setitem(sys.modules, "backend.public", SimpleNamespace(
        create_public_app=lambda settings: public,
    ))
    calls = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    entry.main()
    assert calls[0][0] is public
    assert calls[0][1]["workers"] == 1


def test_private_server_still_rejects_unprotected_public_binding(monkeypatch):
    monkeypatch.setenv("FRAMEKEEP_HOST", "0.0.0.0")
    monkeypatch.delenv("FRAMEKEEP_PUBLIC_MODE", raising=False)
    monkeypatch.delenv("FRAMEKEEP_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(entry, "load_dotenv", lambda: None)
    with pytest.raises(SystemExit, match="FRAMEKEEP_ADMIN_TOKEN"):
        entry.main()


def test_invalid_public_mode_fails_instead_of_silently_changing_access(monkeypatch):
    monkeypatch.setenv("FRAMEKEEP_HOST", "127.0.0.1")
    monkeypatch.setenv("FRAMEKEEP_PUBLIC_MODE", "true")
    monkeypatch.setattr(entry, "load_dotenv", lambda: None)
    monkeypatch.setattr(entry, "create_app", lambda: object())
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: None)
    with pytest.raises(SystemExit, match="FRAMEKEEP_PUBLIC_MODE"):
        entry.main()
