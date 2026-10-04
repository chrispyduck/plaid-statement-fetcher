from __future__ import annotations

from datetime import date

from statement_fetcher.models import LinkedAccount, LinkedItem
from statement_fetcher.settings import Settings
from statement_fetcher.yodlee_api import YodleeClient, extract_document_date


def _settings() -> Settings:
    return Settings(
        yodlee_client_id="client",
        yodlee_secret="secret",
        yodlee_login_name="sbMem1",
    )


def test_extract_document_date_prefers_known_field() -> None:
    assert extract_document_date({"statementDate": "2026-06-30"}) == date(2026, 6, 30)
    assert extract_document_date({"createdDate": "2026-06-30T10:00:00Z"}) == date(2026, 6, 30)


def test_extract_document_date_parses_date_from_name() -> None:
    assert extract_document_date({"name": "Statement 2026-06-30.pdf"}) == date(2026, 6, 30)
    assert extract_document_date({"name": "Statement 06/30/2026.pdf"}) == date(2026, 6, 30)


def test_extract_document_date_falls_back_to_today() -> None:
    result = extract_document_date({"name": "no-date-here.pdf"})
    assert result == date.today()


class CapturingYodleeClient(YodleeClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.search_calls: list[list[str]] = []
        self.refresh_calls: list[str] = []
        self.download_calls: list[str] = []
        self.documents_by_account: dict[str, list[dict]] = {}

    def search_documents(self, account_ids: list[str]) -> list[dict]:
        self.search_calls.append(account_ids)
        documents = []
        for account_id in account_ids:
            documents.extend(self.documents_by_account.get(account_id, []))
        return documents

    def refresh_provider_account(self, provider_account_id: str) -> str | None:
        self.refresh_calls.append(provider_account_id)
        return "req_1"

    def download_document(self, document_id: str) -> tuple[bytes, None]:
        self.download_calls.append(document_id)
        return b"%PDF-1.7 fake", None


def test_list_statements_for_item_groups_documents_by_account() -> None:
    client = CapturingYodleeClient(_settings())
    client.documents_by_account = {
        "acc_1": [
            {
                "id": 111,
                "name": "stmt.pdf",
                "statementDate": "2026-06-30",
                "associatedAccounts": ["acc_1"],
            }
        ],
        "acc_2": [],
    }

    linked_item = LinkedItem(
        provider="yodlee",
        institution_id="16445",
        institution_name="Dag Site",
        item_id="provacc_1",
        access_token="sbMem1",
        accounts=[
            LinkedAccount(account_id="acc_1", account_name="Checking"),
            LinkedAccount(account_id="acc_2", account_name="Savings"),
        ],
    )

    result = client.list_statements_for_item(linked_item)

    assert client.search_calls == [["acc_1", "acc_2"]]
    assert result["institution_name"] == "Dag Site"
    accounts_by_id = {account["account_id"]: account for account in result["accounts"]}
    assert accounts_by_id["acc_1"]["statements"] == [
        {"statement_id": "111", "date_posted": "2026-06-30"}
    ]
    assert accounts_by_id["acc_2"]["statements"] == []


def test_refresh_statements_for_item_delegates_to_provider_account() -> None:
    client = CapturingYodleeClient(_settings())
    linked_item = LinkedItem(
        provider="yodlee",
        institution_id="16445",
        institution_name="Dag Site",
        item_id="provacc_1",
        access_token="sbMem1",
        accounts=[],
    )

    request_id = client.refresh_statements_for_item(
        linked_item, date(2026, 1, 1), date(2026, 6, 30)
    )

    assert request_id == "req_1"
    assert client.refresh_calls == ["provacc_1"]


def test_download_statement_for_item_delegates_to_download_document() -> None:
    client = CapturingYodleeClient(_settings())
    linked_item = LinkedItem(
        provider="yodlee",
        institution_id="16445",
        institution_name="Dag Site",
        item_id="provacc_1",
        access_token="sbMem1",
        accounts=[],
    )

    content, content_hash = client.download_statement_for_item(linked_item, "111")

    assert content == b"%PDF-1.7 fake"
    assert content_hash is None
    assert client.download_calls == ["111"]
