from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .settings import Settings

logger = logging.getLogger(__name__)


class PlaidAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable
        self.details = details or {}


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
        logger.info("Plaid request start endpoint=%s env=%s", endpoint, self._settings.plaid_env)

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, json=merged_payload)
        except httpx.HTTPError as exc:
            logger.exception("Plaid HTTP transport error endpoint=%s", endpoint)
            raise PlaidAPIError(
                f"Plaid API request failed: {exc}",
                retriable=True,
            ) from exc

        if response.status_code >= 400:
            try:
                details = response.json()
            except ValueError:
                details = {"message": response.text}
            retriable = response.status_code == 429 or response.status_code >= 500
            logger.error(
                "Plaid request failed endpoint=%s status=%s code=%s request_id=%s",
                endpoint,
                response.status_code,
                details.get("error_code"),
                details.get("request_id"),
            )
            raise PlaidAPIError(
                "Plaid API request failed",
                status_code=response.status_code,
                retriable=retriable,
                details=details,
            )

        logger.info("Plaid request success endpoint=%s status=%s", endpoint, response.status_code)
        return response.json()

    def _post_binary(
        self,
        endpoint: str,
        payload: dict[str, Any],
    ) -> tuple[bytes, httpx.Headers]:
        url = f"{self._base_url}{endpoint}"
        merged_payload = {**self._auth_payload(), **payload}
        logger.info(
            "Plaid binary request start endpoint=%s env=%s",
            endpoint,
            self._settings.plaid_env,
        )

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, json=merged_payload)
        except httpx.HTTPError as exc:
            logger.exception("Plaid binary transport error endpoint=%s", endpoint)
            raise PlaidAPIError(
                f"Plaid API request failed: {exc}",
                retriable=True,
            ) from exc

        if response.status_code >= 400:
            retriable = response.status_code == 429 or response.status_code >= 500
            logger.error(
                "Plaid binary request failed endpoint=%s status=%s",
                endpoint,
                response.status_code,
            )
            raise PlaidAPIError(
                f"Plaid API binary request failed with status {response.status_code}",
                status_code=response.status_code,
                retriable=retriable,
            )

        logger.info(
            "Plaid binary request success endpoint=%s status=%s bytes=%s",
            endpoint,
            response.status_code,
            len(response.content),
        )
        return response.content, response.headers

    def _normalize_origin(self, origin: str) -> str:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PlaidAPIError(
                "Invalid origin supplied for Plaid link token creation.",
            )
        return f"{parsed.scheme}://{parsed.netloc}"

    def _resolve_redirect_uri(self, origin: str | None) -> str:
        configured_redirect_uri = self._settings.plaid_redirect_uri.strip()
        if configured_redirect_uri and configured_redirect_uri.lower() != "auto":
            parsed = urlparse(configured_redirect_uri)
            if (
                parsed.hostname in {"localhost", "127.0.0.1"}
                and origin
            ):
                return f"{self._normalize_origin(origin)}/plaid/callback"
            return configured_redirect_uri

        if not origin:
            raise PlaidAPIError(
                "No redirect URI available. Set PLAID_REDIRECT_URI or pass a browser origin.",
            )

        return f"{self._normalize_origin(origin)}/plaid/callback"

    def _normalize_language(self, language: str) -> str:
        # Plaid link token expects a base language code (e.g., "en"), not locale variants.
        normalized = language.strip()
        if not normalized:
            raise PlaidAPIError("Plaid language cannot be empty.")
        return normalized.split("-")[0].split("_")[0]

    def _products(self) -> list[str]:
        raw = self._settings.plaid_products or ""
        products = [item.strip() for item in raw.split(",") if item.strip()]
        if not products:
            raise PlaidAPIError("At least one Plaid product is required.")
        return products

    def _statements_window(self) -> dict[str, str]:
        end_date = self._settings.statements_end_date or date.today()
        start_date = self._settings.statements_start_date or (end_date - timedelta(days=730))
        if start_date > end_date:
            raise PlaidAPIError("Statements start date cannot be after end date.")
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

    def create_link_token(self, origin: str | None) -> str:
        redirect_uri = self._resolve_redirect_uri(origin)
        products = self._products()

        payload = {
            "client_name": "Statement Fetcher",
            "language": self._normalize_language(self._settings.plaid_language),
            "country_codes": [self._settings.plaid_country_codes],
            "products": products,
            "redirect_uri": redirect_uri,
            "user": {"client_user_id": "statement-fetcher-local-user"},
        }
        if "statements" in products:
            payload["statements"] = self._statements_window()

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

    def get_institution_name(self, institution_id: str | None) -> tuple[str, str, str | None]:
        if not institution_id:
            return "unknown", "Unknown Institution", None

        response = self._post(
            "/institutions/get_by_id",
            {
                "institution_id": institution_id,
                "country_codes": [self._settings.plaid_country_codes],
            },
        )
        institution = response.get("institution") or {}
        name = institution.get("name") or institution_id
        logo = institution.get("logo")
        return institution_id, name, logo

    def list_statements(self, access_token: str) -> dict[str, Any]:
        return self._post("/statements/list", {"access_token": access_token})

    def download_statement(self, access_token: str, statement_id: str) -> tuple[bytes, str | None]:
        content, headers = self._post_binary(
            "/statements/download",
            {"access_token": access_token, "statement_id": statement_id},
        )
        return content, headers.get("Plaid-Content-Hash")
