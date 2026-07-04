from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .models import LinkedAccount, LinkedItem
from .plaid_api import PlaidAPIError, PlaidClient
from .settings import Settings
from .storage import (
    add_event,
    complete_sync_job,
    create_sync_job,
    delete_service_configuration_keys,
    ensure_environment_files,
    fail_sync_job,
    get_account_details,
    get_service_configuration,
    get_sync_job,
    list_events,
    load_configuration,
    remove_account_from_configuration,
    set_account_alias,
    set_service_configuration,
    update_sync_job_progress,
    upsert_linked_item,
)
from .storage import (
    list_sync_jobs as list_persisted_sync_jobs,
)
from .sync import SyncSummary, sync_statements

logger = logging.getLogger(__name__)


ServiceSettingValue = str | int | float | date | None
ServiceSettingParser = Callable[[str], ServiceSettingValue]


class AliasUpdateRequest(BaseModel):
    account_id: str
    alias: str


class LinkTokenRequest(BaseModel):
    origin: str | None = None


class LinkExchangeRequest(BaseModel):
    public_token: str


class SyncStartRequest(BaseModel):
    dry_run: bool = False
    since: str | None = None
    account_id: str | None = None
    max_downloads: int | None = None


class SyncJobState(BaseModel):
    job_id: str
    status: str
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    listed: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    skipped_filtered: int = 0
    errors: int = 0
    logs: list[dict[str, object]] = Field(default_factory=list)


class ServiceConfigUpdateRequest(BaseModel):
    plaid_language: str | None = None
    plaid_country_codes: str | None = None
    plaid_products: str | None = None
    plaid_redirect_uri: str | None = None
    retry_max_attempts: int | str | None = None
    retry_base_delay_seconds: float | str | None = None
    retry_max_delay_seconds: float | str | None = None
    statements_start_date: str | None = None
    statements_end_date: str | None = None


class AppContext:
    def __init__(self, settings: Settings, plaid_client: PlaidClient | None = None) -> None:
        self.settings = settings
        self.settings.load_credentials_fallback()
        self.plaid = plaid_client or PlaidClient(self.settings)
        self.default_service_config: dict[str, ServiceSettingValue] = {
            "plaid_language": self.settings.plaid_language,
            "plaid_country_codes": self.settings.plaid_country_codes,
            "plaid_products": self.settings.plaid_products,
            "plaid_redirect_uri": self.settings.plaid_redirect_uri,
            "retry_max_attempts": self.settings.retry_max_attempts,
            "retry_base_delay_seconds": self.settings.retry_base_delay_seconds,
            "retry_max_delay_seconds": self.settings.retry_max_delay_seconds,
            "statements_start_date": self.settings.statements_start_date,
            "statements_end_date": self.settings.statements_end_date,
        }


SERVICE_CONFIG_KEYS: dict[str, ServiceSettingParser] = {
    "plaid_language": str,
    "plaid_country_codes": str,
    "plaid_products": str,
    "plaid_redirect_uri": str,
    "retry_max_attempts": int,
    "retry_base_delay_seconds": float,
    "retry_max_delay_seconds": float,
    "statements_start_date": date.fromisoformat,
    "statements_end_date": date.fromisoformat,
}


def _apply_service_overrides(ctx: AppContext) -> None:
    for key, value in ctx.default_service_config.items():
        setattr(ctx.settings, key, value)

    overrides = get_service_configuration(ctx.settings)
    for key, value in overrides.items():
        caster = SERVICE_CONFIG_KEYS.get(key)
        if caster is None:
            continue
        try:
            cast_value: ServiceSettingValue = caster(value)
        except ValueError:
            logger.warning("Invalid persisted service config key=%s value=%s", key, value)
            continue
        setattr(ctx.settings, key, cast_value)


def _runtime_service_config(ctx: AppContext) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for key in SERVICE_CONFIG_KEYS:
        value = getattr(ctx.settings, key)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        config[key] = value
    return config


def _plaid_http_exception(exc: PlaidAPIError) -> HTTPException:
    details = exc.details or {}
    message = details.get("error_message") or str(exc)
    payload: dict[str, str | int | bool | None] = {
        "message": message,
        "status_code": exc.status_code,
        "retriable": exc.retriable,
        "error_code": details.get("error_code"),
        "error_type": details.get("error_type"),
        "request_id": details.get("request_id"),
        "documentation_url": details.get("documentation_url"),
    }
    return HTTPException(status_code=400, detail=payload)


def _map_plaid_accounts(accounts: list[dict[str, Any]]) -> list[LinkedAccount]:
    mapped: list[LinkedAccount] = []
    for account in accounts:
        mapped.append(
            LinkedAccount(
                account_id=account["account_id"],
                account_name=(
                    account.get("name") or account.get("official_name") or "Unnamed Account"
                ),
                account_mask=account.get("mask"),
                account_type=account.get("type"),
                account_subtype=account.get("subtype"),
            )
        )
    return mapped


def create_app(
    settings: Settings | None = None,
    plaid_client: PlaidClient | None = None,
) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    resolved_settings = settings or Settings()
    ctx = AppContext(resolved_settings, plaid_client=plaid_client)
    _apply_service_overrides(ctx)
    logger.info("App startup env=%s", ctx.settings.plaid_env)

    app = FastAPI(title="Statement Fetcher", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "https://localhost:5173",
            "https://127.0.0.1:5173",
            "https://statement-fetcher.localhost:5173",
            "https://statement-fetcher.localhost:8765",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    frontend_dist = Path("frontend/dist")
    frontend_index = frontend_dist / "index.html"
    frontend_assets = frontend_dist / "assets"

    def refresh_linked_item(linked_item: LinkedItem) -> dict[str, str | int]:
        accounts, institution_id = ctx.plaid.get_accounts(linked_item.access_token)
        resolved_id, institution_name, institution_logo = ctx.plaid.get_institution_name(
            institution_id or linked_item.institution_id,
        )
        refreshed_item = LinkedItem(
            institution_id=resolved_id,
            institution_name=institution_name,
            institution_logo=institution_logo,
            item_id=linked_item.item_id,
            access_token=linked_item.access_token,
            accounts=_map_plaid_accounts(accounts),
        )
        upsert_linked_item(ctx.settings, refreshed_item)
        return {
            "item_id": refreshed_item.item_id,
            "institution_id": refreshed_item.institution_id,
            "institution_name": refreshed_item.institution_name,
            "accounts_count": len(refreshed_item.accounts),
        }

    def _frontend_entry() -> FileResponse | dict[str, str]:
        if frontend_index.exists():
            return FileResponse(frontend_index)
        return {
            "message": "Frontend not built. Run frontend dev server or build frontend/dist.",
        }

    @app.get("/", response_model=None)
    def home():
        return _frontend_entry()

    @app.get("/sync", response_model=None)
    def sync_page():
        return _frontend_entry()

    @app.get("/service-config", response_model=None)
    def service_config_page():
        return _frontend_entry()

    @app.get("/accounts/{account_id}", response_model=None)
    def account_details_page(account_id: str):
        _ = account_id
        return _frontend_entry()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        ensure_environment_files(ctx.settings)
        return {"status": "ok", "env": ctx.settings.plaid_env}

    @app.get("/api/accounts")
    def list_accounts() -> list[dict[str, str | None]]:
        config = load_configuration(ctx.settings)
        rows: list[dict[str, str | None]] = []
        for item in config.linked_items:
            for account in item.accounts:
                rows.append(
                    {
                        "institution_id": item.institution_id,
                        "institution_name": item.institution_name,
                        "institution_logo": item.institution_logo,
                        "item_id": item.item_id,
                        "account_id": account.account_id,
                        "account_name": account.account_name,
                        "alias": account.alias,
                    }
                )
        return rows

    @app.post("/api/accounts/refresh")
    def refresh_accounts() -> dict[str, Any]:
        config = load_configuration(ctx.settings)
        results: list[dict[str, str | int]] = []
        failed: list[dict[str, str]] = []

        for linked_item in config.linked_items:
            try:
                results.append(refresh_linked_item(linked_item))
            except PlaidAPIError as exc:
                logger.exception("Refresh failed for item_id=%s", linked_item.item_id)
                details = exc.details or {}
                message = str(details.get("error_message") or str(exc))
                failed.append({"item_id": linked_item.item_id, "error": message})

        status = "refreshed"
        if failed and results:
            status = "partially_refreshed"
        if failed and not results:
            status = "failed"

        return {
            "status": status,
            "refreshed_items": len(results),
            "failed_items": len(failed),
            "items": results,
            "errors": failed,
        }

    @app.post("/api/accounts/{account_id}/refresh")
    def refresh_account(account_id: str) -> dict[str, Any]:
        config = load_configuration(ctx.settings)
        linked_item = next(
            (
                item
                for item in config.linked_items
                if any(account.account_id == account_id for account in item.accounts)
            ),
            None,
        )
        if linked_item is None:
            raise HTTPException(status_code=404, detail="account_id not found")

        try:
            refreshed = refresh_linked_item(linked_item)
        except PlaidAPIError as exc:
            logger.exception("Refresh failed for account_id=%s", account_id)
            raise _plaid_http_exception(exc) from exc

        return {
            "status": "refreshed",
            "account_id": account_id,
            "item": refreshed,
        }

    @app.get("/api/accounts/{account_id}")
    def account_details(account_id: str) -> dict[str, Any]:
        details = get_account_details(ctx.settings, account_id)
        if details is None:
            raise HTTPException(status_code=404, detail="account_id not found")
        details["events"] = list_events(ctx.settings, account_id=account_id, limit=100)
        return details

    @app.delete("/api/accounts/{account_id}")
    def remove_account(account_id: str) -> dict[str, str]:
        changed = remove_account_from_configuration(ctx.settings, account_id)
        if not changed:
            raise HTTPException(status_code=404, detail="account_id not found")
        logger.info("Account removed account_id=%s", account_id)
        return {"status": "removed", "account_id": account_id}

    @app.post("/api/accounts/alias")
    def set_alias(payload: AliasUpdateRequest) -> dict[str, str]:
        logger.info("Alias update requested account_id=%s", payload.account_id)
        updated = set_account_alias(ctx.settings, payload.account_id, payload.alias.strip() or None)
        if not updated:
            logger.warning(
                "Alias update failed account not found account_id=%s",
                payload.account_id,
            )
            raise HTTPException(status_code=404, detail="account_id not found")
        logger.info("Alias updated account_id=%s", payload.account_id)
        return {"status": "updated", "account_id": payload.account_id}

    @app.get("/api/service/config")
    def service_config() -> dict[str, Any]:
        return {
            "environment": ctx.settings.plaid_env,
            "runtime": _runtime_service_config(ctx),
            "persisted": get_service_configuration(ctx.settings),
        }

    @app.put("/api/service/config")
    def update_service_config(payload: ServiceConfigUpdateRequest) -> dict[str, Any]:
        updates: dict[str, str] = {}
        clears: list[str] = []

        for key in SERVICE_CONFIG_KEYS:
            value = getattr(payload, key)
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                clears.append(key)
                continue
            caster = SERVICE_CONFIG_KEYS[key]
            try:
                caster(str(value))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"invalid value for {key}") from exc
            updates[key] = str(value)

        if updates:
            set_service_configuration(ctx.settings, updates)
        if clears:
            delete_service_configuration_keys(ctx.settings, clears)

        _apply_service_overrides(ctx)
        if plaid_client is None:
            ctx.plaid = PlaidClient(ctx.settings)

        add_event(
            ctx.settings,
            event_type="service_config_updated",
            message="Service configuration updated",
            metadata={"updated_keys": sorted(updates.keys()), "cleared_keys": sorted(clears)},
        )

        return {
            "status": "updated",
            "runtime": _runtime_service_config(ctx),
            "persisted": get_service_configuration(ctx.settings),
        }

    @app.post("/api/plaid/link/token")
    def create_link_token(payload: LinkTokenRequest) -> dict[str, str]:
        logger.info("Create link token requested origin=%s", payload.origin)
        try:
            link_token = ctx.plaid.create_link_token(payload.origin)
        except PlaidAPIError as exc:
            logger.exception("Create link token failed")
            raise _plaid_http_exception(exc) from exc
        return {"link_token": link_token}

    @app.post("/api/plaid/link/exchange")
    def exchange_link_token(payload: LinkExchangeRequest) -> dict[str, str | int]:
        logger.info("Exchange public token requested")
        try:
            access_token, item_id = ctx.plaid.exchange_public_token(payload.public_token)
            accounts, institution_id = ctx.plaid.get_accounts(access_token)
            institution_id, institution_name, institution_logo = ctx.plaid.get_institution_name(
                institution_id,
            )
        except PlaidAPIError as exc:
            logger.exception("Exchange public token failed")
            raise _plaid_http_exception(exc) from exc

        linked_item = LinkedItem(
            institution_id=institution_id,
            institution_name=institution_name,
            institution_logo=institution_logo,
            item_id=item_id,
            access_token=access_token,
            accounts=_map_plaid_accounts(accounts),
        )
        upsert_linked_item(ctx.settings, linked_item)
        logger.info(
            "Linked item stored item_id=%s institution=%s accounts=%s",
            item_id,
            institution_name,
            len(linked_item.accounts),
        )
        return {
            "status": "linked",
            "item_id": item_id,
            "accounts_count": len(linked_item.accounts),
        }

    @app.post("/api/sync/start")
    def start_sync(payload: SyncStartRequest) -> dict[str, str]:
        job_id = str(uuid4())
        started_at = datetime.now(UTC).isoformat()
        create_sync_job(ctx.settings, job_id, started_at)
        add_event(
            ctx.settings,
            event_type="sync_started",
            message="Sync job started",
            job_id=job_id,
            metadata={
                "dry_run": payload.dry_run,
                "since": payload.since,
                "account_id": payload.account_id,
                "max_downloads": payload.max_downloads,
            },
        )
        logger.info(
            "Sync job started job_id=%s dry_run=%s since=%s account_id=%s max_downloads=%s",
            job_id,
            payload.dry_run,
            payload.since,
            payload.account_id,
            payload.max_downloads,
        )

        def run_sync() -> None:
            from datetime import date

            since_date = date.fromisoformat(payload.since) if payload.since else None

            def on_progress(summary: SyncSummary) -> None:
                update_sync_job_progress(
                    ctx.settings,
                    job_id=job_id,
                    listed=summary.listed,
                    downloaded=summary.downloaded,
                    skipped_existing=summary.skipped_existing,
                    skipped_filtered=summary.skipped_filtered,
                    errors=summary.errors,
                )

            try:
                summary = sync_statements(
                    ctx.settings,
                    dry_run=payload.dry_run,
                    since=since_date,
                    account_id=payload.account_id,
                    max_downloads=payload.max_downloads,
                    progress_callback=on_progress,
                    event_callback=lambda event_type, message, metadata: add_event(
                        ctx.settings,
                        event_type=event_type,
                        message=message,
                        level=(
                            "debug"
                            if event_type == "statement_existing"
                            else "info"
                        ),
                        account_id=(
                            str(metadata["account_id"])
                            if metadata and metadata.get("account_id")
                            else None
                        ),
                        job_id=job_id,
                        metadata=metadata,
                    ),
                )
                finished_at = datetime.now(UTC).isoformat()
                complete_sync_job(
                    ctx.settings,
                    job_id=job_id,
                    finished_at=finished_at,
                    listed=summary.listed,
                    downloaded=summary.downloaded,
                    skipped_existing=summary.skipped_existing,
                    skipped_filtered=summary.skipped_filtered,
                    errors=summary.errors,
                )
                add_event(
                    ctx.settings,
                    event_type="sync_completed",
                    message="Sync job completed",
                    job_id=job_id,
                    metadata={
                        "listed": summary.listed,
                        "downloaded": summary.downloaded,
                        "skipped_existing": summary.skipped_existing,
                        "skipped_filtered": summary.skipped_filtered,
                        "errors": summary.errors,
                    },
                )
                logger.info(
                    "Sync job completed job_id=%s listed=%s downloaded=%s errors=%s",
                    job_id,
                    summary.listed,
                    summary.downloaded,
                    summary.errors,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Sync job failed job_id=%s", job_id)
                fail_sync_job(
                    ctx.settings,
                    job_id=job_id,
                    finished_at=datetime.now(UTC).isoformat(),
                    error=str(exc),
                )
                add_event(
                    ctx.settings,
                    event_type="sync_failed",
                    message="Sync job failed",
                    level="error",
                    job_id=job_id,
                    metadata={"error": str(exc)},
                )

        Thread(target=run_sync, daemon=True).start()
        return {"job_id": job_id}

    @app.get("/api/sync/status/{job_id}")
    def get_sync_status(job_id: str) -> SyncJobState:
        row = get_sync_job(ctx.settings, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="sync job not found")
        job = SyncJobState.model_validate(row)
        job.logs = list_events(ctx.settings, job_id=job_id, limit=250)
        return job

    @app.get("/api/sync/jobs")
    def list_sync_jobs() -> list[SyncJobState]:
        jobs = [SyncJobState.model_validate(row) for row in list_persisted_sync_jobs(ctx.settings)]
        for job in jobs:
            job.logs = []
        return sorted(jobs, key=lambda value: value.started_at, reverse=True)

    @app.get("/api/events")
    def query_events(
        account_id: str | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return list_events(ctx.settings, account_id=account_id, job_id=job_id, limit=300)

    @app.get("/plaid/callback")
    def plaid_callback() -> dict[str, str]:
        # Link web flow handles token exchange via frontend onSuccess callback.
        return {"status": "ok"}

    if frontend_assets.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(frontend_assets), html=False),
            name="frontend-assets",
        )

    if frontend_dist.exists():
        app.mount("/app", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

        @app.get("/app/{path:path}")
        def frontend_spa_fallback(path: str) -> FileResponse:
            _ = path
            return FileResponse(frontend_dist / "index.html")

    return app
