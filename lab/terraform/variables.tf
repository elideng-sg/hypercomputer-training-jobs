variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "enabled_pools" {
  type = list(string)
}

variable "teams" {
  type    = list(string)
  default = ["team-a"]
}

variable "max_run_duration_seconds" {
  type    = number
  default = 86400
}

variable "cluster_name" {
  type    = string
  default = "hpc-lab"
}

variable "cluster_location" {
  type        = string
  default     = ""
  description = "Location of the cluster (defaults to region if not set)"
}

variable "billing_account" {
  type        = string
  default     = ""
  description = "Billing account ID for budget alerts (optional; budget creation gated if empty)"
}
