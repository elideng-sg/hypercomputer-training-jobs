# Durable Reserved GPUs (beyond DWS Flex-Start's 7 days)

DWS **Flex-Start** nodes have a hard **7-day** `maxRunDuration` — no holder or trick
keeps a Flex-Start node past that. To keep GPUs longer (e.g. a **6-month** demo/debug
lab), use a **Compute Engine reservation** consumed by a **standard** node pool
(`lab/blueprints/pools/h100-reserved.yaml`). Reserved nodes have **no run-duration cap**
and are not idle-scaled — they persist until you delete the pool/reservation, and you
get **guaranteed capacity** (no stockout wait).

## Zone layout: keep all 3 nodes in ONE zone

For this lab's purpose (demo + troubleshooting/debug), **book all 3 A3 nodes in a single
zone** (e.g. `us-central1-a`) with a **COMPACT placement policy** — do NOT spread 1-per-zone.

Why: the high-bandwidth A3 GPU fabric (GPUDirect-TCPX/TCPXO/RDMA) **does not span zones**;
cross-zone nodes fall back to slow TCP. So:

- **1-per-zone** → three isolated single-node boxes; you can't reproduce **multi-node**
  training, inter-node all-reduce, GPUDirect, rail alignment, or NCCL-hang-across-nodes —
  the richest A3-specific debugging surface.
- **3-in-one-zone (+ COMPACT)** → you get both: three independent single-node experiments
  **and** a real 24-GPU multi-node cluster over the fast fabric. Strictly more to demo/debug.

## 1. Create the reservation (3× a3-highgpu-8g, one zone)

```bash
gcloud compute reservations create a3-h100-res \
  --project=hdlab-elideng \
  --zone=us-central1-a \
  --require-specific-reservation \
  --vm-count=3 \
  --machine-type=a3-highgpu-8g \
  --accelerator=type=nvidia-h100-80gb,count=8
```

- The reservation **name must match** `reserved_reservation_name` in the blueprint
  (default `a3-h100-res`).
- H100 reservations are capacity-constrained: fulfilling a 3-node H100 reservation
  usually requires a **capacity request through your GCP account team**. If you need a
  guaranteed *future* window, request it via **DWS Calendar mode** instead of on-demand.
- For a compact multi-node topology you may also attach a COMPACT resource policy /
  placement to the reservation; the node pool already requests COMPACT placement.

## 2. Enable the reserved pool

```bash
cd lab
make up REGION=us-central1 ENABLED_POOLS=h100-reserved   # add other pools as needed
```

The `h100-reserved` pool is a standard (non-DWS) pool pinned to the reservation with
`min=max=3` nodes and COMPACT placement, so all 3 A3 nodes come up and stay up.

## 3. Lifecycle & cost (6 months)

- A reservation **persists until you delete it** and bills at the on-demand rate whether
  used or not — there is no built-in "6-month" term. For a 6-month lab: create it, keep it
  6 months, then `gcloud compute reservations delete a3-h100-res --zone=us-central1-a`.
- Committed Use Discounts (CUDs) are **1-year or 3-year** only, so there is no 6-month
  commitment discount; the reservation runs at on-demand pricing for the 6 months.
- Reserved nodes are not reclaimed, so DWS holders are not needed here. (The house rule —
  never leave a GPU node idle/unheld after work — is automatically satisfied: with
  `min=3` the pool simply stays up.)

## 4. Migrating off the Flex-Start node

You **cannot** convert an existing Flex-Start node to reserved. Stand up this reserved
pool separately and move your workload/checkpoints **before** the Flex-Start node's 7-day
expiry, then release the Flex-Start request.
