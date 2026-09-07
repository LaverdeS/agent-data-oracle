# View a local evidence queue

Use this only with the local PostgreSQL database. It creates a test account and
a short-lived local browser session; it does not send email or contact CPSC.

The helper follows the same passwordless-authentication domain code as the
application: it requests a local captured sign-in link, consumes its one-time
token, and records the required operator declaration. It then prints the
browser-session value for your local browser.

## Start the local app

In one PowerShell terminal, run:

```powershell
docker compose up -d --wait postgres
uv sync --locked
uv run agent-data-oracle migrate
uv run agent-data-oracle job cpsc-import-fixture `
  --fixture tests/fixtures/cpsc/recall-10887.json `
  --observed-at 2026-09-04T00:00:00Z `
  --expected-record-count 1
$env:AUTH_SECRET = "local-evidence-secret-change-this-value"
uv run agent-data-oracle web --host 127.0.0.1 --port 8080
```

Keep that terminal running. The fixture includes a `HANS0002` model and the
`HARPPA` brand, so it gives the evidence queue two possible
recall-to-listing action records to show.

## Open an authenticated queue

In a second PowerShell terminal, set the same secret and create a session:

```powershell
$env:AUTH_SECRET = "local-evidence-secret-change-this-value"
$session = uv run python scripts/create_local_evidence_session.py
$session
```

Copy the value printed by `$session`. In a normal browser, open
<http://127.0.0.1:8080>, open Developer Tools → Console, and run:

```javascript
document.cookie = "ado_session=PASTE_THE_SESSION_VALUE_HERE; Path=/; SameSite=Lax";
```

Refresh the page, open **Your queues** → **New evidence queue**, and submit:

- Model: `HANS0002`
- Brand: `HARPPA`

Check the authorization box and create the queue. You should see two possible
recall-to-listing action records with their match bases, official CPSC notice,
source times, and limitation language.

The console-set cookie is deliberately local-only and is not marked HttpOnly.
Close the local browser profile or clear cookies when finished. Never use this
helper, session value, or browser-console step against a deployed environment.
