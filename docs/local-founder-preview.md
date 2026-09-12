# Local founder preview

The local founder preview is a repeatable product-development harness. It does
not publish the application, activate the usage-learning phase, send email, or
create hosting evidence.

## Run it

Prerequisites are Python and Docker with Compose. From the repository root:

```console
python scripts/local_preview.py run
```

The command resets only the Compose project named
`agent-data-oracle-preview`, then:

1. builds the repository's production `runtime` image;
2. starts an isolated PostgreSQL 17.6 database on an in-memory filesystem;
3. runs the explicit Alembic migration command;
4. imports the recorded `recall-10887.json` fixture without a live request;
5. starts the application and waits for both `/live` and `/ready`;
6. runs a pinned Chromium journey in the Playwright development image on an
   internal-only Compose network.

The app and database remain available after a passing run so the founder can
inspect <http://127.0.0.1:18080>. Remove only the harness-owned containers,
network, and ephemeral data with:

```console
python scripts/local_preview.py down
```

The existing developer database in `compose.yaml` is a different service,
project, port, and data volume. Neither preview command removes it.

## Decision checkpoint

The ticket required three choices against the existing application, database,
email, and HTTP seams.

### Sign-in delivery

Selected: the application-owned `LocalCaptureEmailProvider` plus an explicit
local-preview handshake and claim route. The routes exist only when a
local/test process explicitly enables the harness. The application generates
the preview code in process, exposes it only to a loopback client in the shared
browser network namespace, and requires the same code as bearer authentication
to claim a link. The claim accepts the recipient in an unlogged request body,
removes the link when claimed, and is absent by default and in deployed
configuration.

Rejected for this ticket: a Mailpit SMTP service. Mailpit provides an SMTP
listener, web inbox, and HTTP API, but the application has no SMTP production
adapter: adding one would introduce another transport and service without
exercising the Gmail API adapter or a selected future sender. Its inbox would
also require separate access controls. See the [Mailpit sending
model](https://mailpit.axllent.org/docs/usage/sending-messages/) and [HTTP
configuration](https://mailpit.axllent.org/docs/configuration/http/).

### Browser automation

Selected: Playwright's Python API with Chromium in the version-matched
`v1.62.0-noble` development image. It fits the Python repository, supports
Windows and Linux/CI, provides web-first locators and an API request context,
and avoids host browser/driver setup. The browser context blocks any origin
other than the Compose application. The official documentation recommends the
pytest integration for end-to-end work and requires matching package and image
versions: [installation](https://playwright.dev/python/docs/intro) and [Docker
image](https://playwright.dev/python/docs/docker).

Rejected for this ticket: Selenium. It is credible and cross-browser, but its
official setup requires the language binding, browser, and browser-specific
driver. That is more moving parts than this single deterministic Chromium gate:
[Selenium setup](https://www.selenium.dev/documentation/webdriver/getting_started/).

### Application orchestration

Selected: a separate Compose model whose application, migration, and fixture
services all use the existing production image. A small cross-platform Python
entry point gives the journey one stable command and isolates its lifecycle
from the ordinary developer database. Compose health and completion conditions
make the migration and fixture ordering explicit; this follows Docker's
[startup-order guidance](https://docs.docker.com/compose/how-tos/startup-order/).

Rejected for this ticket: an imperative harness that individually builds and
runs containers. It would duplicate Compose networking, readiness, and cleanup
logic and make later CI use less direct. The Python entry point therefore only
resets the named preview project and invokes the declarative Compose journey.

## What the journey proves

The real browser covers a rejected gate secret, a rejected non-allowlisted
recipient, admitted founder-preview sign-in, one successful link redemption and
rejected replay, the required operator declaration,
founder TOTP enrollment, fixture-backed candidate submission, the pending
audit boundary, inspection and exact-evaluation approval, released evidence,
the official-source action without following its external redirect, and a
typed human acknowledgement.

The same authenticated browser creates a scoped agent key. Playwright's API
client then submits and retrieves a released no-candidate evidence contract,
retrieves its evidence, reports a typed agent review, and proves immediate key
revocation. Those calls use application APIs rather than duplicated matching
or authorization logic.

The run also proves the production image can operate locally without GCP,
Gmail OAuth, a domain, a paid account, or hosted telemetry. Founder-preview
queues do not consume the usage-learning or first-20-real-queue allowance.

## What remains unproven

This is not evidence for Cloud IAM, managed secrets, hosted TLS or secure-cookie
behavior, external email delivery, provider observability, backup/restore,
deployment, public traffic, or any product/market claim. The entry point
does not supply credentials through environment variables or command arguments.
The application generates fresh auth and preview secrets in its own process;
the preview code crosses only the development-only loopback handshake. Neither
value is tracked, printed, stored in container metadata, or written to
application data. PostgreSQL trust authentication is confined to the
internal-only Compose network, has no host port, and protects no persistent
data because the database uses an in-memory filesystem.

Provider copy fails closed outside local/test unless `PROVIDER_DISCLOSURE` is
configured. Localhost truthfully describes local data and in-memory sign-in
capture; it makes no Google Cloud or Gmail claim.
