from __future__ import annotations

import webbrowser
from datetime import date
from pathlib import Path

import typer
from rich import print

from .app import create_app
from .settings import Settings
from .storage import (
    ensure_environment_files,
    load_configuration,
    load_state,
    remove_account_from_configuration,
    remove_institution_from_configuration,
)
from .sync import sync_statements

app = typer.Typer(help="statement_fetcher CLI")
link_app = typer.Typer(help="Link management commands")
app.add_typer(link_app, name="link")

SSL_CERTFILE_OPTION = typer.Option(None, "--ssl-certfile")
SSL_KEYFILE_OPTION = typer.Option(None, "--ssl-keyfile")


def resolve_settings(env: str) -> Settings:
    settings = Settings(plaid_env=env)
    settings.load_credentials_fallback(Path("credentials.json"))
    return settings


@app.command()
def init(env: str = typer.Option("sandbox", "--env")) -> None:
    settings = resolve_settings(env)
    ensure_environment_files(settings)
    print(f"[green]Initialized[/green] environment files at {settings.env_root}")


@app.command("serve")
def serve(
    env: str = typer.Option("sandbox", "--env"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    ssl_certfile: Path | None = SSL_CERTFILE_OPTION,
    ssl_keyfile: Path | None = SSL_KEYFILE_OPTION,
) -> None:
    import uvicorn

    settings = resolve_settings(env)
    application = create_app(settings)
    uvicorn.run(
        application,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        ssl_certfile=str(ssl_certfile) if ssl_certfile else None,
        ssl_keyfile=str(ssl_keyfile) if ssl_keyfile else None,
    )


@link_app.command("start")
def link_start(
    env: str = typer.Option("sandbox", "--env"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    no_browser: bool = typer.Option(False, "--no-browser"),
    ssl_certfile: Path | None = SSL_CERTFILE_OPTION,
    ssl_keyfile: Path | None = SSL_KEYFILE_OPTION,
) -> None:
    scheme = "https" if ssl_certfile and ssl_keyfile else "http"
    url = f"{scheme}://{host}:{port}/"
    print("[cyan]Starting local web app for Plaid link flow...[/cyan]")
    print(f"Open {url} and complete link flow.")
    if not no_browser:
        webbrowser.open(url)
    serve(
        env=env,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


@link_app.command("list")
def link_list(env: str = typer.Option("sandbox", "--env")) -> None:
    settings = resolve_settings(env)
    config = load_configuration(settings)
    if not config.linked_items:
        print("[yellow]No linked accounts found.[/yellow]")
        return

    for item in config.linked_items:
        print(f"[bold]{item.institution_name}[/bold] ({item.institution_id})")
        for account in item.accounts:
            alias = account.alias or "-"
            print(f"  - {account.account_id}: {account.account_name} [alias: {alias}]")


@link_app.command("remove")
def link_remove(
    env: str = typer.Option("sandbox", "--env"),
    account_id: str | None = typer.Option(None, "--account-id"),
    institution_id: str | None = typer.Option(None, "--institution-id"),
) -> None:
    if bool(account_id) == bool(institution_id):
        raise typer.BadParameter("Use exactly one of --account-id or --institution-id")

    settings = resolve_settings(env)
    if account_id:
        changed = remove_account_from_configuration(settings, account_id)
        if changed:
            print(f"[green]Removed account[/green] {account_id}")
        else:
            print(f"[yellow]Account not found[/yellow]: {account_id}")

    if institution_id:
        changed = remove_institution_from_configuration(settings, institution_id)
        if changed:
            print(f"[green]Removed institution[/green] {institution_id}")
        else:
            print(f"[yellow]Institution not found[/yellow]: {institution_id}")


@app.command()
def status(env: str = typer.Option("sandbox", "--env")) -> None:
    settings = resolve_settings(env)
    config = load_configuration(settings)
    state = load_state(settings)

    accounts_count = sum(len(item.accounts) for item in config.linked_items)
    print(f"[bold]Environment:[/bold] {env}")
    print(f"[bold]Linked institutions:[/bold] {len(config.linked_items)}")
    print(f"[bold]Linked accounts:[/bold] {accounts_count}")
    print(f"[bold]Downloaded statements:[/bold] {len(state.downloaded_statements)}")


@app.command()
def fetch(
    env: str = typer.Option("sandbox", "--env"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    since: str | None = typer.Option(None, "--since"),
    account_id: str | None = typer.Option(None, "--account-id"),
    max_downloads: int | None = typer.Option(None, "--max-downloads"),
) -> None:
    settings = resolve_settings(env)

    since_date: date | None = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError as exc:
            raise typer.BadParameter("--since must be YYYY-MM-DD") from exc

    summary = sync_statements(
        settings,
        dry_run=dry_run,
        since=since_date,
        account_id=account_id,
        max_downloads=max_downloads,
    )

    mode_label = "dry-run" if dry_run else "live"
    print(f"[bold]Fetch mode:[/bold] {mode_label}")
    print(f"[bold]Statements listed:[/bold] {summary.listed}")
    print(f"[bold]Statements downloaded:[/bold] {summary.downloaded}")
    print(f"[bold]Skipped existing:[/bold] {summary.skipped_existing}")
    print(f"[bold]Skipped filtered:[/bold] {summary.skipped_filtered}")
    print(f"[bold]Errors:[/bold] {summary.errors}")


if __name__ == "__main__":
    app()
