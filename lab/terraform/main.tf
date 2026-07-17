/**
 * Post-Cluster Configuration Layer
 *
 * This root module applies Kueue, DCGM, and cost guardrails AFTER the
 * Cluster Toolkit-generated cluster stage (`build/hpc-lab/primary`).
 *
 * DEPLOY SEQUENCE:
 * 1. `make up` applies the gcluster-generated cluster terraform
 * 2. `make configure` applies this root module (Kueue/DCGM/cost)
 *
 * All modules use local-exec kubectl/gcloud and assume the cluster exists
 * and kubectl context is authenticated.
 */

locals {
  cluster_location = var.cluster_location != "" ? var.cluster_location : var.region
}

module "kueue" {
  source = "./modules/kueue"

  project_id       = var.project_id
  cluster_name     = var.cluster_name
  cluster_location = local.cluster_location
}

module "dcgm" {
  source = "./modules/dcgm"

  project_id       = var.project_id
  cluster_name     = var.cluster_name
  cluster_location = local.cluster_location
}

module "cost" {
  source = "./modules/cost"

  project_id      = var.project_id
  region          = var.region
  billing_account = var.billing_account
  cluster_name    = var.cluster_name
}
