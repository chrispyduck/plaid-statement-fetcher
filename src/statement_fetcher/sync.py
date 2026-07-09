from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .models import DownloadedStatement
from .plaid_api import PlaidAPIError, PlaidClient
from .settings import Settings
from .storage import load_configuration, load_state, save_state

logger = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    listed: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    skipped_filtered: int = 0
    errors: int = 0


def _sanitize_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "_", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value or "unknown"


def _statement_date(statement: dict) -> date:
    date_posted = statement.get("date_posted")
    if date_posted:
        return date.fromisoformat(date_posted)
    return date(int(statement["year"]), int(statement["month"]), 1)


def _dedupe_key(
    institution_name: str,
    account_id: str,
    statement_date: date,
    statement_id: str | None,
) -> str:
    key_suffix = statement_id or statement_date.isoformat()
    return f"{institution_name}|{account_id}|{key_suffix}"


def _build_output_path(
    output_dir: Path,
    statement_date: date,
    institution_name: str,
    account_name: str,
    statement_id: str,
) -> Path:
    date_text = statement_date.isoformat()
    institution_clean = _sanitize_name(institution_name)
    account_clean = _sanitize_name(account_name)

    base_name = f"{date_text}, {institution_clean}, {account_clean}.pdf"
    base_path = output_dir / base_name
    if not base_path.exists():
        return base_path

    with_statement_id = (
        output_dir / f"{date_text}, {institution_clean}, {account_clean}, {statement_id}.pdf"
    )
    if not with_statement_id.exists():
        return with_statement_id

    short_hash = hashlib.sha256(statement_id.encode("utf-8")).hexdigest()[:8]
    return output_dir / f"{date_text}, {institution_clean}, {account_clean}, {short_hash}.pdf"


def _retry_download(
    settings: Settings,
    plaid_client: PlaidClient,
    access_token: str,
    statement_id: str,
    pace_callback: Callable[[], None] | None = None,
) -> tuple[bytes, str | None]:
    attempt = 0
    while True:
        attempt += 1
        try:
            if pace_callback:
                pace_callback()
            logger.info(
                "Downloading statement statement_id=%s attempt=%s",
                statement_id,
                attempt,
            )
            return plaid_client.download_statement(access_token, statement_id)
        except PlaidAPIError as exc:
            should_retry = exc.retriable and attempt < settings.retry_max_attempts
            if not should_retry:
                logger.error(
                    "Statement download failed statement_id=%s attempt=%s retriable=%s",
                    statement_id,
                    attempt,
                    exc.retriable,
                )
                raise

            delay = min(
                settings.retry_max_delay_seconds,
                settings.retry_base_delay_seconds * (2 ** (attempt - 1)),
            )
            jitter = random.uniform(0.0, 0.25)
            logger.warning(
                "Retrying statement download statement_id=%s next_delay=%.2fs",
                statement_id,
                delay + jitter,
            )
            time.sleep(delay + jitter)


def sync_statements(
    settings: Settings,
    *,
    plaid_client: PlaidClient | None = None,
    dry_run: bool = False,
    since: date | None = None,
    account_id: str | None = None,
    max_downloads: int | None = None,
    progress_callback: Callable[[SyncSummary], None] | None = None,
    event_callback: Callable[[str, str, dict[str, str | int] | None], None] | None = None,
) -> SyncSummary:
    logger.info(
        "Sync started dry_run=%s since=%s account_id=%s max_downloads=%s",
        dry_run,
        since,
        account_id,
        max_downloads,
    )
    summary = SyncSummary()
    client = plaid_client or PlaidClient(settings)

    config = load_configuration(settings)
    state = load_state(settings)
    existing_keys = {entry.dedupe_key for entry in state.downloaded_statements}
    existing_entries = {entry.dedupe_key: entry for entry in state.downloaded_statements}
    min_interval = max(0.0, float(settings.sync_min_interval_seconds))
    last_plaid_call = 0.0

    def pace_plaid_requests() -> None:
        nonlocal last_plaid_call
        if min_interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - last_plaid_call
        wait_for = min_interval - elapsed
        if wait_for > 0:
            time.sleep(wait_for)
        last_plaid_call = time.monotonic()

    for linked_item in config.linked_items:
        logger.info(
            "Listing statements for item_id=%s institution=%s",
            linked_item.item_id,
            linked_item.institution_name,
        )
        account_aliases = {account.account_id: account.alias for account in linked_item.accounts}
        pace_plaid_requests()
        response = client.list_statements(linked_item.access_token)

        institution_name = response.get("institution_name") or linked_item.institution_name
        accounts = response.get("accounts") or []
        response_account_ids = {
            str(response_account.get("account_id"))
            for response_account in accounts
            if response_account.get("account_id")
        }

        for configured_account in linked_item.accounts:
            configured_account_id = configured_account.account_id
            if account_id and configured_account_id != account_id:
                continue
            if configured_account_id in response_account_ids:
                continue
            if event_callback:
                event_callback(
                    "account_statement_unavailable",
                    "Account has no statements available or does not support statements",
                    {
                        "account_id": configured_account_id,
                        "account_name": configured_account.alias or configured_account.account_name,
                        "institution_name": institution_name,
                        "reason": "not_returned_by_plaid_statements_list",
                    },
                )

        for response_account in accounts:
            response_account_id = response_account.get("account_id")
            if not response_account_id:
                continue

            if account_id and response_account_id != account_id:
                summary.skipped_filtered += len(response_account.get("statements") or [])
                continue

            source_name = response_account.get("account_name") or "Unknown Account"
            chosen_name = account_aliases.get(response_account_id) or source_name
            account_statements = response_account.get("statements") or []

            listed_for_account = 0
            new_for_account = 0
            existing_for_account = 0

            for statement in account_statements:
                statement_id = statement.get("statement_id")
                if not statement_id:
                    continue
                listed_for_account += 1
                statement_date = _statement_date(statement)
                dedupe_key = _dedupe_key(
                    institution_name,
                    response_account_id,
                    statement_date,
                    statement_id,
                )
                if dedupe_key in existing_keys:
                    existing_for_account += 1
                    continue
                if since and statement_date < since:
                    continue
                new_for_account += 1

            if event_callback and listed_for_account > 0:
                event_callback(
                    "statement_list_fetched",
                    f"Fetched statement list: {listed_for_account} total, {new_for_account} new",
                    {
                        "account_id": response_account_id,
                        "total_statements": listed_for_account,
                        "new_statements": new_for_account,
                        "existing_statements": existing_for_account,
                    },
                )
            elif event_callback:
                event_callback(
                    "account_no_statements",
                    "No statements available for account",
                    {
                        "account_id": response_account_id,
                        "account_name": chosen_name,
                        "institution_name": institution_name,
                    },
                )

            for statement in account_statements:
                summary.listed += 1
                if progress_callback:
                    progress_callback(summary)

                statement_id = statement.get("statement_id")
                if not statement_id:
                    summary.errors += 1
                    logger.warning(
                        "Statement missing statement_id account_id=%s institution=%s",
                        response_account_id,
                        institution_name,
                    )
                    if event_callback:
                        event_callback(
                            "statement_missing_id",
                            "Statement payload missing statement_id",
                            {
                                "account_id": response_account_id,
                                "institution_name": institution_name,
                            },
                        )
                    if progress_callback:
                        progress_callback(summary)
                    continue

                statement_date = _statement_date(statement)
                if since and statement_date < since:
                    summary.skipped_filtered += 1
                    if event_callback:
                        event_callback(
                            "statement_filtered",
                            "Statement skipped by date/account/download filters",
                            {
                                "account_id": response_account_id,
                                "statement_id": statement_id,
                                "statement_date": statement_date.isoformat(),
                                "reason": "since_filter",
                            },
                        )
                    if progress_callback:
                        progress_callback(summary)
                    continue

                dedupe_key = _dedupe_key(
                    institution_name,
                    response_account_id,
                    statement_date,
                    statement_id,
                )
                if dedupe_key in existing_keys:
                    summary.skipped_existing += 1
                    existing_entry = existing_entries.get(dedupe_key)
                    if event_callback:
                        existing_metadata: dict[str, str | int] = {
                            "account_id": response_account_id,
                            "statement_id": statement_id,
                            "statement_date": statement_date.isoformat(),
                        }
                        if existing_entry and existing_entry.file_path:
                            existing_metadata["file_name"] = Path(existing_entry.file_path).name
                            existing_metadata["file_path"] = existing_entry.file_path
                        existing_metadata["dedupe_key"] = dedupe_key
                        event_callback(
                            "statement_existing",
                            "Statement already downloaded",
                            existing_metadata,
                        )
                    if progress_callback:
                        progress_callback(summary)
                    continue

                if max_downloads is not None and summary.downloaded >= max_downloads:
                    summary.skipped_filtered += 1
                    if event_callback:
                        event_callback(
                            "statement_filtered",
                            "Statement skipped by date/account/download filters",
                            {
                                "account_id": response_account_id,
                                "statement_id": statement_id,
                                "statement_date": statement_date.isoformat(),
                                "reason": "max_downloads",
                            },
                        )
                    if progress_callback:
                        progress_callback(summary)
                    continue

                output_path = _build_output_path(
                    settings.output_dir,
                    statement_date,
                    institution_name,
                    chosen_name,
                    statement_id,
                )

                if dry_run:
                    summary.downloaded += 1
                    if progress_callback:
                        progress_callback(summary)
                    continue

                try:
                    pdf_bytes, plaid_hash = _retry_download(
                        settings,
                        client,
                        linked_item.access_token,
                        statement_id,
                        pace_callback=pace_plaid_requests,
                    )
                except PlaidAPIError:
                    summary.errors += 1
                    logger.exception(
                        "Failed downloading statement statement_id=%s account_id=%s",
                        statement_id,
                        response_account_id,
                    )
                    if event_callback:
                        event_callback(
                            "statement_download_failed",
                            "Statement download failed",
                            {
                                "account_id": response_account_id,
                                "statement_id": statement_id,
                                "statement_date": statement_date.isoformat(),
                                "file_name": output_path.name,
                            },
                        )
                    if progress_callback:
                        progress_callback(summary)
                    continue

                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(pdf_bytes)
                checksum = hashlib.sha256(pdf_bytes).hexdigest()
                if plaid_hash and plaid_hash != checksum:
                    summary.errors += 1
                    logger.error(
                        "Checksum mismatch statement_id=%s plaid_hash=%s computed=%s",
                        statement_id,
                        plaid_hash,
                        checksum,
                    )
                    if event_callback:
                        event_callback(
                            "statement_checksum_mismatch",
                            "Statement checksum mismatch",
                            {
                                "account_id": response_account_id,
                                "statement_id": statement_id,
                                "statement_date": statement_date.isoformat(),
                                "file_name": output_path.name,
                            },
                        )
                    if progress_callback:
                        progress_callback(summary)
                    continue

                state.downloaded_statements.append(
                    DownloadedStatement(
                        statement_id=statement_id,
                        institution_name=institution_name,
                        account_id=response_account_id,
                        account_name=chosen_name,
                        statement_date=statement_date,
                        file_path=str(output_path),
                        dedupe_key=dedupe_key,
                    )
                )
                existing_entries[dedupe_key] = state.downloaded_statements[-1]
                existing_keys.add(dedupe_key)
                summary.downloaded += 1
                if event_callback:
                    event_callback(
                        "statement_downloaded",
                        "Statement downloaded",
                        {
                            "account_id": response_account_id,
                            "statement_id": statement_id,
                            "statement_date": statement_date.isoformat(),
                            "file_name": output_path.name,
                            "file_path": str(output_path),
                            "dedupe_key": dedupe_key,
                        },
                    )
                logger.info(
                    "Statement downloaded statement_id=%s path=%s",
                    statement_id,
                    output_path,
                )
                if progress_callback:
                    progress_callback(summary)

    if not dry_run:
        save_state(settings, state)

    if progress_callback:
        progress_callback(summary)

    logger.info(
        "Sync completed listed=%s downloaded=%s skipped_existing=%s skipped_filtered=%s errors=%s",
        summary.listed,
        summary.downloaded,
        summary.skipped_existing,
        summary.skipped_filtered,
        summary.errors,
    )

    return summary
