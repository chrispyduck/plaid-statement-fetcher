from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Any

import httpx

from .models import LinkedItem
from .provider_errors import ProviderAPIError
from .settings import Settings

logger = logging.getLogger(__name__)

# Yodlee access tokens are valid for 30 minutes; refresh a bit early so a token
# handed to a slow caller doesn't expire mid-use.
_TOKEN_TTL_SECONDS = 30 * 60
_TOKEN_REFRESH_MARGIN_SECONDS = 120

_DATE_KEYS = (
    "statementDate",
    "documentDate",
    "asOfDate",
    "createdDate",
    "lastUpdated",
    "generatedDate",
)
_DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})|(\d{2}/\d{2}/\d{4})")


class YodleeAPIError(ProviderAPIError):
    pass


def _parse_date_like(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def extract_document_date(document: dict[str, Any]) -> date:
    """Best-effort statement date for a Yodlee document.

    Yodlee's public Documents schema doesn't document a date field explicitly, so
    this tries several plausible field names, then a date embedded in the filename,
    before giving up and returning today's date. Callers should treat the
    today's-date fallback as a sign the real field name needs to be identified from
    a live response and added to _DATE_KEYS above.
    """
    for key in _DATE_KEYS:
        parsed = _parse_date_like(document.get(key))
        if parsed is not None:
            return parsed

    name = str(document.get("name") or "")
    match = _DATE_IN_NAME_RE.search(name)
    if match:
        text = match.group(1) or match.group(2)
        if "/" in text:
            month, day, year = text.split("/")
            text = f"{year}-{month}-{day}"
        parsed = _parse_date_like(text)
        if parsed is not None:
            return parsed

    logger.warning(
        "Could not determine statement date for Yodlee document id=%s name=%r; "
        "falling back to today",
        document.get("id"),
        name,
    )
    return date.today()


class YodleeClient:
    def __init__(self, settings: Settings, timeout_seconds: float = 30.0) -> None:
        self._settings = settings
        self._timeout = timeout_seconds
        self._token_lock = threading.Lock()
        self._cached_token: str | None = None
        self._cached_token_expires_at: float = 0.0

    def _require_credentials(self) -> tuple[str, str, str]:
        settings = self._settings
        if not settings.yodlee_client_id or not settings.yodlee_secret:
            raise YodleeAPIError("Yodlee credentials are not configured.")
        if not settings.yodlee_login_name:
            raise YodleeAPIError("Yodlee login name is not configured.")
        return settings.yodlee_client_id, settings.yodlee_secret, settings.yodlee_login_name

    def _access_token(self) -> str:
        with self._token_lock:
            now = time.monotonic()
            if self._cached_token and now < self._cached_token_expires_at:
                return self._cached_token

            client_id, secret, login_name = self._require_credentials()
            url = f"{self._settings.yodlee_api_url}/auth/token"
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        url,
                        headers={
                            "loginName": login_name,
                            "Api-Version": "1.1",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        data={"clientId": client_id, "secret": secret},
                    )
            except httpx.HTTPError as exc:
                raise YodleeAPIError(
                    f"Yodlee auth request failed: {exc}", retriable=True
                ) from exc

            if response.status_code >= 400:
                self._raise_for_response("/auth/token", response)

            payload = response.json()
            token = (payload.get("token") or {}).get("accessToken")
            if not token:
                raise YodleeAPIError("Yodlee did not return an accessToken.")

            self._cached_token = token
            self._cached_token_expires_at = now + _TOKEN_TTL_SECONDS - _TOKEN_REFRESH_MARGIN_SECONDS
            return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Api-Version": "1.1",
        }

    def _raise_for_response(self, endpoint: str, response: httpx.Response) -> None:
        try:
            details = response.json()
        except ValueError:
            details = {"message": response.text}
        # Mirror Plaid's snake_case error_code/error_message keys alongside Yodlee's
        # native errorCode/errorMessage, so sync.py's provider-agnostic error handling
        # (which reads error_code) works for both without caring which provider raised.
        if "errorCode" in details:
            details["error_code"] = details["errorCode"]
        if "errorMessage" in details:
            details["error_message"] = details["errorMessage"]
        retriable = response.status_code == 429 or response.status_code >= 500
        logger.error(
            "Yodlee request failed endpoint=%s status=%s error_code=%s",
            endpoint,
            response.status_code,
            details.get("errorCode"),
        )
        raise YodleeAPIError(
            "Yodlee API request failed",
            status_code=response.status_code,
            retriable=retriable,
            details=details,
        )

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._settings.yodlee_api_url}{endpoint}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise YodleeAPIError(f"Yodlee API request failed: {exc}", retriable=True) from exc

        if response.status_code >= 400:
            self._raise_for_response(endpoint, response)
        return response.json()

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._settings.yodlee_api_url}{endpoint}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise YodleeAPIError(f"Yodlee API request failed: {exc}", retriable=True) from exc

        if response.status_code >= 400:
            self._raise_for_response(endpoint, response)
        return response.json() if response.content else {}

    def _put(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._settings.yodlee_api_url}{endpoint}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.put(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise YodleeAPIError(f"Yodlee API request failed: {exc}", retriable=True) from exc

        if response.status_code >= 400:
            self._raise_for_response(endpoint, response)
        return response.json() if response.content else {}

    def create_fastlink_session(self) -> dict[str, str]:
        return {
            "access_token": self._access_token(),
            "fastlink_url": self._settings.yodlee_fastlink_url,
            "config_name": self._settings.yodlee_fastlink_config_name,
        }

    def get_provider_account(self, provider_account_id: str) -> dict[str, Any]:
        response = self._get(f"/providerAccounts/{provider_account_id}")
        accounts = response.get("providerAccount") or []
        if not accounts:
            raise YodleeAPIError(f"Unknown Yodlee providerAccountId: {provider_account_id}")
        return accounts[0] if isinstance(accounts, list) else accounts

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        response = self._get(f"/providers/{provider_id}")
        providers = response.get("provider") or []
        if not providers:
            raise YodleeAPIError(f"Unknown Yodlee providerId: {provider_id}")
        return providers[0] if isinstance(providers, list) else providers

    def get_accounts_for_provider_account(
        self, provider_account_id: str
    ) -> tuple[list[dict[str, Any]], str, str, str | None]:
        """Returns (accounts, provider_id, provider_name, provider_logo_url).

        providerAccounts/{id} only carries a bare providerId (no name/logo), so the
        institution's display name and logo come from a separate /providers/{id} call.
        """
        provider_account = self.get_provider_account(provider_account_id)
        provider_id = str(provider_account.get("providerId") or provider_account_id)

        provider = self.get_provider(provider_id)
        provider_name = str(provider.get("name") or "Unknown Institution")
        provider_logo = provider.get("logo")

        response = self._get("/accounts", params={"providerAccountId": provider_account_id})
        accounts = response.get("account") or []
        return accounts, provider_id, provider_name, provider_logo

    def refresh_provider_account(self, provider_account_id: str) -> str | None:
        response = self._put(
            f"/providerAccounts/{provider_account_id}/refresh",
            {"configName": self._settings.yodlee_fastlink_config_name},
        )
        provider_account = response.get("providerAccount") or {}
        if isinstance(provider_account, list):
            provider_account = provider_account[0] if provider_account else {}
        return provider_account.get("requestId")

    def search_documents(self, account_ids: list[str]) -> list[dict[str, Any]]:
        response = self._post(
            "/documents/search",
            {"containerType": "bank", "accountId": account_ids},
        )
        documents = response.get("document") or []
        return documents if isinstance(documents, list) else [documents]

    def download_document(self, document_id: str) -> tuple[bytes, None]:
        """Re-look up the document to get a fresh downloadURL, then fetch its bytes.

        downloadURL is time-limited (urlExpiryTime), so this is deliberately a fresh
        lookup rather than reusing a URL captured earlier during search_documents.
        """
        response = self._get(f"/documents/{document_id}")
        documents = response.get("document") or []
        document = documents[0] if isinstance(documents, list) and documents else documents
        download_url = document.get("downloadURL") if isinstance(document, dict) else None
        if not download_url:
            raise YodleeAPIError(f"Yodlee document {document_id} has no downloadURL.")

        auth_header = self._headers()["Authorization"]
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(download_url, headers={"Authorization": auth_header})
        except httpx.HTTPError as exc:
            raise YodleeAPIError(
                f"Yodlee document download failed: {exc}", retriable=True
            ) from exc

        if response.status_code >= 400:
            self._raise_for_response(f"/documents/{document_id}:download", response)

        # Yodlee has no documented equivalent to Plaid's content-hash header, so
        # sync.py's checksum-mismatch check is skipped for Yodlee downloads.
        return response.content, None

    # -- linked_item-aware wrappers, mirroring PlaidClient's call shape so sync.py can
    # dispatch to either provider through a single pair of branches. --

    def list_statements_for_item(self, linked_item: LinkedItem) -> dict[str, Any]:
        account_ids = [account.account_id for account in linked_item.accounts]
        documents = self.search_documents(account_ids)
        configured_ids = set(account_ids)

        statements_by_account: dict[str, list[dict[str, Any]]] = {
            account_id: [] for account_id in account_ids
        }
        for document in documents:
            associated = document.get("associatedAccounts") or []
            account_id = next((str(a) for a in associated if str(a) in configured_ids), None)
            if account_id is None:
                continue
            document_id = document.get("id")
            if document_id is None:
                continue
            statements_by_account[account_id].append(
                {
                    "statement_id": str(document_id),
                    "date_posted": extract_document_date(document).isoformat(),
                }
            )

        return {
            "institution_name": linked_item.institution_name,
            "accounts": [
                {
                    "account_id": account.account_id,
                    "account_name": account.account_name,
                    "statements": statements_by_account.get(account.account_id, []),
                }
                for account in linked_item.accounts
            ],
        }

    def refresh_statements_for_item(
        self, linked_item: LinkedItem, start_date: date, end_date: date
    ) -> str | None:
        _ = (start_date, end_date)  # Yodlee refreshes all data; no date window to pass.
        return self.refresh_provider_account(linked_item.item_id)

    def download_statement_for_item(
        self, linked_item: LinkedItem, statement_id: str
    ) -> tuple[bytes, str | None]:
        _ = linked_item
        return self.download_document(statement_id)
