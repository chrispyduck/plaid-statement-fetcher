# Plaid Statement Fetcher

Web application and companion CLI for linking institutions via Plaid and downloading statement PDFs for long-term personal records.

## What Is Decided

- Default environment is sandbox, with easy switch to production.
- Config/state/output are isolated per environment.
- Link product scope: Statements only.
- Language/locale: en-US.
- Link flow UX should be simple and use one command for local development.
- Project and virtual environment are managed with uv.
- Access tokens are stored in plaintext for now.
- Package/module name: statement_fetcher.

## Architecture

Two interfaces over one core domain layer:

- Web app:
  - Handles Plaid Link callbacks.
  - Shows linked accounts and supports alias editing.
  - Suitable for local use and Kubernetes deployment.
- CLI:
  - Bootstraps and runs local web flow.
  - Triggers statement sync jobs.
  - Supports operational workflows.

## Project Layout

```text
plaid-statement-fetcher/
  credentials.json
  README.md
  pyproject.toml
  src/
    statement_fetcher/
      app.py
      cli.py
      settings.py
      models.py
      storage.py
  config/
    sandbox/
      output/
      configuration.json
      state.json
    production/
      output/
      configuration.json
      state.json
  k8s/
    base/
      kustomization.yaml
      deployment.yaml
      service.yaml
      ingress.yaml
      configmap.yaml
      secret.example.yaml
```

## Plaid API Overview

Plaid integration uses two major flows:

1. Link
- App creates a link token.
- User completes Plaid Link in browser.
- Plaid returns a public token.
- App exchanges public token for access token.
- App stores item metadata and account metadata.

2. Statements sync
- App requests available statements per linked account.
- App compares with locally stored state.
- App downloads only missing PDFs.
- App updates state for successful downloads.

## Configuration and State

### configuration.json

Per-environment linked institution/account data.

Minimum fields:
- schema_version
- environment
- linked_items[]
  - institution_id
  - institution_name
  - item_id
  - access_token
  - accounts[]
    - account_id
    - account_name
    - account_mask
    - account_type
    - account_subtype
    - alias

### state.json

Per-environment statement download tracking.

Minimum fields:
- schema_version
- environment
- downloaded_statements[]
  - statement_id
  - institution_name
  - account_id
  - account_name
  - statement_date
  - file_path
  - downloaded_at
  - dedupe_key

Dedupe rule:
- institution + account + (date OR statement_id)

## File Naming Rule

Statement files are human-readable and sortable by date.

Format:

```text
YYYY-MM-DD, institution_name, account_name[, statement_id].pdf
```

Examples:

```text
2026-07-01, Chase, Sapphire Checking.pdf
2026-07-01, Chase, Sapphire Checking, stmt_abc123.pdf
```

## Runtime Settings

Configured with pydantic_settings.

Primary variables:

- PLAID_ENV=sandbox|production
- PLAID_CLIENT_ID=...
- PLAID_SECRET=...
- PLAID_PRODUCTS=statements
- PLAID_COUNTRY_CODES=US
- PLAID_LANGUAGE=en-US
- PLAID_REDIRECT_URI=http://localhost:8765/plaid/callback
- PSF_CONFIG_ROOT=./config
- PSF_RETRY_MAX_ATTEMPTS=5
- PSF_RETRY_BASE_DELAY_SECONDS=1
- PSF_RETRY_MAX_DELAY_SECONDS=30

Credential precedence:
1. Environment variables
2. credentials.json

## Command Contract

All commands use module name statement_fetcher.

### Local setup

```bash
uv sync --dev
```

## Local Development Standard Procedure (uv)

Use this procedure for all local development to keep environments consistent.

1. Install uv (one-time)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Create/update the local virtual environment and install dependencies

```bash
uv sync --dev
```

3. Run lint checks

```bash
uv run ruff check .
```

4. Run auto-fix for straightforward lint issues

```bash
uv run ruff check . --fix
```

5. Run the app/CLI using the project environment

```bash
uv run statement-fetcher status --env sandbox
uv run statement-fetcher serve --env sandbox --host 127.0.0.1 --port 8765
```

Notes:
- `.venv` is managed by uv and ignored in git.
- Prefer `uv run ...` instead of activating `.venv` manually.

### Initialize local files

```bash
uv run statement-fetcher init --env sandbox
uv run statement-fetcher init --env production
```

### One-command local link flow

Starts app, opens browser, completes Link, then returns account list page where aliases can be edited.

```bash
uv run statement-fetcher link start --env sandbox
```

### List linked accounts

```bash
uv run statement-fetcher link list --env sandbox
```

### Remove links

By account:

```bash
uv run statement-fetcher link remove --env sandbox --account-id <account_id>
```

By institution:

```bash
uv run statement-fetcher link remove --env sandbox --institution-id <institution_id>
```

### Sync statements

```bash
uv run statement-fetcher fetch --env sandbox
uv run statement-fetcher fetch --env production --dry-run
```

Behavior:
- Downloads all historical statements not already in state.
- Continues on errors.
- Retries transient failures with exponential backoff and jitter, max 5 attempts.

### Run web app directly

```bash
uv run statement-fetcher serve --env sandbox --host 127.0.0.1 --port 8765
```

## Kubernetes Deployment

Base manifests are provided under k8s/base.

Included:
- Deployment
- Service
- Ingress
- ConfigMap
- Secret template
- Kustomization

Apply example:

```bash
kubectl apply -k k8s/base
```

Notes:
- For production, use a real HTTPS domain and update Plaid redirect URI accordingly.
- Do not store real secrets in repository YAML files.

## Why Web App For Linking

Plaid Link is institution-centric and often returns multiple accounts per institution. The web flow therefore:

1. Completes one institution link session.
2. Displays all linked accounts from that session.
3. Allows alias assignment for each account.
4. Stores aliases in configuration for later filename/display use.

## Security TODO

- TODO: Encrypt access tokens or move to managed secret store.
- TODO: Add optional key rotation workflow.
- TODO: Add optional downstream document-management export integration.

