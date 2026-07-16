/**
 * DCGM Exporter and GPUDirect/RDMA Installer Module
 *
 * This module applies DCGM monitoring and GPUDirect/RDMA installer manifests
 * to the cluster post-provisioning. It ensures GPU nodes are instrumented for
 * metrics collection and fabric-ready.
 *
 * DEPLOY-TIME CAVEATS:
 * 1. Operator must ensure kubectl context/credentials for the target cluster are
 *    active before apply (local-exec blocks assume an authenticated context).
 * 2. The GPUDirect/RDMA installer manifest URLs are version-specific and depend
 *    on the fabric type (TCPX/TCPXO/RDMA). Verify these URLs against the pinned
 *    Cluster Toolkit A3/A4 examples before applying.
 * 3. Installer DaemonSets are typically per-fabric; apply the appropriate set
 *    based on the enabled GPU pools (h100-high→TCPX, h100-mega→TCPXO,
 *    h200-ultra/b200→RDMA).
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
  description = "Name of the GKE cluster to apply manifests to"
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

variable "enabled_fabrics" {
  description = "List of enabled fabric types (tcpx, tcpxo, rdma) for installer selection"
  type        = list(string)
  default     = ["tcpx", "tcpxo", "rdma"]
}

variable "dcgm_manifest_path" {
  description = "Path to the DCGM exporter manifest file"
  type        = string
  default     = "../../../manifests/dcgm/dcgm-exporter.yaml"
}

# DCGM Exporter deployment
resource "null_resource" "dcgm_exporter" {
  triggers = {
    manifest_sha = filesha256("${path.module}/${var.dcgm_manifest_path}")
    cluster_name = var.cluster_name
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Applying DCGM exporter to cluster ${var.cluster_name}..."
      gcloud container clusters get-credentials ${var.cluster_name} \
        --location=${var.cluster_location} \
        --project=${var.project_id}
      kubectl apply -f ${path.module}/${var.dcgm_manifest_path}
    EOT
  }
}

# GPUDirect/RDMA installer placeholders
# DEPLOY-TIME ACTION REQUIRED: Uncomment and configure the appropriate installer
# based on enabled GPU pools. Example installer URLs (verify against Cluster Toolkit):
#
# For TCPX (h100-high):
# resource "null_resource" "gpudirect_tcpx" {
#   count = contains(var.enabled_fabrics, "tcpx") ? 1 : 0
#   provisioner "local-exec" {
#     command = <<-EOT
#       kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/<PINNED_CLUSTER_TOOLKIT_REF>/gpudirect-tcpx/nccl-plugin-installer-daemonset.yaml
#       kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/<PINNED_CLUSTER_TOOLKIT_REF>/gpudirect-tcpx/nccl-tcpx-device-injector-daemonset.yaml
#     EOT
#   }
#   depends_on = [null_resource.dcgm_exporter]
# }
#
# For TCPXO (h100-mega):
# resource "null_resource" "gpudirect_tcpxo" {
#   count = contains(var.enabled_fabrics, "tcpxo") ? 1 : 0
#   provisioner "local-exec" {
#     command = <<-EOT
#       kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/<PINNED_CLUSTER_TOOLKIT_REF>/gpudirect-tcpxo/nccl-plugin-installer-daemonset.yaml
#       kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/<PINNED_CLUSTER_TOOLKIT_REF>/gpudirect-tcpxo/nccl-tcpxo-device-injector-daemonset.yaml
#     EOT
#   }
#   depends_on = [null_resource.dcgm_exporter]
# }
#
# For RDMA (h200-ultra, b200):
# resource "null_resource" "gpudirect_rdma" {
#   count = contains(var.enabled_fabrics, "rdma") ? 1 : 0
#   provisioner "local-exec" {
#     command = <<-EOT
#       kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/<PINNED_CLUSTER_TOOLKIT_REF>/gpudirect-rdma/nccl-plugin-installer-daemonset.yaml
#       kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/<PINNED_CLUSTER_TOOLKIT_REF>/gpudirect-rdma/rdma-device-injector-daemonset.yaml
#     EOT
#   }
#   depends_on = [null_resource.dcgm_exporter]
# }

output "dcgm_namespace" {
  description = "Namespace where DCGM exporter is deployed"
  value       = "dcgm"
}

output "dcgm_service" {
  description = "DCGM exporter service endpoint"
  value       = "dcgm-exporter.dcgm.svc.cluster.local:9400"
}
