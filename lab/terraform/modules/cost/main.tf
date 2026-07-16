variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "billing_account" {
  description = "Billing account ID for budget alerts (optional; budget creation gated if empty)"
  type        = string
  default     = ""
}

variable "budget_amount" {
  description = "Monthly budget amount in USD"
  type        = number
  default     = 10000
}

variable "alert_thresholds" {
  description = "Thresholds for budget alerts (percentage)"
  type        = list(number)
  default     = [0.5, 0.75, 0.9, 1.0]
}

variable "cluster_name" {
  description = "GKE cluster name for cost allocation"
  type        = string
  default     = "hpc-lab"
}

variable "region" {
  description = "Region for cluster location placeholder"
  type        = string
  default     = "us-central1"
}

# Budget alert (gated on billing_account being provided)
resource "google_billing_budget" "lab_budget" {
  count           = var.billing_account != "" ? 1 : 0
  billing_account = var.billing_account
  display_name    = "HPC Lab Budget Alert"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_amount)
    }
  }

  dynamic "threshold_rules" {
    for_each = var.alert_thresholds
    content {
      threshold_percent = threshold_rules.value
    }
  }

  all_updates_rule {
    monitoring_notification_channels = []
    disable_default_iam_recipients   = false
  }
}

# GKE cost allocation labels
resource "google_container_cluster" "cost_allocation" {
  count    = 0 # Placeholder: assumes cluster already exists; use data source or import
  name     = var.cluster_name
  location = var.region

  resource_labels = {
    cost-center = "hpc-lab"
    environment = "research"
  }

  cost_management_config {
    enabled = true
  }
}

output "budget_id" {
  description = "Budget resource ID (empty if billing_account not provided)"
  value       = var.billing_account != "" ? google_billing_budget.lab_budget[0].id : ""
}
