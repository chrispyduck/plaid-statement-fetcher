from __future__ import annotations

import json
from datetime import datetime

from .models import ConfigurationFile, LinkedAccount, LinkedItem, StateFile
from .settings import Settings


def ensure_environment_files(settings: Settings) -> None:
    settings.env_root.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    if not settings.configuration_path.exists():
        default_config = ConfigurationFile(environment=settings.plaid_env)
        settings.configuration_path.write_text(
            default_config.model_dump_json(indent=2), encoding="utf-8"
        )

    if not settings.state_path.exists():
        default_state = StateFile(environment=settings.plaid_env)
        settings.state_path.write_text(default_state.model_dump_json(indent=2), encoding="utf-8")


def load_configuration(settings: Settings) -> ConfigurationFile:
    ensure_environment_files(settings)
    data = json.loads(settings.configuration_path.read_text(encoding="utf-8"))
    return ConfigurationFile.model_validate(data)


def save_configuration(settings: Settings, config: ConfigurationFile) -> None:
    ensure_environment_files(settings)
    settings.configuration_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")


def load_state(settings: Settings) -> StateFile:
    ensure_environment_files(settings)
    data = json.loads(settings.state_path.read_text(encoding="utf-8"))
    return StateFile.model_validate(data)


def save_state(settings: Settings, state: StateFile) -> None:
    ensure_environment_files(settings)
    settings.state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def remove_account_from_configuration(settings: Settings, account_id: str) -> bool:
    config = load_configuration(settings)
    changed = False
    for item in config.linked_items:
        prior_len = len(item.accounts)
        item.accounts = [a for a in item.accounts if a.account_id != account_id]
        changed = changed or (len(item.accounts) != prior_len)

    if changed:
        config.linked_items = [i for i in config.linked_items if i.accounts]
        save_configuration(settings, config)
    return changed


def remove_institution_from_configuration(settings: Settings, institution_id: str) -> bool:
    config = load_configuration(settings)
    prior_len = len(config.linked_items)
    config.linked_items = [i for i in config.linked_items if i.institution_id != institution_id]
    changed = len(config.linked_items) != prior_len
    if changed:
        save_configuration(settings, config)
    return changed


def upsert_linked_item(settings: Settings, linked_item: LinkedItem) -> None:
    config = load_configuration(settings)

    existing = next((item for item in config.linked_items if item.item_id == linked_item.item_id), None)
    if existing is None:
        config.linked_items.append(linked_item)
        save_configuration(settings, config)
        return

    aliases_by_account_id = {account.account_id: account.alias for account in existing.accounts}
    merged_accounts: list[LinkedAccount] = []
    for account in linked_item.accounts:
        account.alias = aliases_by_account_id.get(account.account_id, account.alias)
        merged_accounts.append(account)

    existing.institution_id = linked_item.institution_id
    existing.institution_name = linked_item.institution_name
    existing.access_token = linked_item.access_token
    existing.accounts = merged_accounts
    existing.updated_at = datetime.utcnow()

    save_configuration(settings, config)
