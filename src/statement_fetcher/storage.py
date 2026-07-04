from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .crypto import decrypt_value, encrypt_value
from .models import ConfigurationFile, DownloadedStatement, LinkedAccount, LinkedItem, StateFile
from .settings import Settings


def ensure_environment_files(settings: Settings) -> None:
    settings.env_root.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    _initialize_database(_database_path(settings))


def _database_path(settings: Settings) -> Path:
    return settings.env_root / "state.db"


def _connect(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(_database_path(settings))
    conn.row_factory = sqlite3.Row
    return conn


def _initialize_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS linked_items (
                item_id TEXT PRIMARY KEY,
                institution_id TEXT NOT NULL,
                institution_name TEXT NOT NULL,
                institution_logo TEXT,
                access_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS linked_accounts (
                account_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                account_name TEXT NOT NULL,
                account_mask TEXT,
                account_type TEXT,
                account_subtype TEXT,
                alias TEXT,
                FOREIGN KEY(item_id) REFERENCES linked_items(item_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_linked_accounts_item_id
            ON linked_accounts(item_id);

            CREATE TABLE IF NOT EXISTS downloaded_statements (
                dedupe_key TEXT PRIMARY KEY,
                statement_id TEXT,
                institution_name TEXT NOT NULL,
                account_id TEXT NOT NULL,
                account_name TEXT NOT NULL,
                statement_date TEXT NOT NULL,
                file_path TEXT NOT NULL,
                downloaded_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_downloaded_statements_account_id
            ON downloaded_statements(account_id);

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                account_id TEXT,
                item_id TEXT,
                job_id TEXT,
                metadata_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_events_account_id
            ON events(account_id);

            CREATE INDEX IF NOT EXISTS idx_events_job_id
            ON events(job_id);

            CREATE TABLE IF NOT EXISTS sync_jobs (
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

            CREATE INDEX IF NOT EXISTS idx_sync_jobs_started_at
            ON sync_jobs(started_at);

            CREATE TABLE IF NOT EXISTS service_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(linked_items)").fetchall()}
        if "institution_logo" not in columns:
            conn.execute("ALTER TABLE linked_items ADD COLUMN institution_logo TEXT")
        conn.commit()
    finally:
        conn.close()


def load_configuration(settings: Settings) -> ConfigurationFile:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        items_rows = conn.execute(
            """
            SELECT
                item_id,
                institution_id,
                institution_name,
                institution_logo,
                access_token,
                created_at,
                updated_at
            FROM linked_items
            ORDER BY institution_name, item_id
            """
        ).fetchall()
        accounts_rows = conn.execute(
            """
            SELECT
                account_id,
                item_id,
                account_name,
                account_mask,
                account_type,
                account_subtype,
                alias
            FROM linked_accounts
            ORDER BY account_name, account_id
            """
        ).fetchall()

    accounts_by_item_id: dict[str, list[LinkedAccount]] = {}
    for row in accounts_rows:
        account = LinkedAccount(
            account_id=row["account_id"],
            account_name=row["account_name"],
            account_mask=row["account_mask"],
            account_type=row["account_type"],
            account_subtype=row["account_subtype"],
            alias=row["alias"],
        )
        accounts_by_item_id.setdefault(row["item_id"], []).append(account)

    linked_items: list[LinkedItem] = []
    for row in items_rows:
        linked_items.append(
            LinkedItem(
                institution_id=row["institution_id"],
                institution_name=row["institution_name"],
                institution_logo=row["institution_logo"],
                item_id=row["item_id"],
                access_token=decrypt_value(
                    row["access_token"],
                    settings.encryption_secret,
                ),
                accounts=accounts_by_item_id.get(row["item_id"], []),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        )

    return ConfigurationFile(environment=settings.plaid_env, linked_items=linked_items)


def save_configuration(settings: Settings, config: ConfigurationFile) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute("DELETE FROM linked_accounts")
        conn.execute("DELETE FROM linked_items")

        for item in config.linked_items:
            conn.execute(
                """
                INSERT INTO linked_items (
                    item_id,
                    institution_id,
                    institution_name,
                    institution_logo,
                    access_token,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    item.institution_id,
                    item.institution_name,
                    item.institution_logo,
                    encrypt_value(item.access_token, settings.encryption_secret),
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )

            for account in item.accounts:
                conn.execute(
                    """
                    INSERT INTO linked_accounts (
                        account_id,
                        item_id,
                        account_name,
                        account_mask,
                        account_type,
                        account_subtype,
                        alias
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account.account_id,
                        item.item_id,
                        account.account_name,
                        account.account_mask,
                        account.account_type,
                        account.account_subtype,
                        account.alias,
                    ),
                )

        conn.commit()


def load_state(settings: Settings) -> StateFile:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        rows = conn.execute(
            """
            SELECT
                statement_id,
                institution_name,
                account_id,
                account_name,
                statement_date,
                file_path,
                downloaded_at,
                dedupe_key
            FROM downloaded_statements
            ORDER BY downloaded_at, dedupe_key
            """
        ).fetchall()

    downloaded_statements = [
        DownloadedStatement(
            statement_id=row["statement_id"],
            institution_name=row["institution_name"],
            account_id=row["account_id"],
            account_name=row["account_name"],
            statement_date=datetime.fromisoformat(row["statement_date"]).date(),
            file_path=row["file_path"],
            downloaded_at=datetime.fromisoformat(row["downloaded_at"]),
            dedupe_key=row["dedupe_key"],
        )
        for row in rows
    ]

    return StateFile(environment=settings.plaid_env, downloaded_statements=downloaded_statements)


def save_state(settings: Settings, state: StateFile) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute("DELETE FROM downloaded_statements")
        for entry in state.downloaded_statements:
            conn.execute(
                """
                INSERT INTO downloaded_statements (
                    dedupe_key,
                    statement_id,
                    institution_name,
                    account_id,
                    account_name,
                    statement_date,
                    file_path,
                    downloaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.dedupe_key,
                    entry.statement_id,
                    entry.institution_name,
                    entry.account_id,
                    entry.account_name,
                    entry.statement_date.isoformat(),
                    entry.file_path,
                    entry.downloaded_at.isoformat(),
                ),
            )
        conn.commit()


def remove_account_from_configuration(settings: Settings, account_id: str) -> bool:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        cursor = conn.execute("DELETE FROM linked_accounts WHERE account_id = ?", (account_id,))
        changed = cursor.rowcount > 0
        if changed:
            conn.execute(
                """
                DELETE FROM linked_items
                WHERE item_id IN (
                    SELECT li.item_id
                    FROM linked_items li
                    LEFT JOIN linked_accounts la ON la.item_id = li.item_id
                    GROUP BY li.item_id
                    HAVING COUNT(la.account_id) = 0
                )
                """
            )
            _add_event_with_connection(
                conn,
                event_type="account_removed",
                message="Linked account removed",
                account_id=account_id,
            )
        conn.commit()
    return changed


def remove_institution_from_configuration(settings: Settings, institution_id: str) -> bool:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        cursor = conn.execute(
            "DELETE FROM linked_items WHERE institution_id = ?",
            (institution_id,),
        )
        changed = cursor.rowcount > 0
        if changed:
            _add_event_with_connection(
                conn,
                event_type="institution_removed",
                message="Linked institution removed",
                metadata={"institution_id": institution_id},
            )
        conn.commit()
    return changed


def upsert_linked_item(settings: Settings, linked_item: LinkedItem) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        existing_aliases_rows = conn.execute(
            "SELECT account_id, alias FROM linked_accounts WHERE item_id = ?",
            (linked_item.item_id,),
        ).fetchall()
        existing_aliases = {
            row["account_id"]: row["alias"]
            for row in existing_aliases_rows
            if row["alias"] is not None
        }

        now = datetime.now(UTC).isoformat()
        existing_item = conn.execute(
            "SELECT created_at FROM linked_items WHERE item_id = ?",
            (linked_item.item_id,),
        ).fetchone()
        created_at = (
            existing_item["created_at"] if existing_item else linked_item.created_at.isoformat()
        )

        conn.execute(
            """
            INSERT INTO linked_items (
                item_id,
                institution_id,
                institution_name,
                institution_logo,
                access_token,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                institution_id = excluded.institution_id,
                institution_name = excluded.institution_name,
                institution_logo = excluded.institution_logo,
                access_token = excluded.access_token,
                updated_at = excluded.updated_at
            """,
            (
                linked_item.item_id,
                linked_item.institution_id,
                linked_item.institution_name,
                linked_item.institution_logo,
                encrypt_value(linked_item.access_token, settings.encryption_secret),
                created_at,
                now,
            ),
        )

        conn.execute("DELETE FROM linked_accounts WHERE item_id = ?", (linked_item.item_id,))
        for account in linked_item.accounts:
            alias = existing_aliases.get(account.account_id, account.alias)
            conn.execute(
                """
                INSERT INTO linked_accounts (
                    account_id,
                    item_id,
                    account_name,
                    account_mask,
                    account_type,
                    account_subtype,
                    alias
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account.account_id,
                    linked_item.item_id,
                    account.account_name,
                    account.account_mask,
                    account.account_type,
                    account.account_subtype,
                    alias,
                ),
            )

        _add_event_with_connection(
            conn,
            event_type="item_linked",
            message="Linked institution item updated",
            item_id=linked_item.item_id,
            metadata={
                "institution_id": linked_item.institution_id,
                "institution_name": linked_item.institution_name,
                "accounts_count": len(linked_item.accounts),
            },
        )
        conn.commit()


def set_account_alias(settings: Settings, account_id: str, alias: str | None) -> bool:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        cursor = conn.execute(
            "UPDATE linked_accounts SET alias = ? WHERE account_id = ?",
            (alias, account_id),
        )
        changed = cursor.rowcount > 0
        if changed:
            _add_event_with_connection(
                conn,
                event_type="alias_updated",
                message="Account alias updated",
                account_id=account_id,
                metadata={"alias": alias},
            )
        conn.commit()
    return changed


def get_account_details(settings: Settings, account_id: str) -> dict[str, Any] | None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            """
            SELECT
                la.account_id,
                la.account_name,
                la.account_mask,
                la.account_type,
                la.account_subtype,
                la.alias,
                li.item_id,
                li.institution_id,
                li.institution_name,
                li.created_at,
                li.updated_at
            FROM linked_accounts la
            INNER JOIN linked_items li ON la.item_id = li.item_id
            WHERE la.account_id = ?
            """,
            (account_id,),
        ).fetchone()

    if row is None:
        return None

    return {
        "account_id": row["account_id"],
        "account_name": row["account_name"],
        "account_mask": row["account_mask"],
        "account_type": row["account_type"],
        "account_subtype": row["account_subtype"],
        "alias": row["alias"],
        "item_id": row["item_id"],
        "institution_id": row["institution_id"],
        "institution_name": row["institution_name"],
        "linked_created_at": row["created_at"],
        "linked_updated_at": row["updated_at"],
    }


def add_event(
    settings: Settings,
    *,
    event_type: str,
    message: str,
    level: str = "info",
    account_id: str | None = None,
    item_id: str | None = None,
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        _add_event_with_connection(
            conn,
            event_type=event_type,
            message=message,
            level=level,
            account_id=account_id,
            item_id=item_id,
            job_id=job_id,
            metadata=metadata,
        )
        conn.commit()


def _add_event_with_connection(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    message: str,
    level: str = "info",
    account_id: str | None = None,
    item_id: str | None = None,
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events (
            created_at,
            level,
            event_type,
            message,
            account_id,
            item_id,
            job_id,
            metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(UTC).isoformat(),
            level,
            event_type,
            message,
            account_id,
            item_id,
            job_id,
            json.dumps(metadata, sort_keys=True) if metadata else None,
        ),
    )


def list_events(
    settings: Settings,
    *,
    account_id: str | None = None,
    job_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_environment_files(settings)
    filters: list[str] = []
    args: list[Any] = []

    if account_id:
        filters.append("account_id = ?")
        args.append(account_id)
    if job_id:
        filters.append("job_id = ?")
        args.append(job_id)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(filters)

    with _connect(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT
                event_id,
                created_at,
                level,
                event_type,
                message,
                account_id,
                item_id,
                job_id,
                metadata_json
            FROM events
            {where_clause}
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        events.append(
            {
                "event_id": row["event_id"],
                "created_at": row["created_at"],
                "level": row["level"],
                "event_type": row["event_type"],
                "message": row["message"],
                "account_id": row["account_id"],
                "item_id": row["item_id"],
                "job_id": row["job_id"],
                "metadata": metadata,
            }
        )
    return events


def get_service_configuration(settings: Settings) -> dict[str, str]:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT key, value FROM service_config ORDER BY key"
        ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_service_configuration(settings: Settings, values: dict[str, str]) -> None:
    ensure_environment_files(settings)
    now = datetime.now(UTC).isoformat()
    with _connect(settings) as conn:
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO service_config (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
        conn.commit()


def delete_service_configuration_keys(settings: Settings, keys: list[str]) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        for key in keys:
            conn.execute("DELETE FROM service_config WHERE key = ?", (key,))
        conn.commit()


def create_sync_job(settings: Settings, job_id: str, started_at: str) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            INSERT INTO sync_jobs (job_id, status, started_at)
            VALUES (?, 'running', ?)
            """,
            (job_id, started_at),
        )
        conn.commit()


def update_sync_job_progress(
    settings: Settings,
    *,
    job_id: str,
    listed: int,
    downloaded: int,
    skipped_existing: int,
    skipped_filtered: int,
    errors: int,
) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            UPDATE sync_jobs
            SET
                listed = ?,
                downloaded = ?,
                skipped_existing = ?,
                skipped_filtered = ?,
                errors = ?
            WHERE job_id = ?
            """,
            (
                listed,
                downloaded,
                skipped_existing,
                skipped_filtered,
                errors,
                job_id,
            ),
        )
        conn.commit()


def complete_sync_job(
    settings: Settings,
    *,
    job_id: str,
    finished_at: str,
    listed: int,
    downloaded: int,
    skipped_existing: int,
    skipped_filtered: int,
    errors: int,
) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            UPDATE sync_jobs
            SET
                status = 'completed',
                finished_at = ?,
                listed = ?,
                downloaded = ?,
                skipped_existing = ?,
                skipped_filtered = ?,
                errors = ?
            WHERE job_id = ?
            """,
            (
                finished_at,
                listed,
                downloaded,
                skipped_existing,
                skipped_filtered,
                errors,
                job_id,
            ),
        )
        conn.commit()


def fail_sync_job(settings: Settings, *, job_id: str, finished_at: str, error: str) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            UPDATE sync_jobs
            SET
                status = 'failed',
                finished_at = ?,
                error = ?
            WHERE job_id = ?
            """,
            (finished_at, error, job_id),
        )
        conn.commit()


def get_sync_job(settings: Settings, job_id: str) -> dict[str, Any] | None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            """
            SELECT
                job_id,
                status,
                started_at,
                finished_at,
                error,
                listed,
                downloaded,
                skipped_existing,
                skipped_filtered,
                errors
            FROM sync_jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

    if row is None:
        return None

    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "listed": row["listed"],
        "downloaded": row["downloaded"],
        "skipped_existing": row["skipped_existing"],
        "skipped_filtered": row["skipped_filtered"],
        "errors": row["errors"],
    }


def list_sync_jobs(settings: Settings, limit: int = 200) -> list[dict[str, Any]]:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        rows = conn.execute(
            """
            SELECT
                job_id,
                status,
                started_at,
                finished_at,
                error,
                listed,
                downloaded,
                skipped_existing,
                skipped_filtered,
                errors
            FROM sync_jobs
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "job_id": row["job_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "listed": row["listed"],
            "downloaded": row["downloaded"],
            "skipped_existing": row["skipped_existing"],
            "skipped_filtered": row["skipped_filtered"],
            "errors": row["errors"],
        }
        for row in rows
    ]
