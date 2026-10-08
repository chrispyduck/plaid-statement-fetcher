from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
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
                provider TEXT NOT NULL DEFAULT 'plaid',
                institution_id TEXT NOT NULL,
                institution_name TEXT NOT NULL,
                institution_logo TEXT,
                access_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                login_required INTEGER NOT NULL DEFAULT 0,
                login_required_at TEXT
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
                job_type TEXT NOT NULL DEFAULT 'sync',
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT,
                listed INTEGER NOT NULL DEFAULT 0,
                downloaded INTEGER NOT NULL DEFAULT 0,
                skipped_existing INTEGER NOT NULL DEFAULT 0,
                skipped_filtered INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                requested INTEGER,
                failed INTEGER
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
        if "provider" not in columns:
            conn.execute(
                "ALTER TABLE linked_items ADD COLUMN provider TEXT NOT NULL DEFAULT 'plaid'"
            )
        if "login_required" not in columns:
            conn.execute(
                "ALTER TABLE linked_items ADD COLUMN login_required INTEGER NOT NULL DEFAULT 0"
            )
        if "login_required_at" not in columns:
            conn.execute("ALTER TABLE linked_items ADD COLUMN login_required_at TEXT")

        sync_job_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sync_jobs)").fetchall()
        }
        if "job_type" not in sync_job_columns:
            conn.execute("ALTER TABLE sync_jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'sync'")
        if "requested" not in sync_job_columns:
            conn.execute("ALTER TABLE sync_jobs ADD COLUMN requested INTEGER")
        if "failed" not in sync_job_columns:
            conn.execute("ALTER TABLE sync_jobs ADD COLUMN failed INTEGER")

        # job_type is only guaranteed to exist once the migration above has run, so
        # this index has to be created after that rather than alongside the table's
        # initial CREATE TABLE (which is a no-op on a pre-existing database).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_jobs_job_type ON sync_jobs(job_type)")
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
                provider,
                institution_id,
                institution_name,
                institution_logo,
                access_token,
                created_at,
                updated_at,
                login_required,
                login_required_at
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
                provider=row["provider"],
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
                login_required=bool(row["login_required"]),
                login_required_at=(
                    datetime.fromisoformat(row["login_required_at"])
                    if row["login_required_at"]
                    else None
                ),
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
                    provider,
                    institution_id,
                    institution_name,
                    institution_logo,
                    access_token,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    item.provider,
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


def statement_dedupe_key(
    institution_name: str,
    account_id: str,
    statement_date: date,
    statement_id: str | None,
) -> str:
    key_suffix = statement_id or statement_date.isoformat()
    return f"{institution_name}|{account_id}|{key_suffix}"


def _account_match_key(row: sqlite3.Row) -> tuple[str, str, str, str]:
    """Identify the same real-world account across separately-linked items.

    Providers mint fresh account_ids for every new item, so linking the same login a
    second time yields different ids for identical accounts. Mask + type/subtype is
    what stays stable; accounts without a mask (e.g. mortgages) fall back to name.
    """
    account_type = row["account_type"] or ""
    account_subtype = row["account_subtype"] or ""
    if row["account_mask"]:
        return ("mask", account_type, account_subtype, row["account_mask"])
    return ("name", account_type, account_subtype, row["account_name"].strip().casefold())


def merge_duplicate_accounts(settings: Settings) -> list[LinkedItem]:
    """Fold accounts duplicated across items at the same institution into the newest item.

    Linking an institution again through a fresh Link/FastLink session (instead of
    reconnecting the existing item) creates a second item whose accounts duplicate the
    first one's. For each duplicate, the newer item's account wins: it inherits the
    older account's alias, downloaded statements and events, and the older account is
    removed. Older items left with no accounts are deleted and returned so the caller
    can also remove them at the provider.

    Two items are only treated as the same login when they share at least one account
    by mask, so that unrelated logins at one institution don't get merged on account
    names alone. Downloaded statements keep their old statement_id here; sync adopts
    them under the new item's statement_ids (see `_adopt_reissued_statements`).
    """
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        items = conn.execute(
            """
            SELECT
                item_id,
                provider,
                institution_id,
                institution_name,
                institution_logo,
                access_token,
                created_at,
                updated_at
            FROM linked_items
            ORDER BY created_at, item_id
            """
        ).fetchall()
        account_rows = conn.execute(
            """
            SELECT account_id, item_id, account_name, account_mask, account_type,
                   account_subtype, alias
            FROM linked_accounts
            """
        ).fetchall()

        rows_by_item: dict[str, list[sqlite3.Row]] = {}
        for row in account_rows:
            rows_by_item.setdefault(row["item_id"], []).append(row)
        # Keys shared by two accounts within one item are ambiguous; never match on them.
        accounts_by_key: dict[str, dict[tuple[str, str, str, str], sqlite3.Row]] = {}
        for item_id, rows in rows_by_item.items():
            keys = [_account_match_key(row) for row in rows]
            accounts_by_key[item_id] = {
                key: row for key, row in zip(keys, rows, strict=True) if keys.count(key) == 1
            }

        merges: list[tuple[sqlite3.Row, sqlite3.Row]] = []
        merged_account_ids: set[str] = set()
        for index, older in enumerate(items):
            older_accounts = accounts_by_key.get(older["item_id"], {})
            # Newest first, so each duplicate goes straight to its final home.
            for newer in reversed(items[index + 1 :]):
                if (newer["provider"], newer["institution_id"]) != (
                    older["provider"],
                    older["institution_id"],
                ):
                    continue
                newer_accounts = accounts_by_key.get(newer["item_id"], {})
                shared_keys = older_accounts.keys() & newer_accounts.keys()
                if not any(key[0] == "mask" for key in shared_keys):
                    continue
                for key in sorted(shared_keys):
                    source = older_accounts[key]
                    if source["account_id"] in merged_account_ids:
                        continue
                    merged_account_ids.add(source["account_id"])
                    merges.append((source, newer_accounts[key]))

        if not merges:
            return []

        for source, target in merges:
            source_id = source["account_id"]
            target_id = target["account_id"]
            if source["alias"] is not None:
                conn.execute(
                    "UPDATE linked_accounts SET alias = ? WHERE account_id = ? AND alias IS NULL",
                    (source["alias"], target_id),
                )
            statement_rows = conn.execute(
                """
                SELECT dedupe_key, statement_id, institution_name, statement_date
                FROM downloaded_statements
                WHERE account_id = ?
                """,
                (source_id,),
            ).fetchall()
            for statement in statement_rows:
                conn.execute(
                    """
                    UPDATE OR IGNORE downloaded_statements
                    SET account_id = ?, dedupe_key = ?
                    WHERE dedupe_key = ?
                    """,
                    (
                        target_id,
                        statement_dedupe_key(
                            statement["institution_name"],
                            target_id,
                            date.fromisoformat(statement["statement_date"]),
                            statement["statement_id"],
                        ),
                        statement["dedupe_key"],
                    ),
                )
            # Anything left collided with a record the target account already has.
            conn.execute("DELETE FROM downloaded_statements WHERE account_id = ?", (source_id,))
            conn.execute(
                "UPDATE events SET account_id = ? WHERE account_id = ?",
                (target_id, source_id),
            )
            conn.execute("DELETE FROM linked_accounts WHERE account_id = ?", (source_id,))
            _add_event_with_connection(
                conn,
                event_type="account_merged",
                message="Duplicate account from an older link merged into this account",
                account_id=target_id,
                item_id=target["item_id"],
                metadata={
                    "merged_account_id": source_id,
                    "merged_item_id": source["item_id"],
                    "account_name": target["account_name"],
                    "account_mask": target["account_mask"],
                },
            )

        removed_items: list[LinkedItem] = []
        source_item_ids = {source["item_id"] for source, _ in merges}
        for item in items:
            if item["item_id"] not in source_item_ids:
                continue
            remaining = conn.execute(
                "SELECT COUNT(*) FROM linked_accounts WHERE item_id = ?",
                (item["item_id"],),
            ).fetchone()[0]
            if remaining:
                continue
            conn.execute("DELETE FROM linked_items WHERE item_id = ?", (item["item_id"],))
            _add_event_with_connection(
                conn,
                event_type="item_superseded",
                message="Institution item removed after its accounts were merged into a newer link",
                item_id=item["item_id"],
                metadata={
                    "institution_id": item["institution_id"],
                    "institution_name": item["institution_name"],
                },
            )
            removed_items.append(
                LinkedItem(
                    provider=item["provider"],
                    institution_id=item["institution_id"],
                    institution_name=item["institution_name"],
                    institution_logo=item["institution_logo"],
                    item_id=item["item_id"],
                    access_token=decrypt_value(item["access_token"], settings.encryption_secret),
                    created_at=datetime.fromisoformat(item["created_at"]),
                    updated_at=datetime.fromisoformat(item["updated_at"]),
                )
            )
        conn.commit()
    return removed_items


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
                provider,
                institution_id,
                institution_name,
                institution_logo,
                access_token,
                created_at,
                updated_at,
                login_required,
                login_required_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            ON CONFLICT(item_id) DO UPDATE SET
                provider = excluded.provider,
                institution_id = excluded.institution_id,
                institution_name = excluded.institution_name,
                institution_logo = excluded.institution_logo,
                access_token = excluded.access_token,
                updated_at = excluded.updated_at,
                login_required = 0,
                login_required_at = NULL
            """,
            (
                linked_item.item_id,
                linked_item.provider,
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


def set_item_login_required(
    settings: Settings,
    item_id: str,
    *,
    required: bool,
) -> bool:
    """Record whether Plaid is reporting ITEM_LOGIN_REQUIRED for a linked item.

    Cleared automatically by `upsert_linked_item` whenever a Plaid call against the
    item succeeds again (manual refresh, reconnect, or a later sync/refresh job).
    """
    ensure_environment_files(settings)
    now = datetime.now(UTC).isoformat() if required else None
    with _connect(settings) as conn:
        cursor = conn.execute(
            """
            UPDATE linked_items
            SET login_required = ?, login_required_at = ?
            WHERE item_id = ?
            """,
            (1 if required else 0, now, item_id),
        )
        changed = cursor.rowcount > 0
        conn.commit()
    return changed


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
                li.provider,
                li.institution_id,
                li.institution_name,
                li.created_at,
                li.updated_at,
                li.login_required,
                li.login_required_at
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
        "provider": row["provider"],
        "institution_id": row["institution_id"],
        "institution_name": row["institution_name"],
        "linked_created_at": row["created_at"],
        "linked_updated_at": row["updated_at"],
        "login_required": bool(row["login_required"]),
        "login_required_at": row["login_required_at"],
    }


def list_downloaded_statements(
    settings: Settings,
    *,
    account_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_environment_files(settings)

    where_clause = ""
    args: list[Any] = []
    if account_id:
        where_clause = "WHERE ds.account_id = ?"
        args.append(account_id)

    with _connect(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT
                ds.dedupe_key,
                ds.statement_id,
                ds.institution_name,
                ds.account_id,
                ds.account_name,
                ds.statement_date,
                ds.file_path,
                ds.downloaded_at,
                la.alias
            FROM downloaded_statements ds
            LEFT JOIN linked_accounts la ON la.account_id = ds.account_id
            {where_clause}
            ORDER BY ds.statement_date DESC, ds.downloaded_at DESC
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()

    statements: list[dict[str, Any]] = []
    for row in rows:
        file_path = Path(row["file_path"])
        statements.append(
            {
                "dedupe_key": row["dedupe_key"],
                "statement_id": row["statement_id"],
                "institution_name": row["institution_name"],
                "account_id": row["account_id"],
                "account_name": row["alias"] or row["account_name"],
                "statement_date": row["statement_date"],
                "file_name": file_path.name,
                "file_path": row["file_path"],
                "downloaded_at": row["downloaded_at"],
                "file_exists": file_path.exists(),
            }
        )
    return statements


def get_downloaded_statement_by_key(settings: Settings, dedupe_key: str) -> dict[str, Any] | None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            """
            SELECT
                dedupe_key,
                statement_id,
                institution_name,
                account_id,
                account_name,
                statement_date,
                file_path,
                downloaded_at
            FROM downloaded_statements
            WHERE dedupe_key = ?
            """,
            (dedupe_key,),
        ).fetchone()

    if row is None:
        return None

    file_path = Path(row["file_path"])
    return {
        "dedupe_key": row["dedupe_key"],
        "statement_id": row["statement_id"],
        "institution_name": row["institution_name"],
        "account_id": row["account_id"],
        "account_name": row["account_name"],
        "statement_date": row["statement_date"],
        "file_name": file_path.name,
        "file_path": row["file_path"],
        "downloaded_at": row["downloaded_at"],
        "file_exists": file_path.exists(),
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


def create_sync_job(
    settings: Settings,
    job_id: str,
    started_at: str,
    *,
    job_type: str = "sync",
) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            INSERT INTO sync_jobs (job_id, job_type, status, started_at)
            VALUES (?, ?, 'running', ?)
            """,
            (job_id, job_type, started_at),
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


def update_refresh_job_progress(
    settings: Settings,
    *,
    job_id: str,
    requested: int,
    failed: int,
) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute(
            "UPDATE sync_jobs SET requested = ?, failed = ? WHERE job_id = ?",
            (requested, failed, job_id),
        )
        conn.commit()


def complete_refresh_job(
    settings: Settings,
    *,
    job_id: str,
    finished_at: str,
    requested: int,
    failed: int,
) -> None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            UPDATE sync_jobs
            SET
                status = 'completed',
                finished_at = ?,
                requested = ?,
                failed = ?
            WHERE job_id = ?
            """,
            (finished_at, requested, failed, job_id),
        )
        conn.commit()


_JOB_COLUMNS = (
    "job_id",
    "job_type",
    "status",
    "started_at",
    "finished_at",
    "error",
    "listed",
    "downloaded",
    "skipped_existing",
    "skipped_filtered",
    "errors",
    "requested",
    "failed",
)


def _job_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {column: row[column] for column in _JOB_COLUMNS}


def get_sync_job(settings: Settings, job_id: str) -> dict[str, Any] | None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            f"SELECT {', '.join(_JOB_COLUMNS)} FROM sync_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _job_row_to_dict(row) if row is not None else None


def list_sync_jobs(
    settings: Settings,
    limit: int = 200,
    *,
    job_type: str | None = None,
) -> list[dict[str, Any]]:
    ensure_environment_files(settings)
    where_clause = "WHERE job_type = ?" if job_type else ""
    args: list[Any] = [job_type] if job_type else []

    with _connect(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT {', '.join(_JOB_COLUMNS)}
            FROM sync_jobs
            {where_clause}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()

    return [_job_row_to_dict(row) for row in rows]


def get_latest_job(settings: Settings, job_type: str) -> dict[str, Any] | None:
    """Most recently started job of the given type, in any status."""
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            f"""
            SELECT {', '.join(_JOB_COLUMNS)}
            FROM sync_jobs
            WHERE job_type = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (job_type,),
        ).fetchone()

    return _job_row_to_dict(row) if row is not None else None


def get_latest_completed_job(settings: Settings, job_type: str) -> dict[str, Any] | None:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            f"""
            SELECT {', '.join(_JOB_COLUMNS)}
            FROM sync_jobs
            WHERE job_type = ? AND status = 'completed'
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            (job_type,),
        ).fetchone()

    return _job_row_to_dict(row) if row is not None else None


def has_running_job(settings: Settings, job_type: str) -> bool:
    ensure_environment_files(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            "SELECT 1 FROM sync_jobs WHERE job_type = ? AND status = 'running' LIMIT 1",
            (job_type,),
        ).fetchone()
    return row is not None
