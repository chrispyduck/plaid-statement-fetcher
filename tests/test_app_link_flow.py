from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

import httpx

from statement_fetcher.app import create_app, scheduler_tick
from statement_fetcher.models import DownloadedStatement, LinkedAccount, LinkedItem, StateFile
from statement_fetcher.plaid_api import PlaidAPIError
from statement_fetcher.settings import Settings
from statement_fetcher.storage import (
    add_event,
    complete_refresh_job,
    complete_sync_job,
    create_sync_job,
    save_state,
    upsert_linked_item,
)


class FakeYodleeClient:
    def create_fastlink_session(self) -> dict[str, str]:
        return {
            "access_token": "yodlee-token",
            "fastlink_url": "https://fl4.sandbox.yodlee.com/authenticate/restserver/fastlink",
            "config_name": "Aggregation",
        }

    def get_accounts_for_provider_account(
        self, provider_account_id: str
    ) -> tuple[list[dict[str, str]], str, str, str | None]:
        assert provider_account_id == "provacc_1"
        return (
            [
                {
                    "id": 12565108,
                    "accountName": "Joint Checking",
                    "accountNumber": "xxxx9060",
                    "accountType": "CHECKING",
                    "CONTAINER": "bank",
                }
            ],
            "16445",
            "Dag Site TokenFMPA",
            "https://cdn.yodlee.com/LOGO/LOGO_16445_1_2.SVG",
        )


class FakePlaidClient:
    def create_link_token(self, origin: str | None) -> str:
        assert origin == "https://statement-fetcher.localhost"
        return "link-sandbox-token"

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        assert public_token == "public-ok"
        return "access-ok", "item-ok"

    def get_accounts(self, access_token: str) -> tuple[list[dict[str, str]], str]:
        assert access_token == "access-ok"
        return [
            {
                "account_id": "acc_1",
                "name": "Everyday Checking",
                "mask": "0001",
                "type": "depository",
                "subtype": "checking",
            }
        ], "ins_109508"

    def get_institution_name(self, institution_id: str | None) -> tuple[str, str, str | None]:
        assert institution_id == "ins_109508"
        return "ins_109508", "Chase", "ZmFrZS1sb2dv"


def run_with_client(
    app,
    test_body: Callable[[httpx.AsyncClient], Awaitable[None]],
) -> None:
    transport = httpx.ASGITransport(app=app)

    async def run_test() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await test_body(client)

    asyncio.run(run_test())


def test_link_token_and_exchange_persists_accounts(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        token_response = await client.post(
            "/api/plaid/link/token",
            json={"origin": "https://statement-fetcher.localhost"},
        )
        assert token_response.status_code == 200
        assert token_response.json() == {"link_token": "link-sandbox-token"}

        exchange_response = await client.post(
            "/api/plaid/link/exchange",
            json={"public_token": "public-ok"},
        )
        assert exchange_response.status_code == 200
        assert exchange_response.json()["status"] == "linked"
        assert exchange_response.json()["accounts_count"] == 1

        accounts_response = await client.get("/api/accounts")
        assert accounts_response.status_code == 200
        payload = accounts_response.json()
        assert len(payload) == 1
        assert payload[0]["institution_name"] == "Chase"
        assert payload[0]["institution_logo"] == "ZmFrZS1sb2dv"
        assert payload[0]["account_name"] == "Everyday Checking"

    run_with_client(app, test_body)


def test_yodlee_fastlink_session_and_link_complete(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path, yodlee_login_name="sbMem1")
    app = create_app(
        settings=settings,
        plaid_client=FakePlaidClient(),
        yodlee_client=FakeYodleeClient(),
    )

    async def test_body(client: httpx.AsyncClient) -> None:
        session_response = await client.post("/api/yodlee/fastlink/session")
        assert session_response.status_code == 200
        assert session_response.json()["access_token"] == "yodlee-token"

        complete_response = await client.post(
            "/api/yodlee/link/complete",
            json={"provider_account_id": "provacc_1"},
        )
        assert complete_response.status_code == 200
        assert complete_response.json()["accounts_count"] == 1

        accounts_response = await client.get("/api/accounts")
        rows = accounts_response.json()
        assert len(rows) == 1
        assert rows[0]["provider"] == "yodlee"
        assert rows[0]["institution_name"] == "Dag Site TokenFMPA"
        assert rows[0]["account_id"] == "12565108"

    run_with_client(app, test_body)


def test_alias_update_round_trip(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        await client.post("/api/plaid/link/exchange", json={"public_token": "public-ok"})
        update_response = await client.post(
            "/api/accounts/alias",
            json={"account_id": "acc_1", "alias": "Primary"},
        )

        assert update_response.status_code == 200
        accounts_response = await client.get("/api/accounts")
        assert accounts_response.status_code == 200
        assert accounts_response.json()[0]["alias"] == "Primary"

    run_with_client(app, test_body)


def test_account_details_and_remove(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        await client.post("/api/plaid/link/exchange", json={"public_token": "public-ok"})
        details_response = await client.get("/api/accounts/acc_1")

        assert details_response.status_code == 200
        details = details_response.json()
        assert details["account_id"] == "acc_1"
        assert details["institution_name"] == "Chase"
        assert isinstance(details["events"], list)

        remove_response = await client.delete("/api/accounts/acc_1")
        assert remove_response.status_code == 200

        missing_response = await client.get("/api/accounts/acc_1")
        assert missing_response.status_code == 404

    run_with_client(app, test_body)


def test_refresh_all_accounts(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        await client.post("/api/plaid/link/exchange", json={"public_token": "public-ok"})

        refresh_response = await client.post("/api/accounts/refresh")
        assert refresh_response.status_code == 200
        payload = refresh_response.json()
        assert payload["status"] == "refreshed"
        assert payload["refreshed_items"] == 1
        assert payload["failed_items"] == 0

    run_with_client(app, test_body)


def test_refresh_single_account(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        await client.post("/api/plaid/link/exchange", json={"public_token": "public-ok"})

        refresh_response = await client.post("/api/accounts/acc_1/refresh")
        assert refresh_response.status_code == 200
        payload = refresh_response.json()
        assert payload["status"] == "refreshed"
        assert payload["account_id"] == "acc_1"

        missing_response = await client.post("/api/accounts/missing/refresh")
        assert missing_response.status_code == 404

    run_with_client(app, test_body)


def test_service_config_update_and_read(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        update_response = await client.put(
            "/api/service/config",
            json={
                "plaid_language": "en",
                "retry_max_attempts": "7",
                "statements_start_date": "2026-01-01",
            },
        )
        assert update_response.status_code == 200

        read_response = await client.get("/api/service/config")
        assert read_response.status_code == 200
        payload = read_response.json()

        assert payload["runtime"]["plaid_language"] == "en"
        assert payload["runtime"]["retry_max_attempts"] == 7
        assert payload["runtime"]["statements_start_date"] == "2026-01-01"
        assert payload["persisted"]["plaid_language"] == "en"

    run_with_client(app, test_body)


def test_sync_history_reads_persisted_jobs(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    create_sync_job(settings, "job-123", "2026-01-01T00:00:00+00:00")
    complete_sync_job(
        settings,
        job_id="job-123",
        finished_at="2026-01-01T00:01:00+00:00",
        listed=4,
        downloaded=2,
        skipped_existing=1,
        skipped_filtered=1,
        errors=0,
    )
    add_event(
        settings,
        event_type="statement_downloaded",
        message="Statement downloaded",
        account_id="acc_1",
        job_id="job-123",
        metadata={"statement_id": "stmt_1"},
    )

    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        jobs_response = await client.get("/api/sync/jobs")
        assert jobs_response.status_code == 200
        jobs = jobs_response.json()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-123"

        status_response = await client.get("/api/sync/status/job-123")
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload["status"] == "completed"
        assert len(payload["logs"]) == 1

    run_with_client(app, test_body)


def test_downloaded_statements_endpoints(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    output_file = tmp_path / "output" / "2026-06-30, Chase, Checking, stmt_1.pdf"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(b"%PDF-1.7 fake")

    save_state(
        settings,
        StateFile(
            environment="sandbox",
            downloaded_statements=[
                DownloadedStatement(
                    statement_id="stmt_1",
                    institution_name="Chase",
                    account_id="acc_1",
                    account_name="Checking",
                    statement_date=date(2026, 6, 30),
                    file_path=str(output_file),
                    dedupe_key="Chase|acc_1|stmt_1",
                )
            ],
        ),
    )

    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        list_response = await client.get("/api/statements")
        assert list_response.status_code == 200
        statements = list_response.json()
        assert len(statements) == 1
        assert statements[0]["dedupe_key"] == "Chase|acc_1|stmt_1"
        assert statements[0]["file_exists"] is True

        download_response = await client.get("/api/statements/Chase%7Cacc_1%7Cstmt_1/download")
        assert download_response.status_code == 200
        assert download_response.content.startswith(b"%PDF-1.7")

        missing_response = await client.get("/api/statements/missing/download")
        assert missing_response.status_code == 404

    run_with_client(app, test_body)


def test_refresh_and_sync_start_endpoints_run_to_completion(tmp_path) -> None:
    # No linked items configured, so both jobs complete immediately without needing
    # the fake Plaid client to implement list_statements/refresh_statements.
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def poll_until_finished(client: httpx.AsyncClient, job_id: str) -> dict:
        for _ in range(50):
            response = await client.get(f"/api/sync/status/{job_id}")
            payload = response.json()
            if payload["status"] != "running":
                return payload
            await asyncio.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish in time")

    async def test_body(client: httpx.AsyncClient) -> None:
        refresh_response = await client.post("/api/refresh/start")
        assert refresh_response.status_code == 200
        refresh_job = await poll_until_finished(client, refresh_response.json()["job_id"])
        assert refresh_job["status"] == "completed"
        assert refresh_job["job_type"] == "refresh"
        assert refresh_job["requested"] == 0

        sync_response = await client.post("/api/sync/start", json={})
        assert sync_response.status_code == 200
        sync_job = await poll_until_finished(client, sync_response.json()["job_id"])
        assert sync_job["status"] == "completed"
        assert sync_job["job_type"] == "sync"

        jobs_response = await client.get("/api/sync/jobs")
        job_types = {job["job_type"] for job in jobs_response.json()}
        assert job_types == {"refresh", "sync"}

        refresh_only = await client.get("/api/sync/jobs", params={"job_type": "refresh"})
        assert [job["job_type"] for job in refresh_only.json()] == ["refresh"]

        summary_response = await client.get("/api/jobs/summary")
        summary = summary_response.json()
        assert summary["refresh"]["job_id"] == refresh_job["job_id"]
        assert summary["sync"]["job_id"] == sync_job["job_id"]

    run_with_client(app, test_body)


class FakeTwoItemPlaidClient:
    """Chase (ITEM_LOGIN_REQUIRED) and Citibank (healthy), linked as separate items."""

    def create_link_token(self, origin: str | None) -> str:
        return "link-token"

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        if public_token == "public-chase":
            return "access-chase", "item-chase"
        if public_token == "public-citi":
            return "access-citi", "item-citi"
        raise AssertionError(public_token)

    def get_accounts(self, access_token: str) -> tuple[list[dict[str, str]], str]:
        if access_token == "access-chase":
            return [{"account_id": "acc-chase", "name": "Chase Checking"}], "ins_56"
        if access_token == "access-citi":
            return [{"account_id": "acc-citi", "name": "Citi Checking"}], "ins_5"
        raise AssertionError(access_token)

    def get_institution_name(self, institution_id: str | None) -> tuple[str, str, str | None]:
        names = {
            "ins_56": "Chase",
            "ins_5": "Citibank Online",
        }
        return institution_id, names[institution_id], None

    def refresh_statements(self, access_token: str, start_date, end_date) -> str | None:
        return "req_1"

    def list_statements(self, access_token: str) -> dict:
        if access_token == "access-chase":
            raise PlaidAPIError(
                "Plaid API request failed",
                status_code=400,
                details={"error_code": "ITEM_LOGIN_REQUIRED", "request_id": "req_x"},
            )
        return {"institution_name": "Citibank Online", "accounts": []}

    def download_statement(self, access_token: str, statement_id: str) -> tuple[bytes, str | None]:
        raise AssertionError("no downloads expected in this test")


def test_sync_job_completes_and_flags_reauth_when_one_item_needs_login(tmp_path) -> None:
    # Regression test: a sync job used to crash entirely (status="failed") the moment
    # any single linked item's /statements/list call raised ITEM_LOGIN_REQUIRED, which
    # meant every item after the failing one in iteration order was silently skipped.
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    app = create_app(settings=settings, plaid_client=FakeTwoItemPlaidClient())

    async def poll_until_finished(client: httpx.AsyncClient, job_id: str) -> dict:
        for _ in range(50):
            response = await client.get(f"/api/sync/status/{job_id}")
            payload = response.json()
            if payload["status"] != "running":
                return payload
            await asyncio.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish in time")

    async def test_body(client: httpx.AsyncClient) -> None:
        await client.post("/api/plaid/link/exchange", json={"public_token": "public-chase"})
        await client.post("/api/plaid/link/exchange", json={"public_token": "public-citi"})

        sync_response = await client.post("/api/sync/start", json={})
        job = await poll_until_finished(client, sync_response.json()["job_id"])

        assert job["status"] == "completed"
        assert job["errors"] == 1

        event_types = {entry["event_type"] for entry in job["logs"]}
        assert "item_login_required" in event_types
        login_required_log = next(
            entry for entry in job["logs"] if entry["event_type"] == "item_login_required"
        )
        assert login_required_log["item_id"] == "item-chase"
        assert login_required_log["level"] == "error"
        assert login_required_log["metadata"]["reauth_required"] is True

        accounts_response = await client.get("/api/accounts")
        accounts_by_id = {row["account_id"]: row for row in accounts_response.json()}
        assert accounts_by_id["acc-chase"]["login_required"] is True
        assert accounts_by_id["acc-citi"]["login_required"] is False

    run_with_client(app, test_body)


def test_scheduler_tick_triggers_refresh_when_none_has_run(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    refresh_calls = []
    sync_calls = []

    scheduler_tick(
        settings,
        launch_refresh=lambda: refresh_calls.append(1),
        launch_sync=lambda: sync_calls.append(1),
    )

    assert refresh_calls == [1]
    assert sync_calls == []  # no completed refresh yet, so fetch stays put


def test_scheduler_tick_does_not_retrigger_running_refresh(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    create_sync_job(settings, "refresh-running", "2026-01-01T00:00:00+00:00", job_type="refresh")

    refresh_calls = []
    scheduler_tick(
        settings,
        launch_refresh=lambda: refresh_calls.append(1),
        launch_sync=lambda: None,
    )

    assert refresh_calls == []


def test_scheduler_tick_waits_for_fetch_after_refresh_hours(tmp_path) -> None:
    settings = Settings(
        plaid_env="sandbox",
        PSF_CONFIG_ROOT=tmp_path,
        PSF_REFRESH_INTERVAL_HOURS=999,
        PSF_FETCH_AFTER_REFRESH_HOURS=24,
    )
    now = datetime.now(UTC)
    refresh_started = (now - timedelta(hours=1)).isoformat()
    refresh_finished = (now - timedelta(hours=1)).isoformat()
    create_sync_job(settings, "refresh-1", refresh_started, job_type="refresh")
    complete_refresh_job(
        settings, job_id="refresh-1", finished_at=refresh_finished, requested=1, failed=0
    )

    sync_calls = []
    scheduler_tick(settings, launch_refresh=lambda: None, launch_sync=lambda: sync_calls.append(1))

    # Refresh only finished an hour ago; fetch shouldn't run until 24h have passed.
    assert sync_calls == []


def test_scheduler_tick_triggers_fetch_once_per_completed_refresh(tmp_path) -> None:
    settings = Settings(
        plaid_env="sandbox",
        PSF_CONFIG_ROOT=tmp_path,
        PSF_REFRESH_INTERVAL_HOURS=999,
        PSF_FETCH_AFTER_REFRESH_HOURS=24,
    )
    now = datetime.now(UTC)
    old_enough = (now - timedelta(hours=25)).isoformat()
    create_sync_job(settings, "refresh-1", old_enough, job_type="refresh")
    complete_refresh_job(
        settings, job_id="refresh-1", finished_at=old_enough, requested=1, failed=0
    )

    sync_calls = []
    scheduler_tick(settings, launch_refresh=lambda: None, launch_sync=lambda: sync_calls.append(1))
    assert sync_calls == [1]

    # Simulate that fetch actually ran (as the real endpoint would record) and confirm
    # the scheduler won't fire a second fetch for the same completed refresh.
    create_sync_job(settings, "sync-1", now.isoformat(), job_type="sync")
    complete_sync_job(
        settings,
        job_id="sync-1",
        finished_at=now.isoformat(),
        listed=0,
        downloaded=0,
        skipped_existing=0,
        skipped_filtered=0,
        errors=0,
    )

    sync_calls_second_tick = []
    scheduler_tick(
        settings,
        launch_refresh=lambda: None,
        launch_sync=lambda: sync_calls_second_tick.append(1),
    )
    assert sync_calls_second_tick == []


def test_download_statement_rejects_path_outside_output_dir(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    outside_file = tmp_path / "outside.pdf"
    outside_file.write_bytes(b"%PDF-1.7 fake")

    save_state(
        settings,
        StateFile(
            environment="sandbox",
            downloaded_statements=[
                DownloadedStatement(
                    statement_id="stmt_1",
                    institution_name="Chase",
                    account_id="acc_1",
                    account_name="Checking",
                    statement_date=date(2026, 6, 30),
                    file_path=str(outside_file),
                    dedupe_key="Chase|acc_1|outside",
                )
            ],
        ),
    )

    app = create_app(settings=settings, plaid_client=FakePlaidClient())

    async def test_body(client: httpx.AsyncClient) -> None:
        response = await client.get("/api/statements/Chase%7Cacc_1%7Coutside/download")
        assert response.status_code == 400

    run_with_client(app, test_body)


class FakeRelinkPlaidClient(FakePlaidClient):
    def __init__(self) -> None:
        self.removed_tokens: list[str] = []

    def remove_item(self, access_token: str) -> None:
        self.removed_tokens.append(access_token)


def test_exchange_merges_accounts_from_earlier_link_of_same_login(tmp_path) -> None:
    settings = Settings(plaid_env="sandbox", PSF_CONFIG_ROOT=tmp_path)
    upsert_linked_item(
        settings,
        LinkedItem(
            institution_id="ins_109508",
            institution_name="Chase",
            item_id="item-earlier",
            access_token="access-earlier",
            accounts=[
                LinkedAccount(
                    account_id="acc_earlier",
                    account_name="Everyday Checking",
                    account_mask="0001",
                    account_type="depository",
                    account_subtype="checking",
                    alias="Main Checking",
                )
            ],
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )
    plaid_client = FakeRelinkPlaidClient()
    app = create_app(settings=settings, plaid_client=plaid_client)

    async def test_body(client: httpx.AsyncClient) -> None:
        exchange_response = await client.post(
            "/api/plaid/link/exchange",
            json={"public_token": "public-ok"},
        )
        assert exchange_response.status_code == 200

        accounts_response = await client.get("/api/accounts")
        payload = accounts_response.json()
        assert [row["account_id"] for row in payload] == ["acc_1"]
        assert payload[0]["item_id"] == "item-ok"
        assert payload[0]["alias"] == "Main Checking"

    run_with_client(app, test_body)
    assert plaid_client.removed_tokens == ["access-earlier"]
