# Founder provisioning checklist

This checklist prepares the authenticated shell in the Frankfurt topology. It
does not activate the usage-learning phase, admit public users, create a
customer account, or authorize collection of validation data.

## 1. Founder-owned accounts and approvals

1. In the [Create a project page](https://console.cloud.google.com/projectcreate),
   create a founder-owned production project. Use **Agent Data Oracle
   Production** as its project name. Try `agent-data-oracle-prod` as its
   globally unique project ID; if it is unavailable, use
   `agent-data-oracle-prod-2026` or another short lowercase suffix. Record the
   final project ID because it cannot be changed later. Select the
   founder-controlled organization, or **No organization** for a personal
   Google Cloud account, then click **Create**.
2. Select the new project in the Google Cloud project selector. Go to
   **Billing** and link the founder-owned billing account. Set budget alerts
   and any supported spend-cap behavior, and record the responsible pause
   owner. Creating the project or linking billing does not create the paid
   database; the first Terraform apply does.
3. Create a separate founder-owned backup project. Backup bucket and scheduled
   backup provisioning belong to ticket #33; do not grant the backup writer
   broader project access in this ticket.
4. In GitHub, protect `main`, create the `production` environment, and require
   the founder's deployment approval. The delivery workflow is manual only.
5. In Google Workspace, create a dedicated sender mailbox and keep its recovery
   controls with the founder. Workspace email processing is the documented
   non-Frankfurt transfer exception.

## 2. Create the regional foundation

Install Terraform and authenticated `gcloud`, then run the initial apply from
the repository root. This creates only Frankfurt infrastructure, identities,
regional Secret Manager containers, Cloud SQL, Artifact Registry, and GitHub
OIDC federation. It deliberately creates no Cloud Run revision yet.

```console
gcloud config set project YOUR_PRODUCTION_PROJECT_ID
gcloud auth application-default set-quota-project YOUR_PRODUCTION_PROJECT_ID
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform apply \
  -var project_id=YOUR_PRODUCTION_PROJECT_ID \
  -var github_repository=LaverdeS/agent-data-oracle
```

The Google Terraform provider uses Application Default Credentials (ADC). If
the quota-project command reports that ADC does not exist yet, run
`gcloud auth application-default login` with the same founder account, then
repeat the quota-project command. A warning that the active project and ADC
quota project differ is not a failed project switch; it is resolved by this
step before Terraform runs.

Record the `cloud_sql_connection_name`, `workload_identity_provider`, and
`deployer_service_account` outputs. Create a least-privilege PostgreSQL login
and database schema access for the application; never place its password in a
Terraform variable or GitHub secret.

## 3. Populate Secret Manager directly

Use the Google Cloud console's Secret Manager page or `gcloud secrets versions
add` to add the following values to the regional secret containers. Enter each
secret directly; do not put a value in Git, Terraform state, an image, CI logs,
or a frontend asset.

| Secret suffix | Value |
| --- | --- |
| `database-url` | `postgresql+psycopg://…?host=/cloudsql/CONNECTION_NAME` |
| `auth-secret` | A newly generated, stable secret of at least 24 bytes |
| `founder-emails` | Comma-separated founder email addresses |
| `gmail-oauth-client-id` | Workspace Gmail API OAuth client ID |
| `gmail-oauth-client-secret` | Workspace Gmail API OAuth client secret |
| `gmail-oauth-refresh-token` | Refresh token for only the dedicated sender mailbox |

In the Google Cloud console, enable the Gmail API for the Workspace project,
create a restricted OAuth client, authorize only `gmail.send` for the sender,
and generate its refresh token through the founder-controlled consent flow.
Keep the client secret and refresh token out of GitHub: the running service
reads them from Secret Manager.

## 4. Bootstrap and connect GitHub delivery

Build and push the first digest-pinned image to the new regional repository
using an authenticated founder session. Then create the Cloud Run service and
one-shot migration job:

```console
terraform -chdir=infra/terraform apply \
  -var project_id=YOUR_PRODUCTION_PROJECT_ID \
  -var github_repository=LaverdeS/agent-data-oracle \
  -var create_application=true \
  -var bootstrap_image=EUROPE-WEST3-docker.pkg.dev/PROJECT/agent-data-oracle/web@sha256:IMAGE_DIGEST
```

The first revision uses the harmless `https://bootstrap.invalid` origin solely
to obtain the generated service URL; it must not be used by an operator. Copy
that URL from the Terraform `service_url` output and apply the same command
once more with `-var public_origin=https://GENERATED.run.app`. Only then can
the sign-in shell issue canonical links. The service uses Cloud Run's generated
`run.app` HTTPS hostname; do not attach a custom domain or load balancer.

Add these non-secret GitHub Actions environment variables to `production`:

| Variable | Value |
| --- | --- |
| `GCP_PROJECT_ID` | Founder-owned production project ID |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Terraform `workload_identity_provider` output |
| `GCP_DEPLOYER_SERVICE_ACCOUNT` | Terraform `deployer_service_account` output |

The workflow uses GitHub OIDC Workload Identity Federation. It must never use
a Google service-account key or a production secret.

## 5. Verify before any activation

After founder approval, run `.github/workflows/deploy.yml` manually with an
immutable image tag. It updates the migration job, waits for Alembic to finish,
and only then moves the web service to that image. Every migration must remain
compatible with the immediately preceding web revision.

Run the repeatable smoke check against the generated HTTPS service URL:

```console
scripts/smoke-deployed-shell.sh https://SERVICE.run.app
```

It checks `/live`, `/ready`, the public sign-in shell, CSRF handling, and the
generic `202` response. Send a sign-in request to a founder-controlled test
inbox and complete the link flow; then, during a scheduled non-public test
window, temporarily use an invalid Gmail OAuth refresh-token secret version
and create a new Cloud Run revision pinned to that exact version:

```console
gcloud run services update agent-data-oracle --region=europe-west3 \
  --update-secrets=GMAIL_OAUTH_REFRESH_TOKEN=agent-data-oracle-gmail-oauth-refresh-token:INVALID_VERSION
scripts/smoke-deployed-shell.sh https://SERVICE.run.app
gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.event="sign_in_delivery_failed"' --limit=1
```

The smoke response must remain generic and the redacted failure event must
contain neither a sign-in token nor product data. Add a new valid refresh-token
secret version and update the service to `:latest` to create the recovery
revision immediately. Do not leave the deliberate failure revision serving.

Do not activate the usage-learning phase until the later readiness ticket has
recorded every required source, privacy, security, deletion, backup, restore,
cost, pause, and alert gate.
