from __future__ import annotations

from html import escape

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .models import LinkedAccount
from .settings import Settings
from .storage import ensure_environment_files, load_configuration, save_configuration


class AliasUpdateRequest(BaseModel):
    account_id: str
    alias: str


class AppContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.load_credentials_fallback()


def _render_accounts_page(settings: Settings) -> str:
    config = load_configuration(settings)

    rows: list[str] = []
    for item in config.linked_items:
        for account in item.accounts:
            alias_value = escape(account.alias or "")
            account_id = escape(account.account_id)
            institution_name = escape(item.institution_name)
            account_name = escape(account.account_name)
            rows.append(
                "<tr>"
                f"<td>{institution_name}</td>"
                f"<td>{account_name}</td>"
                f"<td>{account_id}</td>"
                f"<td><input id='alias-{account_id}' value='{alias_value}'/></td>"
                "<td>"
                f"<button onclick=\"saveAlias('{account_id}')\">Save</button>"
                "</td>"
                "</tr>"
            )

    table_rows = "".join(rows) or (
        "<tr><td colspan='5'>No linked accounts yet. Complete Plaid Link, then refresh.</td></tr>"
    )
    env = escape(settings.plaid_env)

    return (
        "<!doctype html>"
        "<html><head><meta charset='utf-8'><title>Statement Fetcher</title>"
        "<style>"
        "body{font-family:ui-sans-serif,system-ui,sans-serif;margin:2rem;}"
        "table{border-collapse:collapse;width:100%;max-width:1100px;}"
        "th,td{border:1px solid #ddd;padding:8px;text-align:left;}"
        "th{background:#f7f7f7;}"
        "input{width:100%;padding:6px;}"
        "button{padding:6px 10px;}"
        "small{color:#444;}"
        "</style></head><body>"
        f"<h1>Statement Fetcher ({env})</h1>"
        "<p><small>This page lists linked accounts and allows alias updates.</small></p>"
        "<table><thead><tr>"
        "<th>Institution</th><th>Account</th><th>Account ID</th><th>Alias</th><th>Action</th>"
        "</tr></thead><tbody>"
        f"{table_rows}"
        "</tbody></table>"
        "<script>"
        "async function saveAlias(accountId){"
        "const alias=document.getElementById('alias-'+accountId).value;"
        "const r=await fetch('/api/accounts/alias',{" 
        "method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({account_id:accountId,alias})"
        "});"
        "if(!r.ok){alert('Failed to update alias');return;}"
        "alert('Alias updated');"
        "}"
        "</script>"
        "</body></html>"
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    ctx = AppContext(resolved_settings)

    app = FastAPI(title="Statement Fetcher", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return _render_accounts_page(ctx.settings)

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
                        "item_id": item.item_id,
                        "account_id": account.account_id,
                        "account_name": account.account_name,
                        "alias": account.alias,
                    }
                )
        return rows

    @app.post("/api/accounts/alias")
    def set_alias(payload: AliasUpdateRequest) -> dict[str, str]:
        config = load_configuration(ctx.settings)
        updated = False
        for item in config.linked_items:
            for account in item.accounts:
                if account.account_id == payload.account_id:
                    account.alias = payload.alias.strip() or None
                    updated = True

        if not updated:
            raise HTTPException(status_code=404, detail="account_id not found")

        save_configuration(ctx.settings, config)
        return {"status": "updated", "account_id": payload.account_id}

    @app.post("/api/plaid/link/simulate")
    def simulate_link(
        institution_id: str,
        institution_name: str,
        account_name: str,
    ) -> dict[str, str]:
        # Temporary endpoint for local flow validation until real Plaid integration is wired.
        config = load_configuration(ctx.settings)
        account = LinkedAccount(
            account_id=f"acc_{institution_id}_{account_name.replace(' ', '_').lower()}",
            account_name=account_name,
        )
        config.linked_items.append(
            {
                "institution_id": institution_id,
                "institution_name": institution_name,
                "item_id": f"item_{institution_id}",
                "access_token": "TODO_REPLACE_WITH_REAL_EXCHANGE",
                "accounts": [account],
            }
        )
        save_configuration(ctx.settings, config)
        return {"status": "simulated", "institution_id": institution_id}

    @app.get("/plaid/callback", response_class=HTMLResponse)
    def plaid_callback() -> str:
        # Real Plaid callback token handling will be added in implementation phase.
        return _render_accounts_page(ctx.settings)

    return app
