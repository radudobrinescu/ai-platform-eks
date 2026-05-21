# `recommend-instance-v2.py` Multi-Model Test Output

Generated: 2026-05-21 13:21:51 UTC

Tests the recommender against 6 different models with 6 representative argument combinations each.

## Models tested

- `bytedance-research/Lance`
- `deepseek-ai/DeepSeek-V4-Pro`
- `HuggingFaceBio/Carbon-8B`
- `Qwen/WebWorld-32B`
- `openai/gpt-oss-120b`
- `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`

## Scenarios per model

- **Default (just model)** — `(no extra flags)`
- **Chat fleet, 50 users** — `--workload chat --target-users 50`
- **Code fleet, 100 users (50 tok/s SLO)** — `--workload code --target-users 100`
- **INT4 batch fleet, 200 users** — `--quant int4 --workload batch --target-users 200`
- **Pin TP=4, single instance** — `--tp 4`
- **Summarization, 50 users (long context)** — `--workload summarization --target-users 50 --target-tok-s 10`

---

## `bytedance-research/Lance`

### Default (just model)

**Command:**
```bash
./ops/recommend-instance-v2.py bytedance-research/Lance 
```

**Output:**
```
error: bytedance-research/Lance config.json missing required transformer fields.
```

### Chat fleet, 50 users

**Command:**
```bash
./ops/recommend-instance-v2.py bytedance-research/Lance --workload chat --target-users 50
```

**Output:**
```
error: bytedance-research/Lance config.json missing required transformer fields.
```

### Code fleet, 100 users (50 tok/s SLO)

**Command:**
```bash
./ops/recommend-instance-v2.py bytedance-research/Lance --workload code --target-users 100
```

**Output:**
```
error: bytedance-research/Lance config.json missing required transformer fields.
```

### INT4 batch fleet, 200 users

**Command:**
```bash
./ops/recommend-instance-v2.py bytedance-research/Lance --quant int4 --workload batch --target-users 200
```

**Output:**
```
error: bytedance-research/Lance config.json missing required transformer fields.
```

### Pin TP=4, single instance

**Command:**
```bash
./ops/recommend-instance-v2.py bytedance-research/Lance --tp 4
```

**Output:**
```
error: bytedance-research/Lance config.json missing required transformer fields.
```

### Summarization, 50 users (long context)

**Command:**
```bash
./ops/recommend-instance-v2.py bytedance-research/Lance --workload summarization --target-users 50 --target-tok-s 10
```

**Output:**
```
error: bytedance-research/Lance config.json missing required transformer fields.
```

---

## `deepseek-ai/DeepSeek-V4-Pro`

### Default (just model)

**Command:**
```bash
./ops/recommend-instance-v2.py deepseek-ai/DeepSeek-V4-Pro 
```

**Output:**
```
✗ No catalog instance can host this configuration.
  Try: --quant int4, a shorter --seq, fewer --users, or lower --batch.
```

### Chat fleet, 50 users

**Command:**
```bash
./ops/recommend-instance-v2.py deepseek-ai/DeepSeek-V4-Pro --workload chat --target-users 50
```

**Output:**
```
✗ No catalog instance can host this configuration.
  Try: --quant int4, a shorter --seq, fewer --users, or lower --batch.
```

### Code fleet, 100 users (50 tok/s SLO)

**Command:**
```bash
./ops/recommend-instance-v2.py deepseek-ai/DeepSeek-V4-Pro --workload code --target-users 100
```

**Output:**
```
✗ No catalog instance can host this configuration.
  Try: --quant int4, a shorter --seq, fewer --users, or lower --batch.
```

### INT4 batch fleet, 200 users

**Command:**
```bash
./ops/recommend-instance-v2.py deepseek-ai/DeepSeek-V4-Pro --quant int4 --workload batch --target-users 200
```

**Output:**
```
Mode: fleet sizing for 200 users @ 5 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET (SLO-capped): 1× p5e.48xlarge — 8× NVIDIA H200 141GB per replica · TP=8
    $118.02/hr fleet  ·  ~$86,155/month  ($118.02/hr × 1 replicas)
    Capacity:    1 × 282 concurrent users = 282 total (SLO-capped)
    Throughput:  ~5 tok/s/user @ 282 concurrent per replica (SLO 5; ceiling 17 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 1× p5e.48xlarge runs the model with TP=8 on NVLink (aggregate 38.40 TB/s HBM). Each replica serves 282 concurrent users at ~5 tok/s/user; fleet of 1 replicas covers 200 users at $118.02/hr (~$86,155/month).
  Workload preset 'batch' applied: --avg-context 1024, --target-tok-s 5

Model:   deepseek-ai/DeepSeek-V4-Pro  (861.61B params, 61 layers, GQA 1KV/128Q)
Request: 8,192-token context · 200 concurrent · weights int4 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 200 target users, ≥5 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 1× p5e.48xlarge (H200 141GB (TP=8)) — max VRAM concurrency 4038, total GPUs 8

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → p5e.48xlarge    H200 141GB TP=8             282         1       5  $   118.02    $86,155  gpu-inference 
    p4de.24xlarge   A100 80GB TP=8              51         4       5  $   137.16   $100,130  gpu-inference 
    p5.48xlarge     H100 80GB TP=8             137         2       5  $   196.64   $143,547  gpu-inference 

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/deepseek-v4-pro.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: deepseek-v4-pro
  namespace: inference
spec:
  model: "deepseek-ai/DeepSeek-V4-Pro"
  gpuCount: 8
  tensorParallelSize: 8
  maxModelLen: 8192
  minVramPerGpuGiB: 77   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "406Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 423   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/deepseek-v4-pro.yaml
git commit -m "feat: deploy deepseek-v4-pro"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Pin TP=4, single instance

**Command:**
```bash
./ops/recommend-instance-v2.py deepseek-ai/DeepSeek-V4-Pro --tp 4
```

**Output:**
```
✗ No catalog instance can host this configuration.
  Try: --quant int4, a shorter --seq, fewer --users, or lower --batch.
```

### Summarization, 50 users (long context)

**Command:**
```bash
./ops/recommend-instance-v2.py deepseek-ai/DeepSeek-V4-Pro --workload summarization --target-users 50 --target-tok-s 10
```

**Output:**
```
✗ No catalog instance can host this configuration.
  Try: --quant int4, a shorter --seq, fewer --users, or lower --batch.
```

---

## `HuggingFaceBio/Carbon-8B`

### Default (just model)

**Command:**
```bash
./ops/recommend-instance-v2.py HuggingFaceBio/Carbon-8B 
```

**Output:**
```
Mode: cheapest fit (1 user)
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: g6.2xlarge — 1× NVIDIA L4, 24 GB VRAM per GPU · dedicated GPU
    $1.22/hr  ·  ~$892/month
    Utilisation: 19.1 / 24 GB (80%)  — 4.9 GB headroom
    Throughput:  ~12 tok/s single-stream (ceiling 12 tok/s, HBM 0.30 TB/s × 1)
═══════════════════════════════════════════════════════════════════════

  Why: g6.2xlarge runs the model with single-GPU on L4 (0.30 TB/s HBM). Single-stream throughput ~12 tok/s, 80% VRAM utilized.

Model:   HuggingFaceBio/Carbon-8B  (8.26B params, 32 layers, GQA 8KV/32Q)
Request: 8,192-token context · 1 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  g6.2xlarge      L4        1 GPU      19/24 GB         12    $1.22       $892  
  g5.2xlarge      A10G      1 GPU      19/24 GB         24    $1.52     $1,106  
  g6e.xlarge      L40S      1 GPU      19/48 GB         34    $2.33     $1,699  
  p4d.24xlarge    A100 40GB TP=8       3/40 GB         171   $27.43    $20,026  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/carbon-8b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: carbon-8b
  namespace: inference
spec:
  model: "HuggingFaceBio/Carbon-8B"
  gpuCount: 1
  maxModelLen: 8192
  minVramPerGpuGiB: 22   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "20Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 64   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/carbon-8b.yaml
git commit -m "feat: deploy carbon-8b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Production sizing:    --users N --target-tok-s X
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)
  • Workload-tuned defaults:  --workload chat | rag | code | summarization | batch

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Chat fleet, 50 users

**Command:**
```bash
./ops/recommend-instance-v2.py HuggingFaceBio/Carbon-8B --workload chat --target-users 50
```

**Output:**
```
Mode: fleet sizing for 50 users @ 25 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET (SLO-capped): 2× g6e.xlarge — 1× NVIDIA L40S per replica · dedicated GPU
    $4.65/hr fleet  ·  ~$3,397/month  ($2.33/hr × 2 replicas)
    Capacity:    2 × 44 concurrent users = 88 total (SLO-capped)
    Throughput:  ~25 tok/s/user @ 44 concurrent per replica (SLO 25; ceiling 34 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 2× g6e.xlarge runs the model with single-GPU on L40S (0.86 TB/s HBM). Each replica serves 44 concurrent users at ~25 tok/s/user; fleet of 2 replicas covers 50 users at $4.65/hr (~$3,397/month). Beats 2× g6e.2xlarge by 17% on total fleet cost.
  Workload preset 'chat' applied: --avg-context 1024, --target-tok-s 25

Model:   HuggingFaceBio/Carbon-8B  (8.26B params, 32 layers, GQA 8KV/32Q)
Request: 8,192-token context · 50 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 50 target users, ≥25 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 2× g6e.xlarge (L40S) — max VRAM concurrency 175, total GPUs 2

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → g6e.xlarge      L40S      1 GPU             44         2      25  $     4.65     $3,397  gpu-inference 
    g6e.12xlarge    L40S      TP=4              44         2      25  $    26.24    $19,155  gpu-inference 
    p4d.24xlarge    A100 40GB TP=8            1128         1      25  $    27.43    $20,026  gpu-inference 
    p4de.24xlarge   A100 80GB TP=8            1939         1      25  $    34.29    $25,033  gpu-inference 
    g6e.48xlarge    L40S      TP=8              63         1      25  $    37.68    $27,504  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --quant int4            $    735/mo (-78% on 1× g6.xlarge)  — 1-2% quality loss on benchmarks
  --target-tok-s 15       $  1,699/mo (-50% on 1× g6e.xlarge)  — lower per-user latency target

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/carbon-8b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: carbon-8b
  namespace: inference
spec:
  model: "HuggingFaceBio/Carbon-8B"
  gpuCount: 1
  maxModelLen: 8192
  minVramPerGpuGiB: 22   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "20Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 66   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 2
  maxReplicas: 3
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/carbon-8b.yaml
git commit -m "feat: deploy carbon-8b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Code fleet, 100 users (50 tok/s SLO)

**Command:**
```bash
./ops/recommend-instance-v2.py HuggingFaceBio/Carbon-8B --workload code --target-users 100
```

**Output:**
```
Mode: fleet sizing for 100 users @ 50 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET (SLO-capped): 1× p4d.24xlarge — 8× NVIDIA A100 40GB per replica · TP=8
    $27.43/hr fleet  ·  ~$20,026/month  ($27.43/hr × 1 replicas)
    Capacity:    1 × 282 concurrent users = 282 total (SLO-capped)
    Throughput:  ~50 tok/s/user @ 282 concurrent per replica (SLO 50; ceiling 171 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 1× p4d.24xlarge runs the model with TP=8 on NVLink (aggregate 12.44 TB/s HBM). Each replica serves 282 concurrent users at ~50 tok/s/user; fleet of 1 replicas covers 100 users at $27.43/hr (~$20,026/month). Beats 1× p4de.24xlarge by 20% on total fleet cost.
  Workload preset 'code' applied: --avg-context 2048, --target-tok-s 50

Model:   HuggingFaceBio/Carbon-8B  (8.26B params, 32 layers, GQA 8KV/32Q)
Request: 8,192-token context · 100 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 100 target users, ≥50 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 1× p4d.24xlarge (A100 40GB (TP=8)) — max VRAM concurrency 939, total GPUs 8

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → p4d.24xlarge    A100 40GB TP=8             282         1      50  $    27.43    $20,026  gpu-inference 
    p4de.24xlarge   A100 80GB TP=8             484         1      50  $    34.29    $25,033  gpu-inference 
    p5.48xlarge     H100 80GB TP=8            1308         1      50  $    98.32    $71,774  gpu-inference 
    p5e.48xlarge    H200 141GB TP=8            2427         1      53  $   118.02    $86,155  gpu-inference 
  ⚠ g6.2xlarge      L4        1 GPU              1       100      12  $   122.25    $89,242  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --quant int4            $  2,755/mo (-86% on 3× g5.xlarge)  — 1-2% quality loss on benchmarks
  --target-tok-s 30       $  6,795/mo (-66% on 4× g6e.xlarge)  — lower per-user latency target

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/carbon-8b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: carbon-8b
  namespace: inference
spec:
  model: "HuggingFaceBio/Carbon-8B"
  gpuCount: 1
  maxModelLen: 8192
  minVramPerGpuGiB: 22   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "20Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 423   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/carbon-8b.yaml
git commit -m "feat: deploy carbon-8b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### INT4 batch fleet, 200 users

**Command:**
```bash
./ops/recommend-instance-v2.py HuggingFaceBio/Carbon-8B --quant int4 --workload batch --target-users 200
```

**Output:**
```
Mode: fleet sizing for 200 users @ 5 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET: 3× g6.xlarge — 1× NVIDIA L4 per replica · dedicated GPU
    $3.02/hr fleet  ·  ~$2,204/month  ($1.01/hr × 3 replicas)
    Capacity:    3 × 81 concurrent users = 243 total
    Throughput:  ~22 tok/s/user @ 81 concurrent per replica (SLO 5; ceiling 40 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 3× g6.xlarge runs the model with single-GPU on L4 (0.30 TB/s HBM). Each replica serves 81 concurrent users at ~22 tok/s/user; fleet of 3 replicas covers 200 users at $3.02/hr (~$2,204/month). Beats 5× g4dn.xlarge by 8% on total fleet cost.
  Workload preset 'batch' applied: --avg-context 1024, --target-tok-s 5

Model:   HuggingFaceBio/Carbon-8B  (8.26B params, 32 layers, GQA 8KV/32Q)
Request: 8,192-token context · 200 concurrent · weights int4 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 200 target users, ≥5 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 3× g6.xlarge (L4) — max VRAM concurrency 117, total GPUs 3

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → g6.xlarge       L4        1 GPU             81         3      22  $     3.02     $2,204  gpu-inference 
    g4dn.xlarge     T4        1 GPU             46         5      15  $     3.29     $2,402  gpu-inference 
    g5.xlarge       A10G      1 GPU             81         3      44  $     3.77     $2,755  gpu-inference 
    g6e.xlarge      L40S      1 GPU            186         2      41  $     4.65     $3,397  gpu-inference 
    g4dn.12xlarge   T4        TP=4             256         1       7  $     4.89     $3,570  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --avg-context 256       $    735/mo (-67% on 1× g6.xlarge)  — for short Q&A workloads

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/carbon-8b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: carbon-8b
  namespace: inference
spec:
  model: "HuggingFaceBio/Carbon-8B"
  gpuCount: 1
  maxModelLen: 8192
  minVramPerGpuGiB: 7   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "8Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 122   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 3
  maxReplicas: 5
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/carbon-8b.yaml
git commit -m "feat: deploy carbon-8b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Pin TP=4, single instance

**Command:**
```bash
./ops/recommend-instance-v2.py HuggingFaceBio/Carbon-8B --tp 4
```

**Output:**
```
Mode: cheapest fit for 1 users, TP=4 pinned
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: g6.12xlarge — 4× NVIDIA L4, 24 GB VRAM per GPU · TP=4
    $5.75/hr  ·  ~$4,201/month
    Utilisation: 5.3 / 24 GB (22%)  — 18.7 GB headroom
    Throughput:  ~12 tok/s single-stream (ceiling 12 tok/s, HBM 0.30 TB/s × 4)
═══════════════════════════════════════════════════════════════════════

  Why: g6.12xlarge runs the model with TP=4 on PCIe (aggregate 1.20 TB/s HBM). Single-stream throughput ~12 tok/s, 22% VRAM utilized.

Model:   HuggingFaceBio/Carbon-8B  (8.26B params, 32 layers, GQA 8KV/32Q)
Request: 8,192-token context · 1 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  g6.12xlarge     L4        TP=4       5/24 GB          12    $5.75     $4,201  
  g5.12xlarge     A10G      TP=4       5/24 GB          24    $7.09     $5,178  
  g6e.12xlarge    L40S      TP=4       5/48 GB          34   $13.12     $9,578  
  p4d.24xlarge    A100 40GB TP=4 × PP=2 3/40 GB         104   $27.43    $20,026  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/carbon-8b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: carbon-8b
  namespace: inference
spec:
  model: "HuggingFaceBio/Carbon-8B"
  gpuCount: 4
  tensorParallelSize: 4
  maxModelLen: 8192
  minVramPerGpuGiB: 7   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "20Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 64   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/carbon-8b.yaml
git commit -m "feat: deploy carbon-8b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Production sizing:    --users N --target-tok-s X
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)
  • Workload-tuned defaults:  --workload chat | rag | code | summarization | batch

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Summarization, 50 users (long context)

**Command:**
```bash
./ops/recommend-instance-v2.py HuggingFaceBio/Carbon-8B --workload summarization --target-users 50 --target-tok-s 10
```

**Output:**
```
Mode: fleet sizing for 50 users @ 10 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET: 4× g6e.xlarge — 1× NVIDIA L40S per replica · dedicated GPU
    $9.31/hr fleet  ·  ~$6,795/month  ($2.33/hr × 4 replicas)
    Capacity:    4 × 14 concurrent users = 56 total
    Throughput:  ~34 tok/s/user @ 14 concurrent per replica (SLO 10; ceiling 34 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 4× g6e.xlarge runs the model with single-GPU on L40S (0.86 TB/s HBM). Each replica serves 14 concurrent users at ~34 tok/s/user; fleet of 4 replicas covers 50 users at $9.31/hr (~$6,795/month). Beats 4× g6e.2xlarge by 17% on total fleet cost.
  Workload preset 'summarization' applied: --avg-context 8192

Model:   HuggingFaceBio/Carbon-8B  (8.26B params, 32 layers, GQA 8KV/32Q)
Request: 8,192-token context · 50 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 50 target users, ≥10 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 4× g6e.xlarge (L40S) — max VRAM concurrency 21, total GPUs 4

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → g6e.xlarge      L40S      1 GPU             14         4      34  $     9.31     $6,795  gpu-inference 
    g6.12xlarge     L4        TP=4              33         2      10  $    11.51     $8,401  gpu-inference 
    g6e.12xlarge    L40S      TP=4              93         1      17  $    13.12     $9,578  gpu-inference 
    g5.12xlarge     A10G      TP=4              41         2      18  $    14.19    $10,356  gpu-inference 
    g5.48xlarge     A10G      TP=8              93         1      14  $    20.37    $14,869  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --quant int4            $  4,408/mo (-35% on 6× g6.xlarge)  — 1-2% quality loss on benchmarks
  --avg-context 256       $  1,106/mo (-84% on 1× g5.2xlarge)  — for short Q&A workloads

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/carbon-8b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: carbon-8b
  namespace: inference
spec:
  model: "HuggingFaceBio/Carbon-8B"
  gpuCount: 1
  maxModelLen: 8192
  minVramPerGpuGiB: 22   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "20Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 64   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 4
  maxReplicas: 6
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/carbon-8b.yaml
git commit -m "feat: deploy carbon-8b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

---

## `Qwen/WebWorld-32B`

### Default (just model)

**Command:**
```bash
./ops/recommend-instance-v2.py Qwen/WebWorld-32B 
```

**Output:**
```
Mode: cheapest fit (1 user)
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: g6.12xlarge — 4× NVIDIA L4, 24 GB VRAM per GPU · TP=4
    $5.75/hr  ·  ~$4,201/month
    Utilisation: 20.0 / 24 GB (83%)  — 4.0 GB headroom
    Throughput:  ~3 tok/s single-stream (ceiling 3 tok/s, HBM 0.30 TB/s × 4)
═══════════════════════════════════════════════════════════════════════

  Why: g6.12xlarge runs the model with TP=4 on PCIe (aggregate 1.20 TB/s HBM). Single-stream throughput ~3 tok/s, 83% VRAM utilized. ⚠ 83% VRAM used — limited headroom for longer contexts or higher concurrency

Model:   Qwen/WebWorld-32B  (32.76B params, 64 layers, GQA 8KV/64Q)
Request: 8,192-token context · 1 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  g6.12xlarge     L4        TP=4       20/24 GB          3    $5.75     $4,201  
  g5.12xlarge     A10G      TP=4       20/24 GB          6    $7.09     $5,178  
  g6e.12xlarge    L40S      TP=4       20/48 GB          9   $13.12     $9,578  
  p4d.24xlarge    A100 40GB TP=8       10/40 GB         43   $27.43    $20,026  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/webworld-32b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: webworld-32b
  namespace: inference
spec:
  model: "Qwen/WebWorld-32B"
  gpuCount: 4
  tensorParallelSize: 4
  maxModelLen: 8192
  minVramPerGpuGiB: 24   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "66Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 64   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/webworld-32b.yaml
git commit -m "feat: deploy webworld-32b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Production sizing:    --users N --target-tok-s X
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)
  • Workload-tuned defaults:  --workload chat | rag | code | summarization | batch

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Chat fleet, 50 users

**Command:**
```bash
./ops/recommend-instance-v2.py Qwen/WebWorld-32B --workload chat --target-users 50
```

**Output:**
```
Mode: fleet sizing for 50 users @ 25 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET (SLO-capped): 1× p4d.24xlarge — 8× NVIDIA A100 40GB per replica · TP=8
    $27.43/hr fleet  ·  ~$20,026/month  ($27.43/hr × 1 replicas)
    Capacity:    1 × 71 concurrent users = 71 total (SLO-capped)
    Throughput:  ~25 tok/s/user @ 71 concurrent per replica (SLO 25; ceiling 43 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 1× p4d.24xlarge runs the model with TP=8 on NVLink (aggregate 12.44 TB/s HBM). Each replica serves 71 concurrent users at ~25 tok/s/user; fleet of 1 replicas covers 50 users at $27.43/hr (~$20,026/month). Beats 1× p4de.24xlarge by 20% on total fleet cost.
  Workload preset 'chat' applied: --avg-context 1024, --target-tok-s 25

Model:   Qwen/WebWorld-32B  (32.76B params, 64 layers, GQA 8KV/64Q)
Request: 8,192-token context · 50 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 50 target users, ≥25 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 1× p4d.24xlarge (A100 40GB (TP=8)) — max VRAM concurrency 756, total GPUs 8

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → p4d.24xlarge    A100 40GB TP=8              71         1      25  $    27.43    $20,026  gpu-inference 
    p4de.24xlarge   A100 80GB TP=8             123         1      25  $    34.29    $25,033  gpu-inference 
    p5.48xlarge     H100 80GB TP=8             332         1      25  $    98.32    $71,774  gpu-inference 
    p5e.48xlarge    H200 141GB TP=8             682         1      25  $   118.02    $86,155  gpu-inference 
  ⚠ g6.12xlarge     L4        TP=4               1        50       3  $   287.71   $210,032  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --quant int4            $  3,397/mo (-83% on 2× g6e.xlarge)  — 1-2% quality loss on benchmarks

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/webworld-32b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: webworld-32b
  namespace: inference
spec:
  model: "Qwen/WebWorld-32B"
  gpuCount: 4
  tensorParallelSize: 4
  maxModelLen: 8192
  minVramPerGpuGiB: 24   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "66Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 107   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/webworld-32b.yaml
git commit -m "feat: deploy webworld-32b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Code fleet, 100 users (50 tok/s SLO)

**Command:**
```bash
./ops/recommend-instance-v2.py Qwen/WebWorld-32B --workload code --target-users 100
```

**Output:**
```
Mode: fleet sizing for 100 users @ 50 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET (SLO-capped): 1× p5e.48xlarge — 8× NVIDIA H200 141GB per replica · TP=8
    $118.02/hr fleet  ·  ~$86,155/month  ($118.02/hr × 1 replicas)
    Capacity:    1 × 170 concurrent users = 170 total (SLO-capped)
    Throughput:  ~50 tok/s/user @ 170 concurrent per replica (SLO 50; ceiling 133 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 1× p5e.48xlarge runs the model with TP=8 on NVLink (aggregate 38.40 TB/s HBM). Each replica serves 170 concurrent users at ~50 tok/s/user; fleet of 1 replicas covers 100 users at $118.02/hr (~$86,155/month).
  Workload preset 'code' applied: --avg-context 2048, --target-tok-s 50

Model:   Qwen/WebWorld-32B  (32.76B params, 64 layers, GQA 8KV/64Q)
Request: 8,192-token context · 100 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 100 target users, ≥50 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 1× p5e.48xlarge (H200 141GB (TP=8)) — max VRAM concurrency 1642, total GPUs 8

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → p5e.48xlarge    H200 141GB TP=8             170         1      50  $   118.02    $86,155  gpu-inference 
    p4de.24xlarge   A100 80GB TP=8              30         4      51  $   137.16   $100,130  gpu-inference 
    p5.48xlarge     H100 80GB TP=8              83         2      50  $   196.64   $143,547  gpu-inference 
  ⚠ g6.12xlarge     L4        TP=4               1       100       3  $   575.43   $420,063  gpu-inference 
  ⚠ g5.12xlarge     A10G      TP=4               1       100       6  $   709.28   $517,776  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --quant int4            $ 20,026/mo (-77% on 1× p4d.24xlarge)  — 1-2% quality loss on benchmarks
  --target-tok-s 30       $ 50,065/mo (-42% on 2× p4de.24xlarge)  — lower per-user latency target

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/webworld-32b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: webworld-32b
  namespace: inference
spec:
  model: "Qwen/WebWorld-32B"
  gpuCount: 4
  tensorParallelSize: 4
  maxModelLen: 8192
  minVramPerGpuGiB: 24   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "66Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 255   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/webworld-32b.yaml
git commit -m "feat: deploy webworld-32b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### INT4 batch fleet, 200 users

**Command:**
```bash
./ops/recommend-instance-v2.py Qwen/WebWorld-32B --quant int4 --workload batch --target-users 200
```

**Output:**
```
Mode: fleet sizing for 200 users @ 5 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET: 4× g6e.xlarge — 1× NVIDIA L40S per replica · dedicated GPU
    $9.31/hr fleet  ·  ~$6,795/month  ($2.33/hr × 4 replicas)
    Capacity:    4 × 60 concurrent users = 240 total
    Throughput:  ~18 tok/s/user @ 60 concurrent per replica (SLO 5; ceiling 29 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 4× g6e.xlarge runs the model with single-GPU on L40S (0.86 TB/s HBM). Each replica serves 60 concurrent users at ~18 tok/s/user; fleet of 4 replicas covers 200 users at $9.31/hr (~$6,795/month). Beats 4× g6e.2xlarge by 17% on total fleet cost.
  Workload preset 'batch' applied: --avg-context 1024, --target-tok-s 5

Model:   Qwen/WebWorld-32B  (32.76B params, 64 layers, GQA 8KV/64Q)
Request: 8,192-token context · 200 concurrent · weights int4 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 200 target users, ≥5 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 4× g6e.xlarge (L40S) — max VRAM concurrency 87, total GPUs 4

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → g6e.xlarge      L40S      1 GPU             60         4      18  $     9.31     $6,795  gpu-inference 
    g6e.12xlarge    L40S      TP=4             376         1       7  $    13.12     $9,578  gpu-inference 
    g5.12xlarge     A10G      TP=4             166         2       8  $    14.19    $10,356  gpu-inference 
    g6.12xlarge     L4        TP=4              97         3       5  $    17.26    $12,602  gpu-inference 
    g5.48xlarge     A10G      TP=8             376         1       6  $    20.37    $14,869  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --avg-context 256       $  1,699/mo (-75% on 1× g6e.xlarge)  — for short Q&A workloads

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/webworld-32b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: webworld-32b
  namespace: inference
spec:
  model: "Qwen/WebWorld-32B"
  gpuCount: 1
  maxModelLen: 8192
  minVramPerGpuGiB: 24   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "20Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 90   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 4
  maxReplicas: 6
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/webworld-32b.yaml
git commit -m "feat: deploy webworld-32b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Pin TP=4, single instance

**Command:**
```bash
./ops/recommend-instance-v2.py Qwen/WebWorld-32B --tp 4
```

**Output:**
```
Mode: cheapest fit for 1 users, TP=4 pinned
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: g6.12xlarge — 4× NVIDIA L4, 24 GB VRAM per GPU · TP=4
    $5.75/hr  ·  ~$4,201/month
    Utilisation: 20.0 / 24 GB (83%)  — 4.0 GB headroom
    Throughput:  ~3 tok/s single-stream (ceiling 3 tok/s, HBM 0.30 TB/s × 4)
═══════════════════════════════════════════════════════════════════════

  Why: g6.12xlarge runs the model with TP=4 on PCIe (aggregate 1.20 TB/s HBM). Single-stream throughput ~3 tok/s, 83% VRAM utilized. ⚠ 83% VRAM used — limited headroom for longer contexts or higher concurrency

Model:   Qwen/WebWorld-32B  (32.76B params, 64 layers, GQA 8KV/64Q)
Request: 8,192-token context · 1 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  g6.12xlarge     L4        TP=4       20/24 GB          3    $5.75     $4,201  
  g5.12xlarge     A10G      TP=4       20/24 GB          6    $7.09     $5,178  
  g6e.12xlarge    L40S      TP=4       20/48 GB          9   $13.12     $9,578  
  p4d.24xlarge    A100 40GB TP=4 × PP=2 10/40 GB         26   $27.43    $20,026  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/webworld-32b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: webworld-32b
  namespace: inference
spec:
  model: "Qwen/WebWorld-32B"
  gpuCount: 4
  tensorParallelSize: 4
  maxModelLen: 8192
  minVramPerGpuGiB: 24   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "66Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 64   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/webworld-32b.yaml
git commit -m "feat: deploy webworld-32b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Production sizing:    --users N --target-tok-s X
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)
  • Workload-tuned defaults:  --workload chat | rag | code | summarization | batch

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Summarization, 50 users (long context)

**Command:**
```bash
./ops/recommend-instance-v2.py Qwen/WebWorld-32B --workload summarization --target-users 50 --target-tok-s 10
```

**Output:**
```
Mode: fleet sizing for 50 users @ 10 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET: 1× p4d.24xlarge — 8× NVIDIA A100 40GB per replica · TP=8
    $27.43/hr fleet  ·  ~$20,026/month  ($27.43/hr × 1 replicas)
    Capacity:    1 × 65 concurrent users = 65 total
    Throughput:  ~26 tok/s/user @ 65 concurrent per replica (SLO 10; ceiling 43 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 1× p4d.24xlarge runs the model with TP=8 on NVLink (aggregate 12.44 TB/s HBM). Each replica serves 65 concurrent users at ~26 tok/s/user; fleet of 1 replicas covers 50 users at $27.43/hr (~$20,026/month). Beats 1× p4de.24xlarge by 20% on total fleet cost.
  Workload preset 'summarization' applied: --avg-context 8192

Model:   Qwen/WebWorld-32B  (32.76B params, 64 layers, GQA 8KV/64Q)
Request: 8,192-token context · 50 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 50 target users, ≥10 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 1× p4d.24xlarge (A100 40GB (TP=8)) — max VRAM concurrency 94, total GPUs 8

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → p4d.24xlarge    A100 40GB TP=8              65         1      26  $    27.43    $20,026  gpu-inference 
    p4de.24xlarge   A100 80GB TP=8             153         1      22  $    34.29    $25,033  gpu-inference 
    g6e.48xlarge    L40S      TP=8              25         2      10  $    75.35    $55,007  gpu-inference 
    p5.48xlarge     H100 80GB TP=8             153         1      37  $    98.32    $71,774  gpu-inference 
    p5e.48xlarge    H200 141GB TP=8             287         1      39  $   118.02    $86,155  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --quant int4            $ 12,602/mo (-37% on 3× g6.12xlarge)  — 1-2% quality loss on benchmarks
  --target-tok-s 6        $ 19,155/mo (-4% on 2× g6e.12xlarge)  — lower per-user latency target

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/webworld-32b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: webworld-32b
  namespace: inference
spec:
  model: "Qwen/WebWorld-32B"
  gpuCount: 4
  tensorParallelSize: 4
  maxModelLen: 8192
  minVramPerGpuGiB: 24   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "66Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 98   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/webworld-32b.yaml
git commit -m "feat: deploy webworld-32b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

---

## `openai/gpt-oss-120b`

### Default (just model)

**Command:**
```bash
./ops/recommend-instance-v2.py openai/gpt-oss-120b 
```

**Output:**
```
Mode: cheapest fit (1 user)
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: p4de.24xlarge — 8× NVIDIA A100 80GB, 80 GB VRAM per GPU · TP=8
    $34.29/hr  ·  ~$25,033/month
    Utilisation: 37.2 / 80 GB (46%)  — 42.8 GB headroom
    Throughput:  ~87 tok/s single-stream (ceiling 87 tok/s, HBM 2.04 TB/s × 8)
═══════════════════════════════════════════════════════════════════════

  Why: p4de.24xlarge runs the model with TP=8 on NVLink (aggregate 16.31 TB/s HBM). Single-stream throughput ~87 tok/s, 46% VRAM utilized. Beats g6e.48xlarge by 453% on throughput.

  ⚠ MoE: 128 experts, 4 active/token — all 120.41B reside in VRAM; bandwidth ≈ 3.76B active.

Model:   openai/gpt-oss-120b  (120.41B params, 36 layers, GQA 8KV/64Q)
Request: 8,192-token context · 1 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  p4de.24xlarge   A100 80GB TP=8       37/80 GB         87   $34.29    $25,033  
  p5.48xlarge     H100 80GB TP=8       37/80 GB        143   $98.32    $71,774  
  p5e.48xlarge    H200 141GB TP=8       37/141 GB       205  $118.02    $86,155  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/gpt-oss-120b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gpt-oss-120b
  namespace: inference
spec:
  model: "openai/gpt-oss-120b"
  gpuCount: 8
  tensorParallelSize: 8
  maxModelLen: 8192
  minVramPerGpuGiB: 43   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "229Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 64   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/gpt-oss-120b.yaml
git commit -m "feat: deploy gpt-oss-120b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Production sizing:    --users N --target-tok-s X
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)
  • Workload-tuned defaults:  --workload chat | rag | code | summarization | batch

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Chat fleet, 50 users

**Command:**
```bash
./ops/recommend-instance-v2.py openai/gpt-oss-120b --workload chat --target-users 50
```

**Output:**
```
Mode: single instance for 50 users, 25 tok/s SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: p4de.24xlarge — 8× NVIDIA A100 80GB, 80 GB VRAM per GPU · TP=8
    $34.29/hr  ·  ~$25,033/month
    Utilisation: 37.2 / 80 GB (46%)  — 42.8 GB headroom
    Throughput:  ~87 tok/s/user @ 50 concurrent (ceiling 87 tok/s, HBM 2.04 TB/s × 8)
═══════════════════════════════════════════════════════════════════════

  Why: p4de.24xlarge runs the model with TP=8 on NVLink (aggregate 16.31 TB/s HBM). At 50 concurrent users, each user gets ~87 tok/s (ceiling 87 tok/s single-stream). Beats g6e.48xlarge by 453% on throughput.
  Workload preset 'chat' applied: --avg-context 1024, --target-tok-s 25

  ⚠ MoE: 128 experts, 4 active/token — all 120.41B reside in VRAM; bandwidth ≈ 3.76B active.

Model:   openai/gpt-oss-120b  (120.41B params, 36 layers, GQA 8KV/64Q)
Request: 8,192-token context · 50 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  p4de.24xlarge   A100 80GB TP=8       37/80 GB         87   $34.29    $25,033  
  p5.48xlarge     H100 80GB TP=8       37/80 GB        143   $98.32    $71,774  
  p5e.48xlarge    H200 141GB TP=8       37/141 GB       205  $118.02    $86,155  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/gpt-oss-120b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gpt-oss-120b
  namespace: inference
spec:
  model: "openai/gpt-oss-120b"
  gpuCount: 8
  tensorParallelSize: 8
  maxModelLen: 8192
  minVramPerGpuGiB: 43   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "229Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 100   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/gpt-oss-120b.yaml
git commit -m "feat: deploy gpt-oss-120b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Code fleet, 100 users (50 tok/s SLO)

**Command:**
```bash
./ops/recommend-instance-v2.py openai/gpt-oss-120b --workload code --target-users 100
```

**Output:**
```
Mode: fleet sizing for 100 users @ 50 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET (SLO-capped): 2× p4de.24xlarge — 8× NVIDIA A100 80GB per replica · TP=8
    $68.58/hr fleet  ·  ~$50,065/month  ($34.29/hr × 2 replicas)
    Capacity:    2 × 73 concurrent users = 146 total (SLO-capped)
    Throughput:  ~50 tok/s/user @ 73 concurrent per replica (SLO 50; ceiling 87 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 2× p4de.24xlarge runs the model with TP=8 on NVLink (aggregate 16.31 TB/s HBM). Each replica serves 73 concurrent users at ~50 tok/s/user; fleet of 2 replicas covers 100 users at $68.58/hr (~$50,065/month). Beats 1× p5.48xlarge by 30% on total fleet cost.
  Workload preset 'code' applied: --avg-context 2048, --target-tok-s 50

  ⚠ MoE: 128 experts, 4 active/token — all 120.41B reside in VRAM; bandwidth ≈ 3.76B active.

Model:   openai/gpt-oss-120b  (120.41B params, 36 layers, GQA 8KV/64Q)
Request: 8,192-token context · 100 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 100 target users, ≥50 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 2× p4de.24xlarge (A100 80GB (TP=8)) — max VRAM concurrency 1965, total GPUs 16

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → p4de.24xlarge   A100 80GB TP=8              73         2      50  $    68.58    $50,065  gpu-inference 
    p5.48xlarge     H100 80GB TP=8             197         1      50  $    98.32    $71,774  gpu-inference 
    p5e.48xlarge    H200 141GB TP=8             405         1      50  $   118.02    $86,155  gpu-inference 
  ⚠ g6e.48xlarge    L40S      TP=8               1       100      16  $  3767.61  $2,750,357  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --quant int4            $ 20,026/mo (-60% on 1× p4d.24xlarge)  — 1-2% quality loss on benchmarks
  --target-tok-s 30       $ 25,033/mo (-50% on 1× p4de.24xlarge)  — lower per-user latency target

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/gpt-oss-120b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gpt-oss-120b
  namespace: inference
spec:
  model: "openai/gpt-oss-120b"
  gpuCount: 8
  tensorParallelSize: 8
  maxModelLen: 8192
  minVramPerGpuGiB: 43   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "229Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 110   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 2
  maxReplicas: 3
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/gpt-oss-120b.yaml
git commit -m "feat: deploy gpt-oss-120b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### INT4 batch fleet, 200 users

**Command:**
```bash
./ops/recommend-instance-v2.py openai/gpt-oss-120b --quant int4 --workload batch --target-users 200
```

**Output:**
```
Mode: fleet sizing for 200 users @ 5 tok/s/user SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED FLEET: 2× g6.12xlarge — 4× NVIDIA L4 per replica · TP=4
    $11.51/hr fleet  ·  ~$8,401/month  ($5.75/hr × 2 replicas)
    Capacity:    2 × 187 concurrent users = 374 total
    Throughput:  ~6 tok/s/user @ 187 concurrent per replica (SLO 5; ceiling 16 tok/s)
═══════════════════════════════════════════════════════════════════════

  Why: 2× g6.12xlarge runs the model with TP=4 on PCIe (aggregate 1.20 TB/s HBM). Each replica serves 187 concurrent users at ~6 tok/s/user; fleet of 2 replicas covers 200 users at $11.51/hr (~$8,401/month). Beats 1× g6e.12xlarge by 12% on total fleet cost.
  Workload preset 'batch' applied: --avg-context 1024, --target-tok-s 5

  ⚠ MoE: 128 experts, 4 active/token — all 120.41B reside in VRAM; bandwidth ≈ 3.76B active.

Model:   openai/gpt-oss-120b  (120.41B params, 36 layers, GQA 8KV/64Q)
Request: 8,192-token context · 200 concurrent · weights int4 · kv fp16  (prices: cache (eu-central-1))

═══════════════════════════════════════════════════════════════════════
  FLEET ALTERNATIVES — 200 target users, ≥5 tok/s/user SLO, 70% util headroom
═══════════════════════════════════════════════════════════════════════

  Selected: 2× g6.12xlarge (L4 (TP=4)) — max VRAM concurrency 268, total GPUs 8

  Fleet alternatives (sorted by total fleet cost; ⚠ = SLO unmet; duplicates by GPU+strategy hidden)

    INSTANCE        GPU       STRATEGY   CONC/INST  REPLICAS   TOK/S  FLEET $/HR   FLEET/MO  NODEPOOL      
    --------------- --------- ---------- ---------  --------  ------  ----------  ---------  --------------
  → g6.12xlarge     L4        TP=4             187         2       6  $    11.51     $8,401  gpu-inference 
    g6e.12xlarge    L40S      TP=4             935         1       7  $    13.12     $9,578  gpu-inference 
    g5.12xlarge     A10G      TP=4             187         2      11  $    14.19    $10,356  gpu-inference 
    g6.48xlarge     L4        TP=8             333         1       5  $    16.69    $12,187  gpu-inference 
    g5.48xlarge     A10G      TP=8             935         1       6  $    20.37    $14,869  gpu-inference 

Cost levers (impact on monthly fleet cost)
  --avg-context 256       $  4,201/mo (-50% on 1× g6.12xlarge)  — for short Q&A workloads

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/gpt-oss-120b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gpt-oss-120b
  namespace: inference
spec:
  model: "openai/gpt-oss-120b"
  gpuCount: 4
  tensorParallelSize: 4
  maxModelLen: 8192
  minVramPerGpuGiB: 21   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "61Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 281   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 2
  maxReplicas: 3
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/gpt-oss-120b.yaml
git commit -m "feat: deploy gpt-oss-120b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Pin TP=4, single instance

**Command:**
```bash
./ops/recommend-instance-v2.py openai/gpt-oss-120b --tp 4
```

**Output:**
```
Mode: cheapest fit for 1 users, TP=4 pinned
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: p4d.24xlarge — 8× NVIDIA A100 40GB, 40 GB VRAM per GPU · TP=4 × PP=2
    $27.43/hr  ·  ~$20,026/month
    Utilisation: 35.6 / 40 GB (89%)  — 4.4 GB headroom
    Throughput:  ~40 tok/s single-stream (ceiling 40 tok/s, HBM 1.55 TB/s × 8)
═══════════════════════════════════════════════════════════════════════

  Why: p4d.24xlarge runs the model with TP=4 × PP=2 on 8 GPUs (NVLink). Single-stream throughput ~40 tok/s, 89% VRAM utilized. ⚠ 89% VRAM used — limited headroom for longer contexts or higher concurrency

  ⚠ MoE: 128 experts, 4 active/token — all 120.41B reside in VRAM; bandwidth ≈ 3.76B active.

Model:   openai/gpt-oss-120b  (120.41B params, 36 layers, GQA 8KV/64Q)
Request: 8,192-token context · 1 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  p4d.24xlarge    A100 40GB TP=4 × PP=2 36/40 GB         40   $27.43    $20,026  
  p4de.24xlarge   A100 80GB TP=4 × PP=2 36/80 GB         53   $34.29    $25,033  
  p5.48xlarge     H100 80GB TP=4 × PP=2 36/80 GB         87   $98.32    $71,774  
  p5e.48xlarge    H200 141GB TP=4 × PP=2 36/141 GB       125  $118.02    $86,155  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/gpt-oss-120b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gpt-oss-120b
  namespace: inference
spec:
  model: "openai/gpt-oss-120b"
  gpuCount: 8
  tensorParallelSize: 4
  pipelineParallelSize: 2
  maxModelLen: 8192
  minVramPerGpuGiB: 41   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "229Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 64   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/gpt-oss-120b.yaml
git commit -m "feat: deploy gpt-oss-120b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Production sizing:    --users N --target-tok-s X
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)
  • Workload-tuned defaults:  --workload chat | rag | code | summarization | batch

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

### Summarization, 50 users (long context)

**Command:**
```bash
./ops/recommend-instance-v2.py openai/gpt-oss-120b --workload summarization --target-users 50 --target-tok-s 10
```

**Output:**
```
Mode: single instance for 50 users, 10 tok/s SLO
═══════════════════════════════════════════════════════════════════════
  ✓ RECOMMENDED: p4de.24xlarge — 8× NVIDIA A100 80GB, 80 GB VRAM per GPU · TP=8
    $34.29/hr  ·  ~$25,033/month
    Utilisation: 37.2 / 80 GB (46%)  — 42.8 GB headroom
    Throughput:  ~87 tok/s/user @ 50 concurrent (ceiling 87 tok/s, HBM 2.04 TB/s × 8)
═══════════════════════════════════════════════════════════════════════

  Why: p4de.24xlarge runs the model with TP=8 on NVLink (aggregate 16.31 TB/s HBM). At 50 concurrent users, each user gets ~87 tok/s (ceiling 87 tok/s single-stream). Beats g6e.48xlarge by 453% on throughput.
  Workload preset 'summarization' applied: --avg-context 8192

  ⚠ MoE: 128 experts, 4 active/token — all 120.41B reside in VRAM; bandwidth ≈ 3.76B active.

Model:   openai/gpt-oss-120b  (120.41B params, 36 layers, GQA 8KV/64Q)
Request: 8,192-token context · 50 concurrent · weights bf16 · kv fp16  (prices: cache (eu-central-1))

Alternatives (--verbose for full table)

  INSTANCE        GPU       STRATEGY   VRAM          TOK/S     $/HR    MONTHLY  NOTE
  --------------- --------- ---------- ------------ ------  -------  ---------  ----------
  p4de.24xlarge   A100 80GB TP=8       37/80 GB         87   $34.29    $25,033  
  p5.48xlarge     H100 80GB TP=8       37/80 GB        143   $98.32    $71,774  
  p5e.48xlarge    H200 141GB TP=8       37/141 GB       205  $118.02    $86,155  

Deploy this model — copy-paste the block below into your shell:

cat > workloads/models/gpt-oss-120b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gpt-oss-120b
  namespace: inference
spec:
  model: "openai/gpt-oss-120b"
  gpuCount: 8
  tensorParallelSize: 8
  maxModelLen: 8192
  minVramPerGpuGiB: 43   # min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies
  workerMemory: "229Gi"   # CPU memory for the Ray worker pod (default is conservative; this is sized to model)
  # maxNumSeqs: 100   # vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)
  minReplicas: 1
  maxReplicas: 2
EOF

Then commit and push (ArgoCD picks it up within ~30s):

git add workloads/models/gpt-oss-120b.yaml
git commit -m "feat: deploy gpt-oss-120b"
git push

# Watch the deployment come up:
kubectl get inferenceendpoints -n inference -w

Next steps:
  • Cut cost ~50% on this model:  --quant int4   (may lose 1-2% on benchmarks)

Throughput estimates ±25% — validate with `vllm bench serve`. Monthly = hourly × 730h. Prices: cache (eu-central-1)
```

---

## `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`

### Default (just model)

**Command:**
```bash
./ops/recommend-instance-v2.py unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF 
```

**Output:**
```
error: unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF exists on HuggingFace but has no config.json (typical for GGUF/ONNX/quant-only repos). The recommender needs the original transformer config — point to the source HF model instead of the converted artifact.
```

### Chat fleet, 50 users

**Command:**
```bash
./ops/recommend-instance-v2.py unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF --workload chat --target-users 50
```

**Output:**
```
error: unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF exists on HuggingFace but has no config.json (typical for GGUF/ONNX/quant-only repos). The recommender needs the original transformer config — point to the source HF model instead of the converted artifact.
```

### Code fleet, 100 users (50 tok/s SLO)

**Command:**
```bash
./ops/recommend-instance-v2.py unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF --workload code --target-users 100
```

**Output:**
```
error: unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF exists on HuggingFace but has no config.json (typical for GGUF/ONNX/quant-only repos). The recommender needs the original transformer config — point to the source HF model instead of the converted artifact.
```

### INT4 batch fleet, 200 users

**Command:**
```bash
./ops/recommend-instance-v2.py unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF --quant int4 --workload batch --target-users 200
```

**Output:**
```
error: unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF exists on HuggingFace but has no config.json (typical for GGUF/ONNX/quant-only repos). The recommender needs the original transformer config — point to the source HF model instead of the converted artifact.
```

### Pin TP=4, single instance

**Command:**
```bash
./ops/recommend-instance-v2.py unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF --tp 4
```

**Output:**
```
error: unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF exists on HuggingFace but has no config.json (typical for GGUF/ONNX/quant-only repos). The recommender needs the original transformer config — point to the source HF model instead of the converted artifact.
```

### Summarization, 50 users (long context)

**Command:**
```bash
./ops/recommend-instance-v2.py unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF --workload summarization --target-users 50 --target-tok-s 10
```

**Output:**
```
error: unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF exists on HuggingFace but has no config.json (typical for GGUF/ONNX/quant-only repos). The recommender needs the original transformer config — point to the source HF model instead of the converted artifact.
```

---
