# Demo: a model that costs nothing while nobody uses it

**The story.** A team has a model that is busy during the day and idle at
night or between projects. A GPU running idle still costs its full hourly
price. With scale to zero the model stops after a quiet period and its GPU
node goes away. The next request starts it again: the user waits a few
minutes once, and after that answers are fast until the model goes quiet
again.

**What the audience sees.**
1. A model listed in Open WebUI with no GPU running behind it.
2. A question that brings up a GPU node and the model, then gets answered.
3. The model going back to zero, and the GPU node being removed.

## How it works, in one paragraph

The model is an `LLMDEndpoint` with `minReplicas: 0`.
- **Going to zero.** KEDA watches the requests that the llm-d router is
  serving or holding. After `scaleToZeroAfterSeconds` with none, it stops the
  last replica, and Karpenter removes the empty GPU node.
- **Waking up.** A request that arrives while no replica runs waits in the
  llm-d router, for up to `coldStartTimeoutSeconds`. The waiting request
  tells KEDA to start a replica. Karpenter brings up a GPU node, which pulls
  the vLLM image. The model loads, and the router sends the request on.
- **Clients.** Callers use the same LiteLLM `/v1` endpoint as for any other
  model.

## Before the demo (about 10 minutes)

1. Deploy a small model with scale to zero:

   ```bash
   ./platformctl new-model Qwen/Qwen2.5-3B-Instruct --tier llm-d --min-replicas 0 --deploy
   ```

2. For a demo, shorten the idle time. In
   `workloads/scale-models/qwen2-5-3b-instruct.yaml`, set
   `scaleToZeroAfterSeconds: 120`, then commit and push. The default is 600
   (10 minutes).

3. The model starts one replica when it is created and is not stopped before
   the idle time has passed. With the short idle time of this demo it may be
   stopped before it has finished loading; it then loads on its first request
   (step 4). Check:

   ```bash
   kubectl get llmdendpoint qwen2-5-3b-instruct -n inference   # READY True
   kubectl get deploy qwen2-5-3b-instruct-llmd -n inference    # 0/0 once idle
   kubectl get nodes -l workload-type=gpu-inference            # gone ~5 min later
   ```

   The cluster dashboard shows the model with `⇅ 0/N` replicas.

## The demo

1. **Show the idle state.** Open the cluster dashboard: the model has 0
   replicas and there is no GPU node. In Open WebUI the model is still in the
   list.

2. **Wake it.** In Open WebUI pick the model and ask anything. In a terminal,
   watch it come up:

   ```bash
   kubectl get nodeclaims -w                      # Karpenter launches a GPU node
   kubectl get pods -n inference -l llm-d.ai/pool=qwen2-5-3b-instruct -w
   ```

   The answer arrives once the model is ready. Ask again: this answer is
   immediate.

3. **Let it go back to zero.** Stop sending requests. After the idle time,
   the replica stops (`kubectl get deploy` shows 0/0). About five minutes
   later Karpenter removes the GPU node, and the dashboard's cost for the
   model drops to zero.

4. **Optional: under load it scales out.** Lower `targetInflight` to 2 for
   the demo, then send parallel requests (any load tool against LiteLLM).
   KEDA adds replicas while more requests are in flight than the target
   allows per replica, up to `maxReplicas`.

**Talking points:**
- **Cost follows use.** An idle model holds no GPU. The platform's own
  components keep running on small CPU nodes.
- **No error while it starts.** The request waits instead of failing, so a
  user sees a slow first answer rather than an error.
- **Opt-in per model.** Models default to `minReplicas: 1`, always warm. Teams
  choose scale to zero where a slow first answer is acceptable, for example
  internal tools, batch jobs, or models used a few times a day.
- **Bounded wait.** If no GPU can be had, the router gives up after
  `coldStartTimeoutSeconds` (default 10 minutes) and LiteLLM retries once.
  The request then fails with an error instead of hanging.

## How long the first answer takes

The wait is the sum of these steps:
- KEDA notices the request: up to about 30 s.
- Karpenter gets a GPU node and it joins the cluster: about 1-2 minutes.
- The GPU driver and device plugin start.
- The node pulls the vLLM image (about 10 GB).
- The weights download (from the S3 model cache when it is pre-seeded,
  otherwise from Hugging Face).
- vLLM starts.

The wait depends on the model: the larger the weights, the longer vLLM takes to
load them. Measure your model once before the demo, and pick a small one if the
wait matters.

## Limits to mention if asked

- **Held requests live in router memory.** If the router pod restarts while
  requests are waiting, those requests are lost. The callers' retries still
  wake the model.
- **The first start is not waited for.** The idle time runs from the moment the
  model is created, not from the moment it is ready. A model that needs longer
  than `scaleToZeroAfterSeconds` to load, and gets no request meanwhile, is
  stopped before it has ever served and loads for the first time on its first
  request. `READY True` with `scaledToZero: true` therefore means "idle at
  zero", not "has loaded successfully": send one request after deploying.
- **Load balancer timeout.** The internal load balancer keeps a silent
  connection open for 1020 s, which covers the longest hold (900 s) plus the
  answer. A longer `coldStartTimeoutSeconds` is not possible.
- **Public edge timeout.** API clients that call through the public edge
  (CloudFront) are cut off when the origin sends nothing within the edge's
  timeout, 30 s by default. The model still wakes up, so a retry a few minutes
  later succeeds. In-cluster callers wait the full time. Open WebUI calls
  LiteLLM inside the cluster without a timeout. Still, try the whole browser
  path once before the demo.
- **Disaggregated models.** `LLMDDisaggEndpoint` (separate prefill and decode
  pools) does not scale to zero.
