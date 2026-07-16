#!/usr/bin/env python3
import subprocess, sys, json
def sh(c): return subprocess.check_output(c, shell=True).decode()
if len(sys.argv) < 2:
    print("Usage: assert_zero_gpu.py <project-id>"); sys.exit(2)
proj = sys.argv[1]
vms = json.loads(sh(f"gcloud compute instances list --project {proj} "
  f"--filter='machineType~(a2|a3|a4|g2)' --format=json"))
gpu_vms = [v['name'] for v in vms]
prs = sh(f"kubectl get provisioningrequests -A --no-headers 2>/dev/null || true").strip()
if gpu_vms or prs:
    print(f"[FAIL] GPU VMs={gpu_vms} PRs present={bool(prs)}"); sys.exit(1)
print("[OK] zero GPU nodes, zero ProvisioningRequests"); sys.exit(0)
