from __future__ import annotations

from datetime import date

from statement_fetcher.plaid_api import PlaidClient
from statement_fetcher.settings import Settings


def test_resolve_redirect_uri_from_origin_when_auto() -> None:
    settings = Settings(
        plaid_env="sandbox",
        plaid_redirect_uri="auto",
        plaid_client_id="client",
        plaid_secret="secret",
    )
    client = PlaidClient(settings)

    assert (
        client._resolve_redirect_uri("https://statement-fetcher.localhost")
        == "https://statement-fetcher.localhost/plaid/callback"
    )


def test_resolve_redirect_uri_rewrites_localhost_config_with_origin() -> None:
    settings = Settings(
        plaid_env="sandbox",
        plaid_redirect_uri="http://localhost:8765/plaid/callback",
        plaid_client_id="client",
        plaid_secret="secret",
    )
    client = PlaidClient(settings)

    assert (
        client._resolve_redirect_uri("https://statement-fetcher.localhost")
        == "https://statement-fetcher.localhost/plaid/callback"
    )


def test_normalize_language_reduces_locale_variant() -> None:
    settings = Settings(
        plaid_env="sandbox",
        plaid_client_id="client",
        plaid_secret="secret",
    )
    client = PlaidClient(settings)

    assert client._normalize_language("en-US") == "en"
    assert client._normalize_language("fr_CA") == "fr"


def test_products_parser_supports_csv() -> None:
    settings = Settings(
        plaid_env="sandbox",
        plaid_products="statements,transactions",
        plaid_client_id="client",
        plaid_secret="secret",
    )
    client = PlaidClient(settings)

    assert client._products() == ["statements", "transactions"]


def test_statements_window_uses_explicit_dates() -> None:
    settings = Settings(
        plaid_env="sandbox",
        plaid_client_id="client",
        plaid_secret="secret",
        PSF_STATEMENTS_START_DATE=date(2025, 1, 1),
        PSF_STATEMENTS_END_DATE=date(2025, 12, 31),
    )
    client = PlaidClient(settings)

    assert client._statements_window() == {
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
    }


def test_create_link_token_includes_statements_object() -> None:
    class CapturingPlaidClient(PlaidClient):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            self.last_payload: dict | None = None

        def _post(self, endpoint: str, payload: dict) -> dict:  # type: ignore[override]
            assert endpoint == "/link/token/create"
            self.last_payload = payload
            return {"link_token": "token_123"}

    settings = Settings(
        plaid_env="sandbox",
        plaid_client_id="client",
        plaid_secret="secret",
        plaid_products="statements",
    )
    client = CapturingPlaidClient(settings)

    token = client.create_link_token("https://statement-fetcher.localhost:8765")

    assert token == "token_123"
    assert client.last_payload is not None
    assert client.last_payload["products"] == ["statements"]
    assert client.last_payload["statements"]["start_date"]
    assert client.last_payload["statements"]["end_date"]


def test_refresh_statements_posts_date_range_and_returns_request_id() -> None:
    class CapturingPlaidClient(PlaidClient):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            self.last_endpoint: str | None = None
            self.last_payload: dict | None = None

        def _post(self, endpoint: str, payload: dict) -> dict:  # type: ignore[override]
            self.last_endpoint = endpoint
            self.last_payload = payload
            return {"request_id": "req_abc"}

    settings = Settings(plaid_env="sandbox", plaid_client_id="client", plaid_secret="secret")
    client = CapturingPlaidClient(settings)

    request_id = client.refresh_statements("access_1", date(2026, 1, 1), date(2026, 6, 30))

    assert request_id == "req_abc"
    assert client.last_endpoint == "/statements/refresh"
    assert client.last_payload == {
        "access_token": "access_1",
        "start_date": "2026-01-01",
        "end_date": "2026-06-30",
    }
