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
