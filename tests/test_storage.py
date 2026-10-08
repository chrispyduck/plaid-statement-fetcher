from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from statement_fetcher.models import DownloadedStatement, LinkedAccount, LinkedItem, StateFile
from statement_fetcher.settings import Settings
from statement_fetcher.storage import (
    add_event,
    complete_refresh_job,
    complete_sync_job,
    create_sync_job,
    ensure_environment_files,
    fail_sync_job,
    get_latest_completed_job,
    get_latest_job,
    get_sync_job,
    has_running_job,
    list_events,
    list_sync_jobs,
    load_configuration,
    load_state,
    merge_duplicate_accounts,
    remove_account_from_configuration,
    save_state,
    update_refresh_job_progress,
    update_sync_job_progress,
    upsert_linked_item,
)


def test_access_token_encrypted_at_rest_when_secret_is_set(tmp_path) -> None:
    settings = Settings(
        plaid_env="sandbox",
        PSF_CONFIG_ROOT=tmp_path,
        PSF_ENCRYPTION_SECRET="test-secret",
    )

    linked_item = LinkedItem(
        institution_id="ins_1",
        institution_name="Bank A",
        item_id="item_1",
        access_token="access-plain",
        accounts=[LinkedAccount(account_id="acc_1", account_name="Checking")],
    )
    upsert_linked_item(settings, linked_item)

    config = load_configuration(settings)
    assert config.linked_items[0].access_token == "access-plain"

    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        row = conn.execute(
            "SELECT access_token FROM linked_items WHERE item_id = ?",
            ("item_1",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] != "access-plain"
    assert str(row[0]).startswith("enc:v1:")


def test_plaintext_access_token_compatibility_without_secret(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    linked_item = LinkedItem(
        institution_id="ins_1",
        institution_name="Bank A",
        item_id="item_1",
        access_token="access-plain",
        accounts=[LinkedAccount(account_id="acc_1", account_name="Checking")],
    )
    upsert_linked_item(settings, linked_item)

    config = load_configuration(settings)
    assert config.linked_items[0].access_token == "access-plain"


def test_provider_defaults_to_plaid_and_round_trips(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    upsert_linked_item(
        settings,
        LinkedItem(
            institution_id="ins_1",
            institution_name="Bank A",
            item_id="item_1",
            access_token="token_1",
            accounts=[LinkedAccount(account_id="acc_1", account_name="Checking")],
        ),
    )
    upsert_linked_item(
        settings,
        LinkedItem(
            provider="yodlee",
            institution_id="16445",
            institution_name="Dag Site",
            item_id="provacc_1",
            access_token="sbMem1",
            accounts=[LinkedAccount(account_id="acc_2", account_name="Savings")],
        ),
    )

    config = load_configuration(settings)
    providers = {item.item_id: item.provider for item in config.linked_items}
    assert providers == {"item_1": "plaid", "provacc_1": "yodlee"}


def test_migrates_pre_provider_column_linked_items_table(tmp_path) -> None:
    # Regression test: a database created before the provider column existed must
    # upgrade cleanly and treat every pre-existing row as a Plaid item.
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    db_path = tmp_path / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE linked_items (
                item_id TEXT PRIMARY KEY,
                institution_id TEXT NOT NULL,
                institution_name TEXT NOT NULL,
                institution_logo TEXT,
                access_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                login_required INTEGER NOT NULL DEFAULT 0,
                login_required_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO linked_items (
                item_id, institution_id, institution_name, access_token,
                created_at, updated_at
            ) VALUES ('item_old', 'ins_1', 'Bank A', 'token', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()

    config = load_configuration(settings)  # must not raise
    assert config.linked_items[0].provider == "plaid"


def test_upsert_linked_item_preserves_existing_alias(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    first = LinkedItem(
        institution_id="ins_1",
        institution_name="Bank A",
        item_id="item_1",
        access_token="token_1",
        accounts=[LinkedAccount(account_id="acc_1", account_name="Checking", alias="Family")],
    )
    upsert_linked_item(settings, first)

    second = LinkedItem(
        institution_id="ins_1",
        institution_name="Bank A",
        item_id="item_1",
        access_token="token_2",
        accounts=[LinkedAccount(account_id="acc_1", account_name="Checking Updated")],
    )
    upsert_linked_item(settings, second)

    config = load_configuration(settings)
    assert len(config.linked_items) == 1
    assert config.linked_items[0].access_token == "token_2"
    assert config.linked_items[0].accounts[0].alias == "Family"


def test_remove_account_prunes_empty_item(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    linked_item = LinkedItem(
        institution_id="ins_1",
        institution_name="Bank A",
        item_id="item_1",
        access_token="token_1",
        accounts=[LinkedAccount(account_id="acc_1", account_name="Checking")],
    )
    upsert_linked_item(settings, linked_item)

    changed = remove_account_from_configuration(settings, "acc_1")
    config = load_configuration(settings)

    assert changed is True
    assert config.linked_items == []


def test_single_mode_storage_paths(tmp_path) -> None:
    settings = Settings(plaid_env="production", PSF_CONFIG_ROOT=tmp_path)

    ensure_environment_files(settings)

    assert (tmp_path / "state.db").exists()
    assert (tmp_path / "output").exists()
    assert not (tmp_path / "sandbox").exists()
    assert not (tmp_path / "production").exists()


def test_migrates_pre_job_type_sync_jobs_table(tmp_path) -> None:
    # Regression test: a database created before job_type/requested/failed existed
    # must upgrade cleanly, including the job_type index (which previously lived in
    # the same executescript as the no-op CREATE TABLE IF NOT EXISTS, so it ran
    # before the column that backs it was added by the migration below it).
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    db_path = tmp_path / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE sync_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT,
                listed INTEGER NOT NULL DEFAULT 0,
                downloaded INTEGER NOT NULL DEFAULT 0,
                skipped_existing INTEGER NOT NULL DEFAULT 0,
                skipped_filtered INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    ensure_environment_files(settings)  # must not raise

    create_sync_job(
        settings, "job-after-migration", "2026-01-01T00:00:00+00:00", job_type="refresh"
    )
    job = get_sync_job(settings, "job-after-migration")
    assert job is not None
    assert job["job_type"] == "refresh"


def test_sync_job_persistence_lifecycle(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    job_id = "job-test-1"
    started_at = "2026-01-01T00:00:00+00:00"

    create_sync_job(settings, job_id, started_at)
    update_sync_job_progress(
        settings,
        job_id=job_id,
        listed=10,
        downloaded=3,
        skipped_existing=4,
        skipped_filtered=2,
        errors=1,
    )

    current = get_sync_job(settings, job_id)
    assert current is not None
    assert current["status"] == "running"
    assert current["listed"] == 10

    complete_sync_job(
        settings,
        job_id=job_id,
        finished_at="2026-01-01T00:02:00+00:00",
        listed=10,
        downloaded=6,
        skipped_existing=3,
        skipped_filtered=1,
        errors=0,
    )

    completed = get_sync_job(settings, job_id)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["downloaded"] == 6

    jobs = list_sync_jobs(settings)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job_id


def test_sync_job_failure_persisted(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    job_id = "job-test-failed"
    create_sync_job(settings, job_id, "2026-01-01T00:00:00+00:00")

    fail_sync_job(
        settings,
        job_id=job_id,
        finished_at="2026-01-01T00:01:00+00:00",
        error="boom",
    )

    failed = get_sync_job(settings, job_id)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"


def test_refresh_and_sync_jobs_are_tracked_independently(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)

    create_sync_job(settings, "refresh-1", "2026-01-01T00:00:00+00:00", job_type="refresh")
    create_sync_job(settings, "sync-1", "2026-01-01T00:00:00+00:00", job_type="sync")

    assert has_running_job(settings, "refresh") is True
    assert has_running_job(settings, "sync") is True

    update_refresh_job_progress(settings, job_id="refresh-1", requested=2, failed=1)
    complete_refresh_job(
        settings,
        job_id="refresh-1",
        finished_at="2026-01-01T00:01:00+00:00",
        requested=3,
        failed=1,
    )

    assert has_running_job(settings, "refresh") is False
    refreshed = get_sync_job(settings, "refresh-1")
    assert refreshed is not None
    assert refreshed["job_type"] == "refresh"
    assert refreshed["status"] == "completed"
    assert refreshed["requested"] == 3
    assert refreshed["failed"] == 1
    # Fetch-specific counters stay untouched by a refresh job.
    assert refreshed["listed"] == 0
    assert refreshed["downloaded"] == 0

    assert list_sync_jobs(settings, job_type="refresh") == [refreshed]
    assert [job["job_id"] for job in list_sync_jobs(settings, job_type="sync")] == ["sync-1"]
    assert len(list_sync_jobs(settings)) == 2

    assert get_latest_job(settings, "refresh")["job_id"] == "refresh-1"
    assert get_latest_completed_job(settings, "refresh")["job_id"] == "refresh-1"
    assert get_latest_completed_job(settings, "sync") is None


def _navy_item(item_id: str, created_at: datetime, accounts: list[LinkedAccount]) -> LinkedItem:
    return LinkedItem(
        institution_id="ins_15",
        institution_name="Navy Federal Credit Union",
        item_id=item_id,
        access_token=f"access-{item_id}",
        accounts=accounts,
        created_at=created_at,
    )


def test_merge_duplicate_accounts_folds_relinked_item_into_newest(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    upsert_linked_item(
        settings,
        _navy_item(
            "item_old",
            datetime(2026, 7, 1, tzinfo=UTC),
            [
                LinkedAccount(
                    account_id="old_checking",
                    account_name="EveryDay Checking",
                    account_mask="8020",
                    account_type="depository",
                    account_subtype="checking",
                    alias="Checking (8020)",
                ),
                LinkedAccount(
                    account_id="old_mortgage",
                    account_name="Mortgage",
                    account_type="loan",
                    account_subtype="mortgage",
                ),
            ],
        ),
    )
    # A different login at the same institution that only shares an unmasked name.
    upsert_linked_item(
        settings,
        _navy_item(
            "item_other_login",
            datetime(2026, 7, 2, tzinfo=UTC),
            [
                LinkedAccount(
                    account_id="other_mortgage",
                    account_name="Mortgage",
                    account_type="loan",
                    account_subtype="mortgage",
                ),
            ],
        ),
    )
    upsert_linked_item(
        settings,
        _navy_item(
            "item_new",
            datetime(2026, 10, 8, tzinfo=UTC),
            [
                LinkedAccount(
                    account_id="new_checking",
                    account_name="EveryDay Checking",
                    account_mask="8020",
                    account_type="depository",
                    account_subtype="checking",
                ),
                LinkedAccount(
                    account_id="new_mortgage",
                    account_name="Mortgage",
                    account_type="loan",
                    account_subtype="mortgage",
                ),
                LinkedAccount(
                    account_id="new_savings",
                    account_name="Finn Savings",
                    account_mask="6749",
                    account_type="depository",
                    account_subtype="savings",
                ),
            ],
        ),
    )
    save_state(
        settings,
        StateFile(
            environment="sandbox",
            downloaded_statements=[
                DownloadedStatement(
                    statement_id="stmt_old",
                    institution_name="Navy Federal Credit Union",
                    account_id="old_checking",
                    account_name="Checking (8020)",
                    statement_date=date(2026, 9, 18),
                    file_path="/tmp/x.pdf",
                    dedupe_key="Navy Federal Credit Union|old_checking|stmt_old",
                )
            ],
        ),
    )
    add_event(settings, event_type="statement_downloaded", message="x", account_id="old_checking")

    removed = merge_duplicate_accounts(settings)

    assert [item.item_id for item in removed] == ["item_old"]
    assert removed[0].access_token == "access-item_old"

    config = load_configuration(settings)
    items = {item.item_id: item for item in config.linked_items}
    assert set(items) == {"item_new", "item_other_login"}
    assert [account.account_id for account in items["item_other_login"].accounts] == [
        "other_mortgage"
    ]
    new_accounts = {account.account_id: account for account in items["item_new"].accounts}
    assert set(new_accounts) == {"new_checking", "new_mortgage", "new_savings"}
    assert new_accounts["new_checking"].alias == "Checking (8020)"

    [statement] = load_state(settings).downloaded_statements
    assert statement.account_id == "new_checking"
    assert statement.dedupe_key == "Navy Federal Credit Union|new_checking|stmt_old"
    assert any(
        event["event_type"] == "statement_downloaded"
        for event in list_events(settings, account_id="new_checking")
    )

    assert merge_duplicate_accounts(settings) == []


def test_merge_duplicate_accounts_keeps_older_item_with_unmatched_accounts(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    checking = {
        "account_name": "Checking",
        "account_mask": "1708",
        "account_type": "depository",
        "account_subtype": "checking",
    }
    upsert_linked_item(
        settings,
        _navy_item(
            "item_old",
            datetime(2026, 7, 1, tzinfo=UTC),
            [
                LinkedAccount(account_id="old_checking", **checking),
                LinkedAccount(
                    account_id="old_savings", account_name="Savings", account_mask="3009"
                ),
            ],
        ),
    )
    upsert_linked_item(
        settings,
        _navy_item(
            "item_new",
            datetime(2026, 10, 8, tzinfo=UTC),
            [LinkedAccount(account_id="new_checking", **checking)],
        ),
    )

    assert merge_duplicate_accounts(settings) == []

    items = {item.item_id: item for item in load_configuration(settings).linked_items}
    assert [account.account_id for account in items["item_old"].accounts] == ["old_savings"]
    assert [account.account_id for account in items["item_new"].accounts] == ["new_checking"]
