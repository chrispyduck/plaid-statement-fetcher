from __future__ import annotations

from typing import Any

import httpx

from .settings import Settings


class PlaidAPIError(RuntimeError):
    pass


class PlaidClient:
    def __init__(self, settings: Settings, timeout_seconds: float = 30.0) -> None:
        self._settings = settings
        self._timeout = timeout_seconds
        self._base_url = {
            "sandbox": "https://sandbox.plaid.com",
            "production": "https://production.plaid.com",
        }[settings.plaid_env]

    def _auth_payload(self) -> dict[str, str]:
        if not self._settings.plaid_client_id or not self._settings.plaid_secret:
            raise PlaidAPIError("Plaid credentials are not configured.")
        return {
            "client_id": self._settings.plaid_client_id,
            "secret": self._settings.plaid_secret,
        }

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{endpoint}"
        merged_payload = {**self._auth_payload(), **payload}

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(url, json=merged_payload)

        if response.status_code >= 400:
            try:
                details = response.json()
            except ValueError:
                details = {"message": response.text}
            raise PlaidAPIError(f"Plaid API request failed: {details}")

        return response.json()

    def create_link_token(self, host: str) -> str:
        redirect_uri = self._settings.plaid_redirect_uri
        if "localhost" in redirect_uri or "127.0.0.1" in redirect_uri:
            redirect_uri = f"http://{host}/plaid/callback"

        payload = {
            "client_name": "Statement Fetcher",
            "language": self._settings.plaid_language,
            "country_codes": [self._settings.plaid_country_codes],
            "products": [self._settings.plaid_products],
            "redirect_uri": redirect_uri,
            "user": {"client_user_id": "statement-fetcher-local-user"},
        }
        response = self._post("/link/token/create", payload)
        link_token = response.get("link_token")
        if not link_token:
            raise PlaidAPIError("Plaid did not return a link_token.")
        return link_token

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        response = self._post("/item/public_token/exchange", {"public_token": public_token})
        access_token = response.get("access_token")
        item_id = response.get("item_id")
        if not access_token or not item_id:
            raise PlaidAPIError("Plaid token exchange response was missing fields.")
        return access_token, item_id

    def get_accounts(self, access_token: str) -> tuple[list[dict[str, Any]], str | None]:
        response = self._post("/accounts/get", {"access_token": access_token})
        accounts = response.get("accounts") or []
        item = response.get("item") or {}
        return accounts, item.get("institution_id")

    def get_institution_name(self, institution_id: str | None) -> tuple[str, str]:
        if not institution_id:
            return "unknown", "Unknown Institution"

        response = self._post(
            "/institutions/get_by_id",
            {
                "institution_id": institution_id,
                "country_codes": [self._settings.plaid_country_codes],
            },
        )
        institution = response.get("institution") or {}
        name = institution.get("name") or institution_id
        return institution_id, name
