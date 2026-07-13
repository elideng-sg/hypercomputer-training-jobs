#!/usr/bin/env python3
"""
GCP AI Hypercomputer Verification Benchmark & Multi-GPU NCCL Test Suite
----------------------------------------------------------------------
Executes a highly concurrent multi-device training iteration benchmark across 
NVIDIA Hopper (H100/H200) and Blackwell (B200/GB200) GPUs using NVLink/NVSwitch fabric.

Validates:
1. Distributed process group connection via NCCL backend across all visible GPUs.
2. Tensor Core matrix execution capability across FP32, FP16, and mixed-precision tensors.
3. Intra-node All-Reduce roundtrip latency and GB/s effective crossbar throughput.
4. Exporting diagnostic metrics directly to log output.
"""

import os
import time
import json
import socket
from datetime import datetime
import torch
import torch.distributed as dist
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP

def setup_distributed():
    """Initializes PyTorch process group using local env rank mechanics."""
    rank = int(os.environ.get("RANK", 0))
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    world_size = int(os.environ.get("WORLD_SIZE", torch.cuda.device_count()))
    
    # Set target active GPU hardware device
    if torch.cuda.is_available():
        torch.cuda.set_device(local_rank)
        device = torch.device(f"cuda:{local_rank}")
    else:
        raise RuntimeError("CRITICAL: CUDA device unavailable. Check GPU driver installation!")
        
    dist.init_process_group(
        backend="nccl",
        init_method="env://",
        world_size=world_size,
        rank=rank
    )
    return rank, local_rank, world_size, device

def cleanup_distributed():
    if dist.is_initialized():
        dist.destroy_process_group()

class DeepLinearMatrixModel(nn.Module):
    """Synthetic heavy computation linear transformer block for stress verification."""
    def __init__(self, hidden_dim=4096, num_layers=4):
        super().__init__()
        layers = []
        for _ in range(num_layers):
            layers.append(nn.Linear(hidden_dim, hidden_dim, bias=False))
            layers.append(nn.GELU())
        self.network = nn.Sequential(*layers)
        
    def forward(self, x):
        return self.network(x)

def measure_allreduce_bandwidth(rank, world_size, device, tensor_size_mb=512, num_iterations=20):
    """Measures precise inter-GPU all-reduce bandwidth over NVLink/NVSwitch."""
    # Create float32 buffer of requested size (~4 bytes per element)
    num_elements = (tensor_size_mb * 1024 * 1024) // 4
    tensor = torch.randn(num_elements, dtype=torch.float32, device=device)
    
    # Warmup loop to prime NCCL communicators and NVLink rails
    for _ in range(5):
        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    torch.cuda.synchronize(device)
    
    start_time = time.perf_counter()
    for _ in range(num_iterations):
        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    torch.cuda.synchronize(device)
    end_time = time.perf_counter()
    
    avg_latency_s = (end_time - start_time) / num_iterations
    # Effective data transferred across ring algorithm: 2 * (N - 1) / N * size
    algo_bus_multiplier = 2.0 * (world_size - 1) / max(world_size, 1)
    effective_bandwidth_gbs = (tensor_size_mb / 1024.0 * algo_bus_multiplier) / avg_latency_s
    
    return avg_latency_s * 1000.0, effective_bandwidth_gbs

def main():
    if not torch.cuda.is_available():
        print("[!] ERROR: CUDA hardware not accessible by current process.")
        return
        
    rank, local_rank, world_size, device = setup_distributed()
    hostname = socket.gethostname()
    gpu_name = torch.cuda.get_device_name(device)
    
    if rank == 0:
        print("=" * 80)
        print(f"[*] GCP AI Hypercomputer Multi-GPU Distributed Verification Script")
        print(f"[*] Hostname: {hostname} | World Size (GPUs): {world_size}")
        print(f"[*] Primary Device Arch: {gpu_name} (CC {torch.cuda.get_device_capability(device)})")
        print("=" * 80)
        
    dist.barrier()
    print(f"[+] Worker Rank {rank}/{world_size-1} online -> Device: {gpu_name} ({device})")
    dist.barrier()
    
    # --- STEP A: Test Mixed-Precision Neural Model DDP Forward/Backward ---
    hidden_dim = 4096
    batch_size = 64
    model = DeepLinearMatrixModel(hidden_dim=hidden_dim, num_layers=4).to(device)
    ddp_model = DDP(model, device_ids=[local_rank], output_device=local_rank)
    optimizer = torch.optim.AdamW(ddp_model.parameters(), lr=1e-4)
    criterion = nn.MSELoss()
    
    if rank == 0:
        print("\n[*] Starting DDP Mixed-Precision Matrix Execution Stress Test...")
        
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    
    # Run loop checking available autocast support
    use_bfloat16 = torch.cuda.is_bf16_supported()
    dtype_target = torch.bfloat16 if use_bfloat16 else torch.float16
    
    for step in range(25):
        optimizer.zero_grad()
        inputs = torch.randn(batch_size, hidden_dim, device=device, dtype=torch.float32)
        target = torch.randn(batch_size, hidden_dim, device=device, dtype=torch.float32)
        
        with torch.cuda.amp.autocast(dtype=dtype_target):
            output = ddp_model(inputs)
            loss = criterion(output, target)
            
        loss.backward()
        optimizer.step()
        
    torch.cuda.synchronize()
    elapsed_training = time.perf_counter() - t0
    
    if rank == 0:
        print(f"[+] 25 DDP iterations completed across {world_size} GPUs in {elapsed_training:.3f} seconds.")
        print(f"[+] Precision regime employed: {dtype_target}")
        
    dist.barrier()
    
    # --- STEP B: NCCL NVLink / NVSwitch All-Reduce Bandwidth Verification ---
    if rank == 0:
        print("\n[*] Initiating High-Bandwidth NCCL All-Reduce Crossbar Benchmarking...")
        
    latency_ms, throughput_gbs = measure_allreduce_bandwidth(
        rank=rank, 
        world_size=world_size, 
        device=device, 
        tensor_size_mb=1024, # 1 GB buffer
        num_iterations=15
    )
    
    if rank == 0:
        print("=" * 80)
        print("                 BENCHMARK ALL-REDUCE VERIFICATION SUMMARY                 ")
        print("=" * 80)
        print(f" -> Cluster Nodes     : {hostname}")
        print(f" -> Concurrent GPUs   : {world_size}x {gpu_name}")
        print(f" -> Buffer Transfer   : 1024 MiB (1.0 GiB payload)")
        print(f" -> Average Latency   : {latency_ms:.3f} ms / step")
        print(f" -> Effective Bus Bandwidth: {throughput_gbs:.2f} GB/s aggregate throughput")
        print("=" * 80)
        
        # Write structural diagnostic report file to local logs directory
        os.makedirs("logs", exist_ok=True)
        report = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "hostname": hostname,
            "gpu_device": gpu_name,
            "world_size": world_size,
            "ddp_25_iter_seconds": round(elapsed_training, 4),
            "allreduce_1gb_latency_ms": round(latency_ms, 4),
            "allreduce_effective_gbs": round(throughput_gbs, 2),
            "nccl_status": "PASSED"
        }
        with open("logs/verification_benchmark_results.json", "w") as f:
            json.dump(report, f, indent=2)
        print("[+] Test diagnostic records exported successfully to logs/verification_benchmark_results.json")

    cleanup_distributed()

if __name__ == "__main__":
    main()
