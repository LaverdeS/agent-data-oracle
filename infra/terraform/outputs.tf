output "artifact_registry_repository" {
  value = google_artifact_registry_repository.application.name
}

output "cloud_sql_connection_name" {
  value = google_sql_database_instance.primary.connection_name
}

output "deployer_service_account" {
  value = google_service_account.deployer.email
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "service_url" {
  value = var.create_application ? google_cloud_run_v2_service.web[0].uri : null
}
