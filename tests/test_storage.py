from __future__ import annotations

from statement_fetcher.models import LinkedAccount, LinkedItem
from statement_fetcher.settings import Settings
from statement_fetcher.storage import (
    complete_sync_job,
    create_sync_job,
    ensure_environment_files,
    fail_sync_job,
    get_sync_job,
    list_sync_jobs,
    load_configuration,
    remove_account_from_configuration,
    update_sync_job_progress,
    upsert_linked_item,
)


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
