# project_id: GCP project for the AI Hypercomputer lab
project_id = "your-project-id"

# region: Primary region for clusters and resources
region = "us-central1"

# enabled_pools: GPU pool types to provision (l4, a100, h100-high, h100-mega, h200-ultra, b200)
enabled_pools = ["l4", "a100", "h100-high", "h100-mega"]

# teams: List of team identifiers for multi-tenant resource allocation
teams = ["team-a"]

# max_run_duration_seconds: Maximum runtime for training jobs (default: 24 hours)
max_run_duration_seconds = 86400
