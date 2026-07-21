#!/usr/bin/env python3
import os
import argparse
import torch
import torch.distributed as dist
import torch.nn as nn
from torch.utils.data import IterableDataset, DataLoader
try:
    from google.cloud import storage
except ImportError:
    storage = None
from urllib.parse import urlparse
import time
import math
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import wandb
except ImportError:
    wandb = None

# --- Inference and Serving Support ---
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

def generate_text(model, idx, max_new_tokens, context_size, temperature=1.0, repetition_penalty=1.0):
    for _ in range(max_new_tokens):
        idx_cond = idx[:, -context_size:]
        with torch.no_grad():
            logits = model(idx_cond)
        logits = logits[:, -1, :]
        
        if repetition_penalty != 1.0:
            for token_id in set(idx[0].tolist()):
                val = logits[0, token_id].item()
                if val > 0:
                    logits[0, token_id] = val / repetition_penalty
                else:
                    logits[0, token_id] = val * repetition_penalty
                    
        if temperature > 0:
            logits = logits / temperature
            probs = torch.softmax(logits, dim=-1)
            idx_next = torch.multinomial(probs, num_samples=1)
        else:
            idx_next = torch.argmax(logits, dim=-1, keepdim=True)
            
        idx = torch.cat((idx, idx_next), dim=1)
    return idx

class GPT2ServingHandler(BaseHTTPRequestHandler):
    def _set_headers(self, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        
    def do_GET(self):
        if self.path in ('/', '/healthz'):
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "healthy"}).encode('utf-8'))
        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not Found"}).encode('utf-8'))
            
    def do_POST(self):
        if self.path == '/predict':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                import tiktoken
                data = json.loads(post_data.decode('utf-8'))
                prompt = data.get("prompt", "")
                max_new_tokens = int(data.get("max_new_tokens", 50))
                temperature = float(data.get("temperature", 1.0))
                repetition_penalty = float(data.get("repetition_penalty", 1.0))
                
                enc = tiktoken.get_encoding("gpt2")
                encoded = enc.encode(prompt)
                encoded_tensor = torch.tensor(encoded).unsqueeze(0).to(global_device)
                
                context_size = GPT_CONFIG_124M["context_length"]
                with torch.no_grad():
                    token_ids = generate_text(
                        model=global_model,
                        idx=encoded_tensor,
                        max_new_tokens=max_new_tokens,
                        context_size=context_size,
                        temperature=temperature,
                        repetition_penalty=repetition_penalty
                    )
                
                decoded = enc.decode(token_ids.squeeze(0).tolist())
                
                self._set_headers(200)
                response = {"generated_text": decoded}
                self.wfile.write(json.dumps(response).encode('utf-8'))
            except Exception as e:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not Found"}).encode('utf-8'))

def run_serving_server(port, model_instance, device_instance):
    global global_model, global_device
    global_model = model_instance
    global_device = device_instance
    
    server_address = ('', port)
    httpd = HTTPServer(server_address, GPT2ServingHandler)
    print(f"[*] Starting GPT-2 prediction serving endpoint on port {port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    print("[*] Serving endpoint stopped.")

# ==============================================================================
# GPT-2 Model Architecture & Parallel Layers (from models.py)
# ==============================================================================

GPT_CONFIG_124M = {
    "vocab_size": 50257,
    "context_length": 1024,
    "emb_dim": 768,
    "n_heads": 12,
    "n_layers": 12,
    "drop_rate": 0.1,
    "qkv_bias": True
}

class LayerNorm(nn.Module):
    def __init__(self, emb_dim):
        super().__init__()
        self.eps = 1e-5
        self.scale = nn.Parameter(torch.ones(emb_dim))
        self.shift = nn.Parameter(torch.zeros(emb_dim))

    def forward(self, x):
        mean = x.mean(dim=-1, keepdim=True)
        var = x.var(dim=-1, keepdim=True, unbiased=False)
        norm_x = (x - mean) / torch.sqrt(var + self.eps)
        return self.scale * norm_x + self.shift

class GELU(nn.Module):
    def forward(self, x):
        return 0.5 * x * (1 + torch.tanh(
            torch.sqrt(torch.tensor(2.0 / torch.pi)) *
            (x + 0.044715 * torch.pow(x, 3))
        ))


class ColumnParallelLinear(nn.Module):
    def __init__(self, in_features, out_features, bias=True, tp_group=None):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.tp_group = tp_group
        self.tp_size = dist.get_world_size(tp_group) if (dist is not None and tp_group is not None) else 1
        
        assert out_features % self.tp_size == 0, f"out_features ({out_features}) must be divisible by TP size ({self.tp_size})"
        self.sharded_out_features = out_features // self.tp_size
        
        self.weight = nn.Parameter(torch.empty(self.sharded_out_features, in_features))
        if bias:
            self.bias = nn.Parameter(torch.empty(self.sharded_out_features))
        else:
            self.register_parameter('bias', None)
            
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in) if fan_in > 0 else 0
            nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, x):
        return nn.functional.linear(x, self.weight, self.bias)

class RowParallelLinear(nn.Module):
    def __init__(self, in_features, out_features, bias=True, tp_group=None):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.tp_group = tp_group
        self.tp_size = dist.get_world_size(tp_group) if (dist is not None and tp_group is not None) else 1
        
        assert in_features % self.tp_size == 0, f"in_features ({in_features}) must be divisible by TP size ({self.tp_size})"
        self.sharded_in_features = in_features // self.tp_size
        
        self.weight = nn.Parameter(torch.empty(out_features, self.sharded_in_features))
        if bias:
            self.bias = nn.Parameter(torch.empty(out_features))
        else:
            self.register_parameter('bias', None)
            
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in) if fan_in > 0 else 0
            nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, x):
        out = nn.functional.linear(x, self.weight, None)
        if self.tp_size > 1:
            dist.all_reduce(out, op=dist.ReduceOp.SUM, group=self.tp_group)
        if self.bias is not None:
            out = out + self.bias
        return out

class ParallelMultiHeadAttention(nn.Module):
    def __init__(self, d_in, d_out, context_length, dropout, num_heads, qkv_bias=False, tp_group=None):
        super().__init__()
        self.num_heads = num_heads
        self.tp_group = tp_group
        self.tp_size = dist.get_world_size(tp_group) if (dist is not None and tp_group is not None) else 1
        
        assert d_out % num_heads == 0, "d_out must be divisible by n_heads"
        assert num_heads % self.tp_size == 0, f"num_heads ({num_heads}) must be divisible by TP size ({self.tp_size})"
        
        self.local_num_heads = num_heads // self.tp_size
        self.head_dim = d_out // num_heads
        self.d_out = d_out
        
        self.W_query = ColumnParallelLinear(d_in, d_out, bias=qkv_bias, tp_group=tp_group)
        self.W_key = ColumnParallelLinear(d_in, d_out, bias=qkv_bias, tp_group=tp_group)
        self.W_value = ColumnParallelLinear(d_in, d_out, bias=qkv_bias, tp_group=tp_group)
        self.out_proj = RowParallelLinear(d_out, d_out, bias=True, tp_group=tp_group)
        self.dropout_p = dropout

    def forward(self, x):
        b, num_tokens, d_in = x.shape
        keys = self.W_key(x).view(b, num_tokens, self.local_num_heads, self.head_dim)
        values = self.W_value(x).view(b, num_tokens, self.local_num_heads, self.head_dim)
        queries = self.W_query(x).view(b, num_tokens, self.local_num_heads, self.head_dim)
        
        keys = keys.transpose(1, 2)
        queries = queries.transpose(1, 2)
        values = values.transpose(1, 2)
        
        context_vec = torch.nn.functional.scaled_dot_product_attention(
            queries, keys, values,
            attn_mask=None,
            dropout_p=self.dropout_p if self.training else 0.0,
            is_causal=True
        )
        
        context_vec = context_vec.transpose(1, 2).reshape(b, num_tokens, self.local_num_heads * self.head_dim)
        context_vec = self.out_proj(context_vec)
        return context_vec

class ParallelFeedForward(nn.Module):
    def __init__(self, cfg, tp_group=None):
        super().__init__()
        emb_dim = cfg["emb_dim"]
        self.fc1 = ColumnParallelLinear(emb_dim, 4 * emb_dim, bias=True, tp_group=tp_group)
        self.gelu = GELU()
        self.fc2 = RowParallelLinear(4 * emb_dim, emb_dim, bias=True, tp_group=tp_group)

    def forward(self, x):
        x = self.fc1(x)
        x = self.gelu(x)
        x = self.fc2(x)
        return x

class ParallelTransformerBlock(nn.Module):
    def __init__(self, cfg, tp_group=None):
        super().__init__()
        self.att = ParallelMultiHeadAttention(
            d_in=cfg["emb_dim"],
            d_out=cfg["emb_dim"],
            context_length=cfg["context_length"],
            num_heads=cfg["n_heads"],
            dropout=cfg["drop_rate"],
            qkv_bias=cfg["qkv_bias"],
            tp_group=tp_group
        )
        self.ff = ParallelFeedForward(cfg, tp_group=tp_group)
        self.norm1 = LayerNorm(cfg["emb_dim"])
        self.norm2 = LayerNorm(cfg["emb_dim"])
        self.drop_shortcut = nn.Dropout(cfg["drop_rate"])

    def forward(self, x):
        shortcut = x
        x = self.norm1(x)
        x = self.att(x)
        x = self.drop_shortcut(x)
        x = x + shortcut

        shortcut = x
        x = self.norm2(x)
        x = self.ff(x)
        x = self.drop_shortcut(x)
        x = x + shortcut
        return x

class GPTModel(nn.Module):
    def __init__(self, cfg, checkpoint_activations=False, pp_rank=0, pp_size=1, tp_group=None):
        super().__init__()
        self.pp_rank = pp_rank
        self.pp_size = pp_size
        self.checkpoint_activations = checkpoint_activations
        
        block_fn = lambda: ParallelTransformerBlock(cfg, tp_group=tp_group)
            
        total_layers = cfg["n_layers"]
        assert total_layers % pp_size == 0, f"n_layers ({total_layers}) must be divisible by pp_size ({pp_size})"
        self.layers_per_stage = total_layers // pp_size
        self.start_layer = pp_rank * self.layers_per_stage
        self.end_layer = self.start_layer + self.layers_per_stage
            
        if pp_rank == 0:
            self.tok_emb = nn.Embedding(cfg["vocab_size"], cfg["emb_dim"])
            self.pos_emb = nn.Embedding(cfg["context_length"], cfg["emb_dim"])
            self.drop_emb = nn.Dropout(cfg["drop_rate"])
            
        self.trf_blocks = nn.ModuleList(
            [block_fn() for _ in range(self.start_layer, self.end_layer)]
        )
        
        if pp_rank == pp_size - 1:
            self.final_norm = LayerNorm(cfg["emb_dim"])
            self.out_head = nn.Linear(cfg["emb_dim"], cfg["vocab_size"], bias=True)

    def forward(self, in_idx_or_x):
        if self.pp_rank == 0:
            in_idx = in_idx_or_x
            batch_size, seq_len = in_idx.shape
            tok_embeds = self.tok_emb(in_idx)
            pos_embeds = self.pos_emb(torch.arange(seq_len, device=in_idx.device))
            x = tok_embeds + pos_embeds
            x = self.drop_emb(x)
        else:
            x = in_idx_or_x

        for block in self.trf_blocks:
            if self.checkpoint_activations and self.training:
                x = torch.utils.checkpoint.checkpoint(block, x, use_reentrant=False)
            else:
                x = block(x)

        if self.pp_rank == self.pp_size - 1:
            x = self.final_norm(x)
            logits = self.out_head(x)
            return logits
        else:
            return x

# ==============================================================================
# Pre-Tokenized Offline Dataset
# ==============================================================================

class GPTOfflineDataset(IterableDataset):
    def __init__(self, bin_path=None, stream_url=None, context_length=1024, rank=0, world_size=1, total_tokens=None):
        self.bin_path = bin_path
        self.stream_url = stream_url
        self.context_length = context_length
        self.rank = rank
        self.world_size = world_size
        self.total_tokens = total_tokens
        if self.stream_url:
            print(f"[Rank {rank}] Initializing GPTOfflineDataset in HTTP Streaming mode: {stream_url}...")
        else:
            print(f"[Rank {rank}] Initializing GPTOfflineDataset with binary file: {bin_path}...")

    def __iter__(self):
        import numpy as np
        
        if self.stream_url:
            worker_info = torch.utils.data.get_worker_info()
            if worker_info is None:
                worker_id = 0
                num_workers = 1
            else:
                worker_id = worker_info.id
                num_workers = worker_info.num_workers

            total_blocks = (self.total_tokens - 1) // self.context_length
            blocks_per_rank = total_blocks // self.world_size
            rank_start_block = self.rank * blocks_per_rank
            
            blocks_per_worker = blocks_per_rank // num_workers
            start_block = rank_start_block + worker_id * blocks_per_worker
            end_block = start_block + blocks_per_worker

            worker_tokens = (end_block - start_block) * self.context_length + 1
            start_byte = start_block * self.context_length * 2
            end_byte = start_byte + worker_tokens * 2 - 1

            import urllib.request
            req = urllib.request.Request(
                self.stream_url, 
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Range': f'bytes={start_byte}-{end_byte}'
                }
            )
            
            try:
                resp = urllib.request.urlopen(req)
                
                # Yield first block
                first_block_bytes = resp.read((self.context_length + 1) * 2)
                if len(first_block_bytes) < (self.context_length + 1) * 2:
                    return
                first_chunk = np.frombuffer(first_block_bytes, dtype=np.uint16)
                chunk_tensor = torch.tensor(first_chunk.astype(np.int64))
                yield chunk_tensor[:-1], chunk_tensor[1:]
                
                last_token = chunk_tensor[-1:]
                
                # Yield remaining blocks
                read_size = self.context_length * 2
                for _ in range(1, blocks_per_worker):
                    block_bytes = resp.read(read_size)
                    if len(block_bytes) < read_size:
                        break
                    np_chunk = np.frombuffer(block_bytes, dtype=np.uint16)
                    chunk_tensor = torch.tensor(np_chunk.astype(np.int64))
                    
                    full_tensor = torch.cat([last_token, chunk_tensor])
                    yield full_tensor[:-1], full_tensor[1:]
                    last_token = chunk_tensor[-1:]
                    
            except Exception as e:
                print(f"[Rank {self.rank} Worker {worker_id}] Error streaming from HTTP: {e}")
                raise e
        else:
            data = np.memmap(self.bin_path, dtype=np.uint16, mode='r')

            worker_info = torch.utils.data.get_worker_info()
            if worker_info is None:
                worker_id = 0
                num_workers = 1
            else:
                worker_id = worker_info.id
                num_workers = worker_info.num_workers

            total_blocks = (len(data) - 1) // self.context_length
            blocks_per_rank = total_blocks // self.world_size
            rank_start_block = self.rank * blocks_per_rank
            
            blocks_per_worker = blocks_per_rank // num_workers
            start_block = rank_start_block + worker_id * blocks_per_worker
            end_block = start_block + blocks_per_worker

            for block_idx in range(start_block, end_block):
                start_idx = block_idx * self.context_length
                end_idx = start_idx + self.context_length + 1

                chunk = data[start_idx:end_idx]
                chunk_tensor = torch.tensor(chunk.astype(np.int64))

                input_chunk = chunk_tensor[:-1]
                target_chunk = chunk_tensor[1:]
                yield input_chunk, target_chunk

# ==============================================================================
# Helper Functions
# ==============================================================================

def save_checkpoint(model, optimizer, output_uri, step, pp_coord=0, tp_coord=0):
    suffix = f"_pp{pp_coord}_tp{tp_coord}"
    local_model_path = os.path.join(output_uri, f"model{suffix}.pth") if not output_uri.startswith("gs://") else f"model{suffix}.pth"
    local_ckpt_path = os.path.join(output_uri, f"checkpoint{suffix}.pth") if not output_uri.startswith("gs://") else f"checkpoint{suffix}.pth"
    
    if not output_uri.startswith("gs://"):
        os.makedirs(output_uri, exist_ok=True)
        
    raw_model = model.module if hasattr(model, "module") else model
    raw_model = raw_model._orig_mod if hasattr(raw_model, "_orig_mod") else raw_model
    raw_state_dict = raw_model.state_dict()
    
    torch.save(raw_state_dict, local_model_path)
    print(f"Saved prediction-friendly model weights at step {step} for PP={pp_coord}, TP={tp_coord}")
    
    torch.save({
        "step": step,
        "model_state_dict": raw_state_dict,
        "optimizer_state_dict": optimizer.state_dict(),
    }, local_ckpt_path)
    print(f"Saved training-friendly checkpoint file at step {step} for PP={pp_coord}, TP={tp_coord}")
    
    if output_uri.startswith("gs://"):
        parsed = urlparse(output_uri)
        bucket_name = parsed.netloc
        blob_path = parsed.path.lstrip("/")
        
        if blob_path and not blob_path.endswith("/"):
            blob_path += "/"
            
        if storage is None:
            raise ImportError("google-cloud-storage package is required to save checkpoints to GCS (gs://...) paths. Run 'pip install google-cloud-storage' first.")
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        
        dest_model_name = os.path.join(blob_path, f"model{suffix}.pth")
        print(f"Uploading model weights to gs://{bucket_name}/{dest_model_name}...")
        blob = bucket.blob(dest_model_name)
        blob.upload_from_filename(local_model_path)
        
        dest_ckpt_name = os.path.join(blob_path, f"checkpoint{suffix}.pth")
        print(f"Uploading full checkpoint state to gs://{bucket_name}/{dest_ckpt_name}...")
        blob_ckpt = bucket.blob(dest_ckpt_name)
        blob_ckpt.upload_from_filename(local_ckpt_path)
        
        backup_name = os.path.join(blob_path, f"checkpoint{suffix}_step_{step}.pth")
        print(f"Saving historical checkpoint backup to gs://{bucket_name}/{backup_name}...")
        backup_blob = bucket.blob(backup_name)
        backup_blob.upload_from_filename(local_ckpt_path)
        print("Checkpoint uploads complete.")
        
        try:
            os.remove(local_model_path)
            os.remove(local_ckpt_path)
        except OSError:
            pass
    else:
        backup_path = os.path.join(output_uri, f"checkpoint{suffix}_step_{step}.pth")
        torch.save({
            "step": step,
            "model_state_dict": raw_state_dict,
            "optimizer_state_dict": optimizer.state_dict(),
        }, backup_path)
        print(f"Saved local historical checkpoint backup at {backup_path}")

def format_tokens(n):
    if n >= 1e9: return f"{n / 1e9:.2f}B"
    if n >= 1e6: return f"{n / 1e6:.2f}M"
    if n >= 1e3: return f"{n / 1e3:.1f}K"
    return str(n)

def train_step_3d(input_batch, target_batch, model, optimizer, pp_coord, pp_size, prev_rank, next_rank, tp_group, dp_group, device, batch_size, context_length, emb_dim, scaler=None):
    optimizer.zero_grad()
    
    if pp_size == 1:
        input_batch = input_batch.to(device)
        target_batch = target_batch.to(device)
        
        logits = model(input_batch)
        loss = torch.nn.functional.cross_entropy(logits.flatten(0, 1), target_batch.flatten())
        loss_val = loss.item()
        
        if scaler is not None:
            scaler.scale(loss).backward()
        else:
            loss.backward()
            
    else:
        if pp_coord == 0:
            input_batch = input_batch.to(device)
            x = model(input_batch)
            dist.send(x, dst=next_rank)
            
            grad_x_out = torch.empty(batch_size, context_length, emb_dim, device=device)
            dist.recv(grad_x_out, src=next_rank)
            
            if scaler is not None:
                scaler.scale(torch.tensor(0.0, device=device))
                
            torch.autograd.backward(tensors=[x], grad_tensors=[grad_x_out])
            loss_val = None
            
        elif pp_coord == pp_size - 1:
            x = torch.empty(batch_size, context_length, emb_dim, device=device)
            dist.recv(x, src=prev_rank)
            x.requires_grad_()
            
            logits = model(x)
            target_batch = target_batch.to(device)
            loss = torch.nn.functional.cross_entropy(logits.flatten(0, 1), target_batch.flatten())
            loss_val = loss.item()
            
            if scaler is not None:
                scaler.scale(loss).backward()
            else:
                loss.backward()
                
            dist.send(x.grad, dst=prev_rank)
            
        else:
            x = torch.empty(batch_size, context_length, emb_dim, device=device)
            dist.recv(x, src=prev_rank)
            x.requires_grad_()
            
            x_out = model(x)
            dist.send(x_out, dst=next_rank)
            
            grad_x_out = torch.empty(batch_size, context_length, emb_dim, device=device)
            dist.recv(grad_x_out, src=next_rank)
            
            if scaler is not None:
                scaler.scale(torch.tensor(0.0, device=device))
                
            torch.autograd.backward(tensors=[x_out], grad_tensors=[grad_x_out])
            dist.send(x.grad, dst=prev_rank)
            loss_val = None

    if dp_group is not None and dist.get_world_size(dp_group) > 1:
        for param in model.parameters():
            if param.grad is not None:
                dist.all_reduce(param.grad, op=dist.ReduceOp.SUM, group=dp_group)
                param.grad /= dist.get_world_size(dp_group)

    if scaler is not None:
        scaler.unscale_(optimizer)
        
        optimizer_state = scaler._per_optimizer_states[id(optimizer)]
        found_inf = torch.tensor(
            0.0 if len(optimizer_state["found_inf_per_device"]) == 0 else
            sum(v.item() for v in optimizer_state["found_inf_per_device"].values()),
            device=device
        )
        if dist.is_initialized():
            dist.all_reduce(found_inf, op=dist.ReduceOp.MAX)
            
        for k in optimizer_state["found_inf_per_device"].keys():
            optimizer_state["found_inf_per_device"][k].fill_(found_inf.item())

        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        scaler.step(optimizer)
        scaler.update()
    else:
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        
    return loss_val

def get_url_metadata(url):
    import urllib.request
    req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req) as resp:
            final_url = resp.geturl()
            content_length = int(resp.getheader('Content-Length'))
            return final_url, content_length
    except Exception as e:
        print(f"Error retrieving metadata for {url}: {e}")
        raise e

def consolidate_checkpoints(output_uri, tp_size, pp_size, cfg):
    print("Consolidating sharded checkpoints into single model.pth...")
    is_gcs = output_uri.startswith("gs://")
    bucket = None
    blob_path = ""
    
    if is_gcs:
        parsed = urlparse(output_uri)
        bucket_name = parsed.netloc
        blob_path = parsed.path.lstrip("/")
        if blob_path and not blob_path.endswith("/"):
            blob_path += "/"
        if storage is None:
            raise ImportError("google-cloud-storage package is required to consolidate checkpoints on GCS (gs://...) paths. Run 'pip install google-cloud-storage' first.")
        client = storage.Client()
        bucket = client.bucket(bucket_name)

    shards = {}
    for p in range(pp_size):
        shards[p] = {}
        for t in range(tp_size):
            fn = f"model_pp{p}_tp{t}.pth"
            if is_gcs:
                local_tmp_path = f"tmp_{fn}"
                blob = bucket.blob(os.path.join(blob_path, fn))
                blob.download_to_filename(local_tmp_path)
                shards[p][t] = torch.load(local_tmp_path, map_location="cpu")
                try:
                    os.remove(local_tmp_path)
                except OSError:
                    pass
            else:
                shards[p][t] = torch.load(os.path.join(output_uri, fn), map_location="cpu")
                
    stage_state_dicts = {}
    for p in range(pp_size):
        stage_sd = {}
        keys = list(shards[p][0].keys())
        for k in keys:
            is_col_parallel = any(suffix in k for suffix in [
                "W_query.weight", "W_query.bias",
                "W_key.weight", "W_key.bias",
                "W_value.weight", "W_value.bias",
                "fc1.weight", "fc1.bias"
            ])
            is_row_parallel = any(suffix in k for suffix in [
                "out_proj.weight", "fc2.weight"
            ])
            
            if is_col_parallel:
                stage_sd[k] = torch.cat([shards[p][t][k] for t in range(tp_size)], dim=0)
            elif is_row_parallel:
                stage_sd[k] = torch.cat([shards[p][t][k] for t in range(tp_size)], dim=1)
            else:
                stage_sd[k] = shards[p][0][k]
        stage_state_dicts[p] = stage_sd
        
    consolidated_sd = {}
    layers_per_stage = cfg["n_layers"] // pp_size
    for p in range(pp_size):
        for k, v in stage_state_dicts[p].items():
            if k.startswith("trf_blocks."):
                parts = k.split(".", 2)
                local_block_idx = int(parts[1])
                global_block_idx = p * layers_per_stage + local_block_idx
                new_key = f"trf_blocks.{global_block_idx}.{parts[2]}"
                consolidated_sd[new_key] = v
            else:
                consolidated_sd[k] = v
                
    local_consolidated_path = "model.pth" if is_gcs else os.path.join(output_uri, "model.pth")
    torch.save(consolidated_sd, local_consolidated_path)
    
    if is_gcs:
        dest_model_name = os.path.join(blob_path, "model.pth")
        print(f"Uploading consolidated model weights to gs://{bucket_name}/{dest_model_name}...")
        blob = bucket.blob(dest_model_name)
        blob.upload_from_filename(local_consolidated_path)
        try:
            os.remove(local_consolidated_path)
        except OSError:
            pass
            
    print("Checkpoint consolidation finished successfully!")

# ==============================================================================
# Main training loop
# ==============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--learning-rate", type=float, default=5e-4)
    parser.add_argument("--max-steps", type=int, default=100, help="Total training steps")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--weight-decay", type=float, default=0.1)
    parser.add_argument("--model-output-uri", type=str, default=".", help="Local path or GCS URI (gs://...) to save outputs")
    parser.add_argument("--log-freq", type=int, default=10, help="Steps frequency to log loss")
    parser.add_argument("--save-freq", type=int, default=0, help="Steps frequency to save checkpoints (0 to disable)")
    parser.add_argument("--dataset-subset", type=str, default="sample-10BT", help="FineWeb-Edu subset name")
    parser.add_argument("--restore-from", type=str, default=None, help="Local path or GCS URI to checkpoint.pth to restore training")
    parser.add_argument("--shuffle-buffer", type=int, default=2000, help="Shuffle buffer size for streaming")
    parser.add_argument("--wandb-api-key", type=str, default=None, help="Weights & Biases API Key")
    parser.add_argument("--num-workers", type=int, default=2, help="Number of dataloader worker processes")
    parser.add_argument("--dataset-bin", type=str, default=None, help="GCS URI or local path to pre-tokenized train.bin")
    parser.add_argument("--hf-dataset-repo", type=str, default="HuggingFaceFW/fineweb-edu", help="Hugging Face dataset repository fallback")
    parser.add_argument("--hf-dataset-file", type=str, default="sample/10BT/000_00000.bin", help="Hugging Face dataset file path fallback")
    parser.add_argument("--checkpoint-activations", type=str, default="False", help="Enable activation checkpointing (True/False)")
    parser.add_argument("--tp-size", type=int, default=1, help="Tensor Parallelism size")
    parser.add_argument("--pp-size", type=int, default=1, help="Pipeline Parallelism size")
    parser.add_argument("--serve", action="store_true", help="Serve the model after pretraining completes")
    args = parser.parse_args()
    
    args.checkpoint_activations = args.checkpoint_activations.lower() in ("true", "1", "yes")
    
    if args.wandb_api_key:
        os.environ["WANDB_API_KEY"] = args.wandb_api_key
        
    output_uri = args.model_output_uri

    torch.manual_seed(123)
    
    ddp = int(os.environ.get("RANK", -1)) != -1
    if ddp:
        ddp_rank = int(os.environ["RANK"])
        ddp_local_rank = int(os.environ["LOCAL_RANK"])
        ddp_world_size = int(os.environ["WORLD_SIZE"])
        
        if torch.cuda.is_available():
            device = f"cuda:{ddp_local_rank}"
            torch.cuda.set_device(device)
            backend = "nccl"
        else:
            device = "cpu"
            backend = "gloo"
            
        from torch.distributed import init_process_group
        init_process_group(backend=backend)
        
        tp_size = args.tp_size
        pp_size = args.pp_size
        assert ddp_world_size % (tp_size * pp_size) == 0, f"World size ({ddp_world_size}) must be divisible by TP ({tp_size}) * PP ({pp_size})"
        dp_size = ddp_world_size // (tp_size * pp_size)
        
        dp_coord = ddp_rank // (pp_size * tp_size)
        pp_coord = (ddp_rank // tp_size) % pp_size
        tp_coord = ddp_rank % tp_size
        
        tp_group = None
        for d in range(dp_size):
            for p in range(pp_size):
                ranks = [d * (pp_size * tp_size) + p * tp_size + t for t in range(tp_size)]
                group = dist.new_group(ranks)
                if ddp_rank in ranks:
                    tp_group = group
                    
        dp_group = None
        for p in range(pp_size):
            for t in range(tp_size):
                ranks = [d * (pp_size * tp_size) + p * tp_size + t for d in range(dp_size)]
                group = dist.new_group(ranks)
                if ddp_rank in ranks:
                    dp_group = group
                    
        prev_rank = None
        next_rank = None
        if pp_coord > 0:
            prev_rank = dp_coord * (pp_size * tp_size) + (pp_coord - 1) * tp_size + tp_coord
        if pp_coord < pp_size - 1:
            next_rank = dp_coord * (pp_size * tp_size) + (pp_coord + 1) * tp_size + tp_coord
            
        master_process = ddp_rank == 0
        loss_master = (pp_coord == pp_size - 1) and (tp_coord == 0) and (dp_coord == 0)
        
        print(f"[Rank {ddp_rank}] Coordinate: DP={dp_coord}, PP={pp_coord}, TP={tp_coord} | Prev Rank={prev_rank}, Next Rank={next_rank}")
    else:
        ddp_rank = 0
        ddp_local_rank = 0
        ddp_world_size = 1
        tp_size = 1
        pp_size = 1
        dp_size = 1
        dp_coord = 0
        pp_coord = 0
        tp_coord = 0
        tp_group = None
        dp_group = None
        prev_rank = None
        next_rank = None
        master_process = True
        loss_master = True
        device = torch.device(
            "cuda" if torch.cuda.is_available() else 
            "mps" if torch.backends.mps.is_available() else 
            "cpu"
        )
        
    if master_process:
        print(f"Using device: {device}")
        if ddp:
            print(f"3D Parallelism Grid Topology: DP_SIZE={dp_size}, PP_SIZE={pp_size}, TP_SIZE={tp_size}")
            print(f"Effective batch size: {args.batch_size * dp_size} (batch size per GPU: {args.batch_size}, total GPUs: {ddp_world_size})")

    is_cuda_device = (
        (hasinstance := isinstance(device, torch.device)) and device.type == "cuda"
    ) or (not hasinstance and "cuda" in str(device))

    if is_cuda_device:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

    if master_process:
        print("-" * 50)
        print("SYSTEM HARDWARE SPECIFICATIONS:")
        print(f"CPUs available: {os.cpu_count()}")
        if is_cuda_device:
            gpu_count = torch.cuda.device_count()
            gpu_name = torch.cuda.get_device_name(0)
            total_mem = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            print(f"GPUs available: {gpu_count} x {gpu_name} ({total_mem:.2f} GB VRAM)")
        print("-" * 50)

    if is_cuda_device:
        gpu_name = torch.cuda.get_device_name(0)
        hw_suffix = "T4" if "T4" in gpu_name else "L4" if "L4" in gpu_name else "A100" if "A100" in gpu_name else "GPU"
    else:
        hw_suffix = "CPU"

    if loss_master and os.environ.get("WANDB_API_KEY") and wandb is not None:
        print("Initializing Weights & Biases (wandb) logging...")
        run_name = f"gpt2-{hw_suffix}-3d-dp{dp_size}-pp{pp_size}-tp{tp_size}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        wandb.init(
            project="gpt2-pretraining",
            name=run_name,
            config={
                "learning_rate": args.learning_rate,
                "max_steps": args.max_steps,
                "batch_size": args.batch_size,
                "dp_size": dp_size,
                "pp_size": pp_size,
                "tp_size": tp_size,
                "effective_batch_size": args.batch_size * dp_size,
                "weight_decay": args.weight_decay,
                "dataset_subset": args.dataset_subset,
                "device": str(device)
            },
            notes="3D Parallel pretraining run"
        )

    model = GPTModel(
        GPT_CONFIG_124M, 
        checkpoint_activations=args.checkpoint_activations,
        pp_rank=pp_coord,
        pp_size=pp_size,
        tp_group=tp_group
    )
    model.to(device)
    use_fused = is_cuda_device
    optimizer = torch.optim.AdamW(
        model.parameters(), 
        lr=args.learning_rate, 
        weight_decay=args.weight_decay,
        fused=use_fused
    )
    
    start_step = 1
    
    if ddp and dp_group is not None and dist.get_world_size(dp_group) > 1:
        dp_master_rank = pp_coord * tp_size + tp_coord
        for param in model.parameters():
            dist.broadcast(param.data, src=dp_master_rank, group=dp_group)

    if is_cuda_device and hasattr(torch, "compile"):
        if loss_master:
            print("Compiling local model stage using torch.compile(mode='default')...")
        model = torch.compile(model, mode="default")
        
    scaler = torch.cuda.amp.GradScaler() if is_cuda_device else None

    num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[Rank {ddp_rank}] Local shard parameter size: {num_params:,}")
    
    full_params = 163000000
    flops_per_step = 6 * full_params * (args.batch_size * dp_size) * GPT_CONFIG_124M["context_length"]
    tflops_per_step = flops_per_step / 1e12

    dataset_bin_uri = args.dataset_bin
    use_streaming = False
    stream_url = None
    total_tokens = None
    
    if dataset_bin_uri and dataset_bin_uri.startswith("gs://"):
        parsed = urlparse(dataset_bin_uri)
        bucket_name = parsed.netloc
        blob_name = parsed.path.lstrip("/")
        
        local_bin_path = "/tmp/train.bin"
        os.makedirs(os.path.dirname(local_bin_path), exist_ok=True)
        
        if ddp:
            from torch.distributed import barrier
        local_master = (ddp_local_rank == 0) if ddp else True
        
        if local_master:
            if not os.path.exists(local_bin_path):
                print(f"Downloading pre-tokenized dataset from {dataset_bin_uri} to {local_bin_path}...")
                try:
                    if storage is None:
                        raise ImportError("google-cloud-storage library is not installed.")
                    client = storage.Client()
                    bucket = client.bucket(bucket_name)
                    blob = bucket.blob(blob_name)
                    blob.download_to_filename(local_bin_path)
                    print("Dataset download complete.")
                except Exception as e:
                    print(f"GCS download failed: {e}. Falling back to Hugging Face streaming fallback...")
            else:
                print(f"Using cached local dataset at {local_bin_path}.")
                
        if ddp:
            barrier()
            
        gcs_success = os.path.exists(local_bin_path)
        
        if ddp:
            gcs_success_tensor = torch.tensor(1.0 if gcs_success else 0.0, device=device)
            dist.all_reduce(gcs_success_tensor, op=dist.ReduceOp.MIN)
            gcs_success = gcs_success_tensor.item() > 0.5
            
        if not gcs_success:
            use_streaming = True
            hf_url = f"https://huggingface.co/datasets/{args.hf_dataset_repo}/resolve/main/{args.hf_dataset_file}"
            print(f"[Rank {ddp_rank}] GCS dataset unavailable. Initializing Hugging Face HTTP streaming resolve for: {hf_url}")
            
            try:
                stream_url, content_length = get_url_metadata(hf_url)
                total_tokens = content_length // 2
            except Exception as e:
                raise RuntimeError(f"CRITICAL: Failed to initialize Hugging Face stream: {e}")
    else:
        if dataset_bin_uri:
            local_bin_path = dataset_bin_uri
        else:
            use_streaming = True
            hf_url = f"https://huggingface.co/datasets/{args.hf_dataset_repo}/resolve/main/{args.hf_dataset_file}"
            print(f"[Rank {ddp_rank}] No GCS dataset provided. Initializing Hugging Face HTTP streaming resolve for: {hf_url}")
            try:
                stream_url, content_length = get_url_metadata(hf_url)
                total_tokens = content_length // 2
            except Exception as e:
                raise RuntimeError(f"CRITICAL: Failed to initialize Hugging Face stream: {e}")

    if use_streaming:
        dataset = GPTOfflineDataset(
            stream_url=stream_url,
            context_length=GPT_CONFIG_124M["context_length"],
            rank=dp_coord,
            world_size=dp_size,
            total_tokens=total_tokens
        )
    else:
        dataset = GPTOfflineDataset(
            bin_path=local_bin_path,
            context_length=GPT_CONFIG_124M["context_length"],
            rank=dp_coord,
            world_size=dp_size
        )
        
    pin_memory = is_cuda_device
    dataloader = DataLoader(dataset, batch_size=args.batch_size, num_workers=args.num_workers, pin_memory=pin_memory)
    data_iter = iter(dataloader)
    
    import numpy as np
    if use_streaming:
        total_epoch_tokens = total_tokens
    else:
        temp_data = np.memmap(local_bin_path, dtype=np.uint16, mode='r')
        total_epoch_tokens = len(temp_data)
        del temp_data

    total_run_tokens = args.max_steps * (args.batch_size * dp_size) * GPT_CONFIG_124M["context_length"]
    run_epoch_percent = (total_run_tokens / total_epoch_tokens) * 100
    
    if loss_master:
        print(f"Beginning training loop for {args.max_steps} steps (total tokens: {format_tokens(total_run_tokens)}, approx {run_epoch_percent:.5f}% of an epoch)...")
    
    model.train()
    start_time = time.time()
    last_log_time = start_time
    
    if is_cuda_device:
        autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.float16)
    else:
        autocast_ctx = torch.autocast(device_type="cpu", dtype=torch.bfloat16)

    for step in range(start_step, args.max_steps + 1):
        try:
            input_batch, target_batch = next(data_iter)
        except StopIteration:
            data_iter = iter(dataloader)
            input_batch, target_batch = next(data_iter)

        with autocast_ctx:
            loss_val = train_step_3d(
                input_batch=input_batch,
                target_batch=target_batch,
                model=model,
                optimizer=optimizer,
                pp_coord=pp_coord,
                pp_size=pp_size,
                prev_rank=prev_rank,
                next_rank=next_rank,
                tp_group=tp_group,
                dp_group=dp_group,
                device=device,
                batch_size=args.batch_size,
                context_length=GPT_CONFIG_124M["context_length"],
                emb_dim=GPT_CONFIG_124M["emb_dim"],
                scaler=scaler
            )

        if step % args.log_freq == 0:
            current_time = time.time()
            step_elapsed = current_time - last_log_time
            avg_step_time = step_elapsed / args.log_freq
            
            total_elapsed = current_time - start_time
            avg_time_per_step = total_elapsed / step
            steps_remaining = args.max_steps - step
            eta_seconds = int(steps_remaining * avg_time_per_step)
            
            if eta_seconds < 3600:
                eta_str = f"{eta_seconds // 60:02d}m {eta_seconds % 60:02d}s"
            else:
                eta_str = f"{eta_seconds // 3600:02d}h {(eta_seconds % 3600) // 60:02d}m"
                
            dur_seconds = int(total_elapsed)
            if dur_seconds < 3600:
                dur_str = f"{dur_seconds // 60:02d}m {dur_seconds % 60:02d}s"
            else:
                dur_str = f"{dur_seconds // 3600:02d}h {(dur_seconds % 3600) // 60:02d}m {dur_seconds % 60:02d}s"
                
            interval_tflops = tflops_per_step * args.log_freq
            tflops_per_sec = interval_tflops / step_elapsed
            
            tokens_trained = step * (args.batch_size * dp_size) * GPT_CONFIG_124M["context_length"]
            epoch_percent = (tokens_trained / total_epoch_tokens) * 100
            
            if loss_master and loss_val is not None:
                perplexity = math.exp(loss_val)
                tokens_str = format_tokens(tokens_trained)
                print(f"Step {step:06d}/{args.max_steps:06d} - Loss: {loss_val:.4f} | PPL: {perplexity:.2f} | Time/step: {avg_step_time:.3f}s | TFLOPS: {tflops_per_sec:.4f} | Tokens: {tokens_str} | Epoch: {epoch_percent:.5f}% | Duration: {dur_str} | ETA: {eta_str}")
                
                if wandb is not None and wandb.run is not None:
                    wandb.log({
                        "loss": loss_val,
                        "ppl": perplexity,
                        "tflops": tflops_per_sec,
                        "tokens": tokens_trained,
                        "epoch_percent": epoch_percent,
                        "time_per_step": avg_step_time
                     }, step=step)
                
            last_log_time = current_time

        if (args.save_freq > 0 and step % args.save_freq == 0) or step == args.max_steps:
            if dp_coord == 0:
                save_checkpoint(model, optimizer, output_uri, step, pp_coord=pp_coord, tp_coord=tp_coord)
            
            if ddp:
                dist.barrier()
                
            if loss_master and step == args.max_steps:
                consolidate_checkpoints(output_uri, tp_size, pp_size, GPT_CONFIG_124M)

    if ddp:
        from torch.distributed import destroy_process_group
        destroy_process_group()

    if args.serve:
        if ddp_rank == 0:
            print("[*] Training finished. Launching prediction serving server on Rank 0...")
            local_consolidated_path = "model.pth" if output_uri.startswith("gs://") else os.path.join(output_uri, "model.pth")
            if not os.path.exists(local_consolidated_path):
                raise FileNotFoundError(f"Cannot serve model: Consolidated checkpoint not found at {local_consolidated_path}")
            
            # Initialize model in standard single-GPU evaluation mode
            serve_model = GPTModel(GPT_CONFIG_124M)
            print(f"Loading state dict from {local_consolidated_path}...")
            serve_model.load_state_dict(torch.load(local_consolidated_path, map_location=device))
            serve_model.to(device)
            serve_model.eval()
            
            # Start native HTTP server on port 8080
            run_serving_server(8080, serve_model, device)
        else:
            print(f"[Rank {ddp_rank}] Entering idle standby standby loop while Rank 0 serves...")
            while True:
                time.sleep(3600)

if __name__ == "__main__":
    main()
