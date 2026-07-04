from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Environment = Literal["sandbox", "production"]


class LinkedAccount(BaseModel):
    account_id: str
    account_name: str
    account_mask: str | None = None
    account_type: str | None = None
    account_subtype: str | None = None
    alias: str | None = None


class LinkedItem(BaseModel):
    institution_id: str
    institution_name: str
    item_id: str
    access_token: str
    accounts: list[LinkedAccount] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ConfigurationFile(BaseModel):
    schema_version: int = 1
    environment: Environment
    linked_items: list[LinkedItem] = Field(default_factory=list)


class DownloadedStatement(BaseModel):
    statement_id: str | None = None
    institution_name: str
    account_id: str
    account_name: str
    statement_date: date
    file_path: str
    downloaded_at: datetime = Field(default_factory=datetime.utcnow)
    dedupe_key: str


class StateFile(BaseModel):
    schema_version: int = 1
    environment: Environment
    downloaded_statements: list[DownloadedStatement] = Field(default_factory=list)
