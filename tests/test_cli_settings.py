from __future__ import annotations

from statement_fetcher.cli import resolve_settings


def test_resolve_settings_uses_env_var_by_default(monkeypatch) -> None:
    monkeypatch.setenv("PLAID_ENV", "production")
    settings = resolve_settings(None)
    assert settings.plaid_env == "production"


def test_resolve_settings_explicit_env_overrides_env_var(monkeypatch) -> None:
    monkeypatch.setenv("PLAID_ENV", "production")
    settings = resolve_settings("sandbox")
    assert settings.plaid_env == "sandbox"