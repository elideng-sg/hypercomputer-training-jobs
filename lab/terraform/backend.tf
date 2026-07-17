terraform {
  backend "gcs" {} # bucket/prefix supplied via -backend-config at init
  required_providers {
    google      = { source = "hashicorp/google", version = ">= 5.30" }
    google-beta = { source = "hashicorp/google-beta", version = ">= 5.30" }
  }
}
