from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from statement_fetcher.app import create_app
from statement_fetcher.settings import Settings
from statement_fetcher.storage import add_event, complete_sync_job, create_sync_job


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
