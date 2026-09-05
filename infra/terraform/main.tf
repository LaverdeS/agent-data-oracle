locals {
  required_services = toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iamcredentials.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "sts.googleapis.com",
  ])
  runtime_secrets = toset([
    "auth-secret",
    "database-url",
    "founder-emails",
    "gmail-oauth-client-id",
    "gmail-oauth-client-secret",
    "gmail-oauth-refresh-token",
  ])
}

resource "google_project_service" "required" {
  for_each           = local.required_services
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "application" {
  location      = var.region
  repository_id = var.service_name
  description   = "Regional containers for Agent Data Oracle"
  format        = "DOCKER"
  depends_on    = [google_project_service.required]
}

resource "google_service_account" "web" {
  account_id   = "${var.service_name}-web"
  display_name = "Agent Data Oracle web runtime"
}

resource "google_service_account" "jobs" {
  account_id   = "${var.service_name}-jobs"
  display_name = "Agent Data Oracle maintenance jobs"
}

resource "google_service_account" "backup" {
  account_id   = "${var.service_name}-backup"
  display_name = "Agent Data Oracle backup writer"
}

resource "google_service_account" "deployer" {
  account_id   = "${var.service_name}-deployer"
  display_name = "GitHub Actions deployer"
}

resource "google_project_iam_member" "web_cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.web.email}"
}

resource "google_project_iam_member" "jobs_cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.jobs.email}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.application.location
  repository = google_artifact_registry_repository.application.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_can_use_runtime_identities" {
  for_each = toset([
    google_service_account.web.name,
    google_service_account.jobs.name,
  ])
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_secret_manager_secret" "runtime" {
  for_each  = local.runtime_secrets
  secret_id = "${var.service_name}-${each.value}"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "web_runtime" {
  for_each  = google_secret_manager_secret.runtime
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.web.email}"
}

resource "google_secret_manager_secret_iam_member" "jobs_database" {
  secret_id = google_secret_manager_secret.runtime["database-url"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.jobs.email}"
}

resource "google_sql_database_instance" "primary" {
  name                = "${var.service_name}-postgres"
  region              = var.region
  database_version    = "POSTGRES_17"
  deletion_protection = true

  settings {
    tier              = var.cloud_sql_tier
    availability_type = "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 10

    ip_configuration {
      ipv4_enabled = true
      require_ssl  = true
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
    }
  }
  depends_on = [google_project_service.required]
}

resource "google_sql_database" "application" {
  name     = "agent_data_oracle"
  instance = google_sql_database_instance.primary.name
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${var.service_name}-github"
  display_name              = "Agent Data Oracle GitHub Actions"
  description               = "GitHub OIDC identities permitted to deploy this repository"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Actions OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == '${var.github_repository}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

data "google_project" "current" {}

resource "google_service_account_iam_member" "github_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/attribute.repository/${var.github_repository}"
}

resource "google_cloud_run_v2_service" "web" {
  count    = var.create_application ? 1 : 0
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.web.email
    max_instance_request_concurrency = 10

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.primary.connection_name]
      }
    }

    containers {
      image = var.bootstrap_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 3
        http_get {
          path = "/live"
          port = 8080
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name  = "APP_ENV"
        value = "production"
      }
      env {
        name  = "PUBLIC_ORIGIN"
        value = var.public_origin
      }
      dynamic "env" {
        for_each = google_secret_manager_secret.runtime
        content {
          name = upper(replace(env.key, "-", "_"))
          value_source {
            secret_key_ref {
              secret  = env.value.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [
    google_project_iam_member.web_cloud_sql_client,
    google_secret_manager_secret_iam_member.web_runtime,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.create_application ? 1 : 0
  name     = google_cloud_run_v2_service.web[0].name
  location = google_cloud_run_v2_service.web[0].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "deployer" {
  count    = var.create_application ? 1 : 0
  name     = google_cloud_run_v2_service.web[0].name
  location = google_cloud_run_v2_service.web[0].location
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_cloud_run_v2_job" "migrate" {
  count    = var.create_application ? 1 : 0
  name     = "${var.service_name}-migrate"
  location = var.region

  template {
    template {
      service_account = google_service_account.jobs.email
      timeout         = "600s"
      max_retries     = 0

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.primary.connection_name]
        }
      }

      containers {
        image   = var.bootstrap_image
        command = ["agent-data-oracle"]
        args    = ["migrate"]

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.runtime["database-url"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [
    google_project_iam_member.jobs_cloud_sql_client,
    google_secret_manager_secret_iam_member.jobs_database,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "deployer" {
  count    = var.create_application ? 1 : 0
  name     = google_cloud_run_v2_job.migrate[0].name
  location = google_cloud_run_v2_job.migrate[0].location
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.deployer.email}"
}
