# Demo: the llm-d router remembers what each GPU already read

**The story.** A support assistant serves eight products. Every chat starts
with that product's manual as the system prompt, about 6,000 tokens. On top of
the manual each customer has a growing conversation. Before a GPU writes the
first word of an answer, it must read the whole prompt (prefill). vLLM keeps
what it has read in its KV cache, so a prompt it has seen before is nearly
free to read again, but only on the GPU that read it.

The demo runs the same four GPUs with the same traffic twice:

- **Kubernetes Service (round robin).** Each request lands on a random GPU.
  Every GPU ends up reading every manual, its cache cannot hold them all, and
  it keeps re-reading 6,000-token manuals.
- **llm-d router.** The router (Endpoint Picker) knows which GPU holds which
  prompt prefix and sends the request there. Each manual and each
  conversation stays on the GPU that already has it in cache.

**What the audience sees.** Prism, llm-d's benchmark viewer, shows the two
runs side by side as the request rate goes up. Look at three curves:
- time to first token, which is how long a user waits before the answer
  starts;
- output tokens per second;
- the prefix-cache hit rate.

## What it runs

| Piece | What |
|---|---|
| Model | `Qwen/Qwen3-8B` on 4 replicas, one NVIDIA L4 each (g6.2xlarge), an `LLMDEndpoint` with `routingProfile: prefix` |
| Load | [inference-perf](https://github.com/kubernetes-sigs/inference-perf) v0.7.0 (the harness llm-d-benchmark uses), llm-d-benchmark's `shared_prefix_multi_turn_chat` workload: 8 system prompts x 6 conversations, 150-token questions and answers, 1, 2, 3, 4 requests/s for 60 s each |
| Results | Benchmark Report v0.2 files on a 5 GiB volume, with vLLM's prefix-cache hit rate per stage from the platform's autoscaling Prometheus |
| Viewer | [Prism](https://github.com/llm-d/llm-d-prism) v1.0.0 in the cluster, reached with `kubectl port-forward` |

Everything is Apache-2.0. The benchmark takes about 15 minutes for both runs.

**Cost.** 4 x g6.2xlarge is about $4 an hour on demand (eu-central-1). An hour
covers the model start (about 10 minutes), one benchmark, and showing the
results. Remove the model afterwards (last step).

**Security.** Prism has no login. It gets a ClusterIP Service and a
NetworkPolicy that lets no other pod in, and you open it through `kubectl
port-forward`, which listens on 127.0.0.1 only. Do not add an Ingress for it.

## Before the demo (about 15 minutes)

1. Deploy the model. Commit this file to `workloads/scale-models/` on the
   branch Argo CD syncs and push. It is the same kind of file that
   `./platformctl new-model --tier llm-d` writes, pinned to 4 replicas so the
   demo compares routing, not autoscaling.

   ```yaml
   apiVersion: kro.run/v1alpha1
   kind: LLMDEndpoint
   metadata:
     name: support-assistant
     namespace: inference
   spec:
     model: "Qwen/Qwen3-8B"
     gpuCount: 1
     maxModelLen: 16384
     minVramPerGpuGiB: 22        # L4 class or larger; Karpenter picks the cheapest (g6)
     workerCpu: "6"              # fits a g6.2xlarge (8 vCPU, 32 GiB)
     workerMemory: "24Gi"
     minReplicas: 4
     maxReplicas: 4              # fixed size for the comparison
     routingProfile: prefix      # weight the router's prefix-cache scorer highest
     extraArgs: ["--enable-prompt-tokens-details"]   # cached tokens in every API response
   ```

2. Wait until it is ready, with 4 replicas:

   ```bash
   kubectl get llmdendpoint support-assistant -n inference -w
   ```

3. Optional but recommended: run the benchmark once before the audience
   arrives (next section). Prism keeps every run, so the charts are ready even
   if the live run is cut short.

## The demo

1. **Run both benchmarks** (about 15 minutes; talk through the story while it
   runs):

   ```bash
   ./ops/demos/prefix_cache_bench.py run --model support-assistant
   ```

   The script:
   - creates the namespace `demo-bench`, the results volume, and a Service
     `support-assistant-direct` that reaches the same vLLM pods without the
     router;
   - runs inference-perf against that Service, then against the router. Both
     runs have the same workload shape and fresh random prompts, so neither
     benefits from the other's cache;
   - prints a side-by-side table and starts Prism.

   Add `--restart` to restart the vLLM pods between the two runs. That makes
   both runs start with empty caches, and adds a model reload of a few
   minutes.

2. **Open Prism:**

   ```bash
   kubectl port-forward -n demo-bench svc/prism 8080:8080
   open http://localhost:8080
   ```

   In the dashboard, pick the two runs, "llm-d router (prefix-cache aware)"
   and "Kubernetes Service (round robin)". Plot time to first token (p50 and
   p99) and output tokens per second against the request rate, and show the
   prefix-cache hit rate.

3. **Talking points:**
   - **Same GPUs, same model, same traffic.** The only difference is who
     picks the GPU.
   - **Hit rate is the cause.** With round robin, the hit rate stays low
     because every GPU has to read every manual. With the router, the hit rate
     stays high, because each manual and each conversation is read once and
     then reused.
   - **Time to first token is what users feel.** Round robin's waits grow
     quickly with load as the GPUs spend their time re-reading manuals. The
     router's stay short.
   - **Throughput is cost.** Serving the same traffic with fewer re-reads
     means fewer GPUs for the same service level.
   - **Nothing to configure in the application.** Clients call the same
     LiteLLM `/v1` endpoint. The routing happens in the platform.

4. **Optional, one request at a time.** The model reports cached tokens per
   request (`--enable-prompt-tokens-details`). Send the same long system
   prompt twice through LiteLLM and show
   `usage.prompt_tokens_details.cached_tokens` going from 0 to nearly the
   whole prompt.

## What to expect, and how to tune it

The numbers depend on the GPU, the model and the load, so measure them before
you show them. A rough estimate for the settings above:
- After the weights, an L4 has about 3.5 GB for KV cache. For Qwen3-8B that is
  roughly 25,000 tokens, or a few manuals.
- Round robin sends all eight manuals to every GPU, so each GPU keeps
  evicting manuals and reading them again.
- Through the router, each manual stays on a GPU that already has it.

The gap should show in the hit rate first. Time to first token follows as the
request rate rises. For a bigger effect, raise `--groups` (more manuals) or
`--prefix-len`; lower them for a smaller one. The longest conversation has to
stay under `maxModelLen` (16,384): the script's defaults reach about 10,000
tokens.

## Afterwards

```bash
./ops/demos/prefix_cache_bench.py cleanup --model support-assistant   # namespace demo-bench, results, the direct Service
git rm workloads/scale-models/support-assistant.yaml && git commit -m "demo: remove support-assistant" && git push
```

Karpenter removes the four GPU nodes about five minutes after the model is
gone.
