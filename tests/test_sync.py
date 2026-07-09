from __future__ import annotations

from datetime import date

from statement_fetcher.models import LinkedAccount, LinkedItem
from statement_fetcher.settings import Settings
from statement_fetcher.storage import load_state, upsert_linked_item
from statement_fetcher.sync import sync_statements


class FakeSyncPlaidClient:
    def __init__(self) -> None:
        self.download_calls = 0

    def list_statements(self, access_token: str) -> dict:
        assert access_token == "access_1"
        return {
            "institution_name": "Chase",
            "accounts": [
                {
                    "account_id": "acc_1",
                    "account_name": "Everyday Checking",
                    "statements": [
                        {
                            "statement_id": "stmt_1",
                            "date_posted": "2026-06-30",
                            "month": 6,
                            "year": 2026,
                        }
                    ],
                }
            ],
        }

    def download_statement(self, access_token: str, statement_id: str) -> tuple[bytes, str | None]:
        assert access_token == "access_1"
        assert statement_id == "stmt_1"
        self.download_calls += 1
        content = b"%PDF-1.7 fake"
        return content, None


def test_sync_downloads_and_dedupes(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    linked_item = LinkedItem(
        institution_id="ins_1",
        institution_name="Chase",
        item_id="item_1",
        access_token="access_1",
        accounts=[LinkedAccount(account_id="acc_1", account_name="Checking")],
    )
    upsert_linked_item(settings, linked_item)

    client = FakeSyncPlaidClient()

    first = sync_statements(settings, plaid_client=client)
    second = sync_statements(settings, plaid_client=client)

    assert first.downloaded == 1
    assert second.downloaded == 0
    assert second.skipped_existing == 1

    state = load_state(settings)
    assert len(state.downloaded_statements) == 1
    assert "2026-06-30" in state.downloaded_statements[0].file_path


def test_sync_since_filter(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    linked_item = LinkedItem(
        institution_id="ins_1",
        institution_name="Chase",
        item_id="item_1",
        access_token="access_1",
        accounts=[LinkedAccount(account_id="acc_1", account_name="Checking")],
    )
    upsert_linked_item(settings, linked_item)

    client = FakeSyncPlaidClient()
    result = sync_statements(
        settings,
        plaid_client=client,
        since=date(2026, 7, 1),
    )

    assert result.downloaded == 0
    assert result.skipped_filtered == 1
    assert client.download_calls == 0


class FakeSyncPlaidClientWithSparseAccounts:
    def list_statements(self, access_token: str) -> dict:
        assert access_token == "access_1"
        return {
            "institution_name": "Chase",
            "accounts": [
                {
                    "account_id": "acc_1",
                    "account_name": "Everyday Checking",
                    "statements": [],
                }
            ],
        }

    def download_statement(self, access_token: str, statement_id: str) -> tuple[bytes, str | None]:
        raise AssertionError("No statement downloads expected")


def test_sync_logs_no_statement_and_unavailable_accounts(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    linked_item = LinkedItem(
        institution_id="ins_1",
        institution_name="Chase",
        item_id="item_1",
        access_token="access_1",
        accounts=[
            LinkedAccount(account_id="acc_1", account_name="Checking"),
            LinkedAccount(account_id="acc_2", account_name="Savings"),
        ],
    )
    upsert_linked_item(settings, linked_item)

    client = FakeSyncPlaidClientWithSparseAccounts()
    captured_events: list[tuple[str, str, dict[str, str | int] | None]] = []

    summary = sync_statements(
        settings,
        plaid_client=client,
        event_callback=lambda event_type, message, metadata: captured_events.append(
            (event_type, message, metadata)
        ),
    )

    assert summary.downloaded == 0
    event_types = [event_type for event_type, _message, _metadata in captured_events]
    assert "account_no_statements" in event_types
    assert "account_statement_unavailable" in event_types

    unavailable = [entry for entry in captured_events if entry[0] == "account_statement_unavailable"]
    assert unavailable[0][2] is not None
    assert unavailable[0][2]["account_id"] == "acc_2"
