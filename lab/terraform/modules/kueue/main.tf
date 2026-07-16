/**
 * Kueue Installation and Configuration Module
 *
 * This module installs Kueue (pinned version v0.8.1) and applies per-GPU-type
 * resource flavors, cluster queues, local queues, and DWS ProvisioningRequest
 * admission configuration.
 *
 * ARCHITECTURE:
 * - 6 ResourceFlavors (l4, a100, h100-high, h100-mega, h200-ultra, b200)
 *   with nodeLabels matching lab.gpu/type from Task 4 pools.
 * - 6 ClusterQueues (one per flavor) in a shared "gpu" cohort, each with
 *   dws-flex admission check and quotas for nvidia.com/gpu, cpu, memory.
 * - Per-team LocalQueues (default: team-a) pointing at each ClusterQueue.
 * - DWS integration via ProvisioningRequestConfig + AdmissionCheck:
 *   queued-provisioning.gke.io manages nvidia.com/gpu resources.
 *
 * DEPLOY-TIME CAVEATS:
 * 1. Kueue version is pinned to v0.8.1 (verify compatibility with GKE version).
 * 2. Multi-team expansion: the current manifests hard-code team-a. For multiple
 *    teams, consider templating the local-queues.yaml and namespaces.yaml at
 *    apply time (e.g., Helm, Kustomize overlays, or a script loop).
 * 3. Quota tuning: nominal quotas are set to 16 GPU / 400 CPU / 3000Gi mem per
 *    type. Adjust based on actual pool max_nodes * GPUs-per-node before production.
 * 4. This module uses null_resource + local-exec for offline validation; for
 *    production use, consider migrating to kubectl provider or Helm.
 */

terraform {
  required_version = ">= 1.5"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

variable "cluster_name" {
  description = "Name of the GKE cluster to install Kueue on"
  type        = string
}

variable "cluster_location" {
  description = "Location (region or zone) of the GKE cluster"
  type        = string
}

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "kueue_version" {
  description = "Kueue version to install (pinned)"
  type        = string
  default     = "v0.8.1"
}

variable "kueue_manifest_dir" {
  description = "Path to the directory containing Kueue custom manifests"
  type        = string
  default     = "../../manifests/kueue"
}

# Install Kueue CRDs and controller (pinned version)
resource "null_resource" "kueue_install" {
  triggers = {
    kueue_version = var.kueue_version
    cluster_name  = var.cluster_name
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Installing Kueue ${var.kueue_version} on cluster ${var.cluster_name}..."
      gcloud container clusters get-credentials ${var.cluster_name} \
        --location=${var.cluster_location} \
        --project=${var.project_id}
      kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/${var.kueue_version}/manifests.yaml
    EOT
  }
}

# Apply DWS ProvisioningRequest admission configuration
resource "null_resource" "kueue_provisioning_config" {
  triggers = {
    manifest_sha = filesha256("${path.module}/${var.kueue_manifest_dir}/provisioning-config.yaml")
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Applying Kueue ProvisioningRequestConfig and AdmissionCheck..."
      kubectl apply -f ${path.module}/${var.kueue_manifest_dir}/provisioning-config.yaml
    EOT
  }

  depends_on = [null_resource.kueue_install]
}

# Apply ResourceFlavors
resource "null_resource" "kueue_flavors" {
  triggers = {
    manifest_sha = filesha256("${path.module}/${var.kueue_manifest_dir}/flavors.yaml")
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Applying Kueue ResourceFlavors..."
      kubectl apply -f ${path.module}/${var.kueue_manifest_dir}/flavors.yaml
    EOT
  }

  depends_on = [null_resource.kueue_install]
}

# Apply ClusterQueues
resource "null_resource" "kueue_cluster_queues" {
  triggers = {
    manifest_sha = filesha256("${path.module}/${var.kueue_manifest_dir}/cluster-queues.yaml")
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Applying Kueue ClusterQueues..."
      kubectl apply -f ${path.module}/${var.kueue_manifest_dir}/cluster-queues.yaml
    EOT
  }

  depends_on = [
    null_resource.kueue_flavors,
    null_resource.kueue_provisioning_config
  ]
}

# Apply team namespaces
resource "null_resource" "kueue_namespaces" {
  triggers = {
    manifest_sha = filesha256("${path.module}/${var.kueue_manifest_dir}/namespaces.yaml")
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Applying Kueue team namespaces..."
      kubectl apply -f ${path.module}/${var.kueue_manifest_dir}/namespaces.yaml
    EOT
  }

  depends_on = [null_resource.kueue_install]
}

# Apply LocalQueues
resource "null_resource" "kueue_local_queues" {
  triggers = {
    manifest_sha = filesha256("${path.module}/${var.kueue_manifest_dir}/local-queues.yaml")
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Applying Kueue LocalQueues..."
      kubectl apply -f ${path.module}/${var.kueue_manifest_dir}/local-queues.yaml
    EOT
  }

  depends_on = [
    null_resource.kueue_cluster_queues,
    null_resource.kueue_namespaces
  ]
}

output "kueue_version" {
  description = "Installed Kueue version"
  value       = var.kueue_version
}

output "resource_flavors" {
  description = "List of GPU ResourceFlavors"
  value       = ["l4", "a100", "h100-high", "h100-mega", "h200-ultra", "b200"]
}

output "cluster_queues" {
  description = "List of ClusterQueues"
  value       = ["cq-l4", "cq-a100", "cq-h100-high", "cq-h100-mega", "cq-h200-ultra", "cq-b200"]
}

output "admission_check" {
  description = "DWS ProvisioningRequest admission check name"
  value       = "dws-flex"
}
