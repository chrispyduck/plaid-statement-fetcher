from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .logging_utils import ContextualFormatter, set_log_context
from .models import LinkedAccount, LinkedItem
from .plaid_api import PlaidAPIError, PlaidClient
from .provider_errors import ProviderAPIError
from .settings import Settings
from .storage import (
    add_event,
    complete_refresh_job,
    complete_sync_job,
    create_sync_job,
    delete_service_configuration_keys,
    ensure_environment_files,
    fail_sync_job,
    get_account_details,
    get_downloaded_statement_by_key,
    get_latest_completed_job,
    get_latest_job,
    get_service_configuration,
    get_sync_job,
    has_running_job,
    list_downloaded_statements,
    list_events,
    load_configuration,
    remove_account_from_configuration,
    set_account_alias,
    set_item_login_required,
    set_service_configuration,
    update_refresh_job_progress,
    update_sync_job_progress,
    upsert_linked_item,
)
from .storage import (
    list_sync_jobs as list_persisted_sync_jobs,
)
from .sync import RefreshSummary, SyncSummary, refresh_statements, sync_statements
from .yodlee_api import YodleeAPIError, YodleeClient

logger = logging.getLogger(__name__)

# How often the scheduler wakes up to check whether a refresh or fetch job is due.
SCHEDULER_POLL_SECONDS = 300


ServiceSettingValue = str | int | float | date | None
ServiceSettingParser = Callable[[str], ServiceSettingValue]


class AliasUpdateRequest(BaseModel):
    account_id: str
    alias: str


class LinkTokenRequest(BaseModel):
    origin: str | None = None
    item_id: str | None = None


class LinkExchangeRequest(BaseModel):
    public_token: str


class YodleeLinkCompleteRequest(BaseModel):
    provider_account_id: str


class SyncStartRequest(BaseModel):
    dry_run: bool = False
    since: str | None = None
    account_id: str | None = None
    max_downloads: int | None = None


class SyncJobState(BaseModel):
    job_id: str
    job_type: str = "sync"
    status: str
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    listed: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    skipped_filtered: int = 0
    errors: int = 0
    requested: int | None = None
    failed: int | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list)


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
    def __init__(
        self,
        settings: Settings,
        plaid_client: PlaidClient | None = None,
        yodlee_client: YodleeClient | None = None,
    ) -> None:
        self.settings = settings
        self.settings.load_credentials_fallback()
        self.plaid = plaid_client or PlaidClient(self.settings)
        self.yodlee = yodlee_client or YodleeClient(self.settings)
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


def _provider_http_exception(exc: ProviderAPIError) -> HTTPException:
    if isinstance(exc, YodleeAPIError):
        return _yodlee_http_exception(exc)
    if isinstance(exc, PlaidAPIError):
        return _plaid_http_exception(exc)
    return HTTPException(status_code=400, detail={"message": str(exc)})


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


def _map_yodlee_accounts(accounts: list[dict[str, Any]]) -> list[LinkedAccount]:
    mapped: list[LinkedAccount] = []
    for account in accounts:
        account_number = str(account.get("accountNumber") or "")
        mapped.append(
            LinkedAccount(
                account_id=str(account["id"]),
                account_name=account.get("accountName") or "Unnamed Account",
                account_mask=account_number[-4:] if account_number else None,
                account_type=account.get("accountType"),
                account_subtype=account.get("CONTAINER"),
            )
        )
    return mapped


def _yodlee_http_exception(exc: YodleeAPIError) -> HTTPException:
    details = exc.details or {}
    message = details.get("errorMessage") or str(exc)
    payload: dict[str, str | int | bool | None] = {
        "message": message,
        "status_code": exc.status_code,
        "retriable": exc.retriable,
        "error_code": details.get("errorCode"),
    }
    return HTTPException(status_code=400, detail=payload)


def scheduler_tick(
    settings: Settings,
    *,
    launch_refresh: Callable[[], Any],
    launch_sync: Callable[[], Any],
) -> None:
    """Run refresh weekly, and run fetch once ~24h after each refresh completes.

    Refresh and fetch are deliberately on independent clocks: refresh just needs to
    happen regularly so Plaid has time to pick up newly-posted statements, and fetch
    should trail it by enough time for that to actually finish (Plaid gives no
    completion signal short of a webhook), not run on its own weekly clock that could
    race ahead of a refresh that's still in flight. `launch_refresh`/`launch_sync` are
    injected so this can be tested without spawning real background jobs.
    """
    now = datetime.now(UTC)

    if not has_running_job(settings, "refresh"):
        last_refresh = get_latest_job(settings, "refresh")
        refresh_due = last_refresh is None or (
            now - datetime.fromisoformat(last_refresh["started_at"])
            >= timedelta(hours=settings.refresh_interval_hours)
        )
        if refresh_due:
            logger.info("Scheduler triggering statement refresh")
            launch_refresh()

    if not has_running_job(settings, "sync"):
        last_refresh_completed = get_latest_completed_job(settings, "refresh")
        if last_refresh_completed and last_refresh_completed["finished_at"]:
            refresh_done_at = datetime.fromisoformat(last_refresh_completed["finished_at"])
            last_sync = get_latest_job(settings, "sync")
            already_ran_after_refresh = last_sync is not None and (
                datetime.fromisoformat(last_sync["started_at"]) >= refresh_done_at
            )
            hours_since_refresh = now - refresh_done_at
            fetch_due = (
                not already_ran_after_refresh
                and hours_since_refresh >= timedelta(hours=settings.fetch_after_refresh_hours)
            )
            if fetch_due:
                logger.info("Scheduler triggering statement fetch")
                launch_sync()


def create_app(
    settings: Settings | None = None,
    plaid_client: PlaidClient | None = None,
    yodlee_client: YodleeClient | None = None,
    *,
    enable_scheduler: bool = False,
) -> FastAPI:
    if not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            ContextualFormatter("%(asctime)s %(levelname)s %(name)s %(context)s%(message)s")
        )
        logging.basicConfig(level=logging.INFO, handlers=[handler])

    resolved_settings = settings or Settings()
    ctx = AppContext(resolved_settings, plaid_client=plaid_client, yodlee_client=yodlee_client)
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
        # This runs on a request-handling thread pulled from FastAPI's shared
        # threadpool, which gets reused across unrelated requests -- unlike the
        # dedicated per-job threads sync/refresh jobs run on -- so the log context set
        # here must be cleared before returning to avoid it bleeding into whatever the
        # next request on this thread logs.
        set_log_context(item_id=linked_item.item_id, institution=linked_item.institution_name)
        try:
            logger.info(
                "Refreshing linked item item_id=%s institution=%s provider=%s",
                linked_item.item_id,
                linked_item.institution_name,
                linked_item.provider,
            )
            try:
                if linked_item.provider == "yodlee":
                    accounts, resolved_id, institution_name, institution_logo = (
                        ctx.yodlee.get_accounts_for_provider_account(linked_item.item_id)
                    )
                    mapped_accounts = _map_yodlee_accounts(accounts)
                else:
                    plaid_accounts, institution_id = ctx.plaid.get_accounts(
                        linked_item.access_token
                    )
                    resolved_id, institution_name, institution_logo = (
                        ctx.plaid.get_institution_name(institution_id or linked_item.institution_id)
                    )
                    mapped_accounts = _map_plaid_accounts(plaid_accounts)
            except (PlaidAPIError, YodleeAPIError) as exc:
                details = exc.details or {}
                reauth_required = details.get("error_code") == "ITEM_LOGIN_REQUIRED"
                logger.error(
                    "Refreshing linked item failed item_id=%s institution=%s "
                    "reauth_required=%s: %s",
                    linked_item.item_id,
                    linked_item.institution_name,
                    reauth_required,
                    exc,
                )
                if reauth_required:
                    set_item_login_required(ctx.settings, linked_item.item_id, required=True)
                    add_event(
                        ctx.settings,
                        event_type="item_login_required",
                        message="Institution requires reauthentication before statements can sync",
                        level="error",
                        item_id=linked_item.item_id,
                        metadata={
                            "institution_name": linked_item.institution_name,
                            "error_code": details.get("error_code"),
                            "reauth_required": True,
                        },
                    )
                raise
            refreshed_item = LinkedItem(
                provider=linked_item.provider,
                institution_id=resolved_id,
                institution_name=institution_name,
                institution_logo=institution_logo,
                item_id=linked_item.item_id,
                access_token=linked_item.access_token,
                accounts=mapped_accounts,
            )
            upsert_linked_item(ctx.settings, refreshed_item)
            logger.info(
                "Refreshed linked item item_id=%s institution=%s accounts=%s",
                refreshed_item.item_id,
                refreshed_item.institution_name,
                len(refreshed_item.accounts),
            )
            return {
                "item_id": refreshed_item.item_id,
                "institution_id": refreshed_item.institution_id,
                "institution_name": refreshed_item.institution_name,
                "accounts_count": len(refreshed_item.accounts),
            }
        finally:
            set_log_context()

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
    def list_accounts() -> list[dict[str, str | bool | None]]:
        config = load_configuration(ctx.settings)
        rows: list[dict[str, str | bool | None]] = []
        for item in config.linked_items:
            for account in item.accounts:
                rows.append(
                    {
                        "provider": item.provider,
                        "institution_id": item.institution_id,
                        "institution_name": item.institution_name,
                        "institution_logo": item.institution_logo,
                        "item_id": item.item_id,
                        "account_id": account.account_id,
                        "account_name": account.account_name,
                        "alias": account.alias,
                        "login_required": item.login_required,
                        "login_required_at": (
                            item.login_required_at.isoformat()
                            if item.login_required_at
                            else None
                        ),
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
            except ProviderAPIError as exc:
                logger.exception("Refresh failed for item_id=%s", linked_item.item_id)
                details = exc.details or {}
                message = str(details.get("error_message") or details.get("errorMessage") or exc)
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
        except ProviderAPIError as exc:
            logger.exception("Refresh failed for account_id=%s", account_id)
            raise _provider_http_exception(exc) from exc

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
        logger.info(
            "Create link token requested origin=%s item_id=%s",
            payload.origin,
            payload.item_id,
        )
        try:
            if payload.item_id:
                config = load_configuration(ctx.settings)
                linked_item = next(
                    (item for item in config.linked_items if item.item_id == payload.item_id),
                    None,
                )
                if linked_item is None:
                    raise HTTPException(status_code=404, detail="item_id not found")
                link_token = ctx.plaid.create_link_token(
                    payload.origin,
                    access_token=linked_item.access_token,
                )
            else:
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

    @app.post("/api/yodlee/fastlink/session")
    def create_yodlee_fastlink_session() -> dict[str, str]:
        logger.info("Create Yodlee FastLink session requested")
        try:
            session = ctx.yodlee.create_fastlink_session()
        except YodleeAPIError as exc:
            logger.exception("Create Yodlee FastLink session failed")
            raise _yodlee_http_exception(exc) from exc
        return session

    @app.post("/api/yodlee/link/complete")
    def complete_yodlee_link(payload: YodleeLinkCompleteRequest) -> dict[str, str | int]:
        logger.info(
            "Completing Yodlee link provider_account_id=%s",
            payload.provider_account_id,
        )
        try:
            accounts, provider_id, provider_name, provider_logo = (
                ctx.yodlee.get_accounts_for_provider_account(payload.provider_account_id)
            )
        except YodleeAPIError as exc:
            logger.exception("Completing Yodlee link failed")
            raise _yodlee_http_exception(exc) from exc

        if not ctx.settings.yodlee_login_name:
            raise HTTPException(status_code=500, detail="Yodlee login name is not configured")

        linked_item = LinkedItem(
            provider="yodlee",
            institution_id=provider_id,
            institution_name=provider_name,
            institution_logo=provider_logo,
            item_id=payload.provider_account_id,
            access_token=ctx.settings.yodlee_login_name,
            accounts=_map_yodlee_accounts(accounts),
        )
        upsert_linked_item(ctx.settings, linked_item)
        logger.info(
            "Linked Yodlee item stored item_id=%s institution=%s accounts=%s",
            linked_item.item_id,
            provider_name,
            len(linked_item.accounts),
        )
        return {
            "status": "linked",
            "item_id": linked_item.item_id,
            "accounts_count": len(linked_item.accounts),
        }

    def _launch_sync_job(payload: SyncStartRequest, *, trigger: str = "manual") -> str:
        job_id = str(uuid4())
        started_at = datetime.now(UTC).isoformat()
        create_sync_job(ctx.settings, job_id, started_at, job_type="sync")
        add_event(
            ctx.settings,
            event_type="sync_started",
            message="Sync job started",
            job_id=job_id,
            metadata={
                "trigger": trigger,
                "dry_run": payload.dry_run,
                "since": payload.since,
                "account_id": payload.account_id,
                "max_downloads": payload.max_downloads,
            },
        )
        logger.info(
            "Sync job started job_id=%s trigger=%s dry_run=%s since=%s account_id=%s "
            "max_downloads=%s",
            job_id,
            trigger,
            payload.dry_run,
            payload.since,
            payload.account_id,
            payload.max_downloads,
        )

        def run_sync() -> None:
            set_log_context(job_id=job_id)
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
                    plaid_client=ctx.plaid,
                    yodlee_client=ctx.yodlee,
                    job_id=job_id,
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
                            else "error"
                            if event_type in ("item_login_required", "item_list_failed")
                            else "info"
                        ),
                        account_id=(
                            str(metadata["account_id"])
                            if metadata and metadata.get("account_id")
                            else None
                        ),
                        item_id=(
                            str(metadata["item_id"])
                            if metadata and metadata.get("item_id")
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
        return job_id

    def _launch_refresh_job(*, trigger: str = "manual") -> str:
        job_id = str(uuid4())
        started_at = datetime.now(UTC).isoformat()
        create_sync_job(ctx.settings, job_id, started_at, job_type="refresh")
        add_event(
            ctx.settings,
            event_type="refresh_started",
            message="Statement refresh job started",
            job_id=job_id,
            metadata={"trigger": trigger},
        )
        logger.info("Refresh job started job_id=%s trigger=%s", job_id, trigger)

        def run_refresh() -> None:
            set_log_context(job_id=job_id)

            def on_progress(summary: RefreshSummary) -> None:
                update_refresh_job_progress(
                    ctx.settings,
                    job_id=job_id,
                    requested=summary.requested,
                    failed=summary.failed,
                )

            try:
                summary = refresh_statements(
                    ctx.settings,
                    plaid_client=ctx.plaid,
                    yodlee_client=ctx.yodlee,
                    job_id=job_id,
                    progress_callback=on_progress,
                    event_callback=lambda event_type, message, metadata: add_event(
                        ctx.settings,
                        event_type=event_type,
                        message=message,
                        level=(
                            "error"
                            if event_type == "statement_refresh_failed"
                            and metadata
                            and metadata.get("reauth_required")
                            else "warning"
                            if event_type == "statement_refresh_failed"
                            else "info"
                        ),
                        item_id=(
                            str(metadata["item_id"])
                            if metadata and metadata.get("item_id")
                            else None
                        ),
                        job_id=job_id,
                        metadata=metadata,
                    ),
                )
                finished_at = datetime.now(UTC).isoformat()
                complete_refresh_job(
                    ctx.settings,
                    job_id=job_id,
                    finished_at=finished_at,
                    requested=summary.requested,
                    failed=summary.failed,
                )
                add_event(
                    ctx.settings,
                    event_type="refresh_completed",
                    message="Statement refresh job completed",
                    job_id=job_id,
                    metadata={"requested": summary.requested, "failed": summary.failed},
                )
                logger.info(
                    "Refresh job completed job_id=%s requested=%s failed=%s",
                    job_id,
                    summary.requested,
                    summary.failed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Refresh job failed job_id=%s", job_id)
                fail_sync_job(
                    ctx.settings,
                    job_id=job_id,
                    finished_at=datetime.now(UTC).isoformat(),
                    error=str(exc),
                )
                add_event(
                    ctx.settings,
                    event_type="refresh_failed",
                    message="Statement refresh job failed",
                    level="error",
                    job_id=job_id,
                    metadata={"error": str(exc)},
                )

        Thread(target=run_refresh, daemon=True).start()
        return job_id

    @app.post("/api/sync/start")
    def start_sync(payload: SyncStartRequest) -> dict[str, str]:
        return {"job_id": _launch_sync_job(payload, trigger="manual")}

    @app.post("/api/refresh/start")
    def start_refresh() -> dict[str, str]:
        return {"job_id": _launch_refresh_job(trigger="manual")}

    @app.get("/api/sync/status/{job_id}")
    def get_sync_status(job_id: str) -> SyncJobState:
        row = get_sync_job(ctx.settings, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="sync job not found")
        job = SyncJobState.model_validate(row)
        job.logs = list_events(ctx.settings, job_id=job_id, limit=250)
        return job

    @app.get("/api/sync/jobs")
    def list_sync_jobs(job_type: str | None = None) -> list[SyncJobState]:
        jobs = [
            SyncJobState.model_validate(row)
            for row in list_persisted_sync_jobs(ctx.settings, job_type=job_type)
        ]
        for job in jobs:
            job.logs = []
        return sorted(jobs, key=lambda value: value.started_at, reverse=True)

    @app.get("/api/jobs/summary")
    def jobs_summary() -> dict[str, Any]:
        return {
            "refresh": get_latest_job(ctx.settings, "refresh"),
            "sync": get_latest_job(ctx.settings, "sync"),
        }

    @app.get("/api/events")
    def query_events(
        account_id: str | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return list_events(ctx.settings, account_id=account_id, job_id=job_id, limit=300)

    @app.get("/api/statements")
    def list_statements(account_id: str | None = None) -> list[dict[str, Any]]:
        return list_downloaded_statements(ctx.settings, account_id=account_id, limit=1000)

    @app.get("/api/statements/{dedupe_key}/download")
    def download_statement(dedupe_key: str) -> FileResponse:
        statement = get_downloaded_statement_by_key(ctx.settings, dedupe_key)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")

        raw_path = Path(str(statement["file_path"]))
        file_path = raw_path.expanduser().resolve()
        output_root = ctx.settings.output_dir.expanduser().resolve()
        if output_root not in file_path.parents:
            raise HTTPException(status_code=400, detail="invalid statement path")
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="statement file missing")

        return FileResponse(
            file_path,
            media_type="application/pdf",
            filename=str(statement["file_name"]),
        )

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

    if enable_scheduler:

        scheduled_sync_payload = SyncStartRequest()

        def _scheduler_loop() -> None:
            while True:
                try:
                    scheduler_tick(
                        ctx.settings,
                        launch_refresh=lambda: _launch_refresh_job(trigger="scheduled"),
                        launch_sync=lambda: _launch_sync_job(
                            scheduled_sync_payload, trigger="scheduled"
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Scheduler tick failed")
                time.sleep(SCHEDULER_POLL_SECONDS)

        Thread(target=_scheduler_loop, daemon=True).start()
        logger.info(
            "Scheduler enabled refresh_interval_hours=%s fetch_after_refresh_hours=%s",
            ctx.settings.refresh_interval_hours,
            ctx.settings.fetch_after_refresh_hours,
        )

    return app
