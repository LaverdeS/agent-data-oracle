variable "project_id" {
  description = "The founder-owned production Google Cloud project ID."
  type        = string
}

variable "github_repository" {
  description = "The GitHub repository allowed to federate as the deployer."
  type        = string
}

variable "region" {
  description = "The sole application region for this usage-learning phase."
  type        = string
  default     = "europe-west3"
}

variable "service_name" {
  description = "Cloud Run service and resource prefix."
  type        = string
  default     = "agent-data-oracle"
}

variable "bootstrap_image" {
  description = "A digest-pinned image in the regional Artifact Registry repository."
  type        = string
  default     = ""

  validation {
    condition     = var.create_application == false || can(regex("@sha256:", var.bootstrap_image))
    error_message = "bootstrap_image must be digest-pinned when create_application is true."
  }
}

variable "create_application" {
  description = "Create Cloud Run resources after the first image and secret versions exist."
  type        = bool
  default     = false
}

variable "public_origin" {
  description = "Canonical generated run.app HTTPS origin; bootstrap.invalid allows first service creation."
  type        = string
  default     = "https://bootstrap.invalid"

  validation {
    condition     = can(regex("^https://[^/]+$", var.public_origin))
    error_message = "public_origin must be an HTTPS origin without a path."
  }
}

variable "cloud_sql_tier" {
  description = "The selected single-zone shared-core Cloud SQL tier."
  type        = string
  default     = "db-f1-micro"
}
