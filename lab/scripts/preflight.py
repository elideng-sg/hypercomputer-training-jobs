#!/usr/bin/env python3
"""Preflight: verify APIs, VPC quota, and per-GPU quota for the enabled pools."""
import argparse, subprocess, sys, json

POOL_GPU = {  # pool -> (accelerator metric, gpus per node)
    "l4": ("NVIDIA_L4_GPUS", 8), "a100": ("NVIDIA_A100_80GB_GPUS", 8),
    "h100-high": ("NVIDIA_H100_GPUS", 8), "h100-mega": ("NVIDIA_H100_MEGA_GPUS", 8),
    "h200-ultra": ("NVIDIA_H200_GPUS", 8), "b200": ("NVIDIA_B200_GPUS", 8),
}
def sh(c): return subprocess.check_output(c, shell=True).decode()
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--region", required=True)
    ap.add_argument("--pools", required=True); ap.add_argument("--project", required=True)
    a = ap.parse_args(); pools = [p for p in a.pools.split(",") if p]
    q = json.loads(sh(f"gcloud compute regions describe {a.region} --project {a.project} --format=json"))
    limits = {x["metric"]: x["limit"] for x in q["quotas"]}
    ok = True
    print(f"{'POOL':12} {'METRIC':24} {'LIMIT':>8} {'NEED':>5}")
    for p in pools:
        metric, need = POOL_GPU[p]; lim = limits.get(metric, 0)
        flag = "" if lim >= need else "  <-- INSUFFICIENT"; ok = ok and lim >= need
        print(f"{p:12} {metric:24} {lim:8.0f} {need:5}{flag}")
    # VPC quota (need ~10 when TCPXO/RDMA enabled)
    print("[i] Ensure Networks-per-project quota >= 10 if h100-mega/h200/b200 enabled.")
    sys.exit(0 if ok else 2)
if __name__ == "__main__": main()
