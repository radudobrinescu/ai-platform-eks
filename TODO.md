# AI Inference Platform — Prioritized Improvements

## P0 — Required for demo/adoption

- [x] **Wire ArgoCD to Git repo** — Push gitops repo to GitHub, register in ArgoCD, apply Applications. Makes the entire platform truly GitOps-managed.

- [ ] **EBS snapshot image cache** (`25.image-cache` Terraform module) — Pre-cache the Ray LLM image (~15GB) in an EBS snapshot using Bottlerocket. Switch GPU NodeClass to Bottlerocket + snapshot. Cuts model cold start from ~15 min to ~7 min. Re-run only when Ray image version changes.

## P1 — Required for production

- [ ] **LiteLLM model cleanup CronJob** — Periodically reconcile LiteLLM model entries with live RayServices. Delete orphaned models via `DELETE /model/delete` when their InferenceEndpoint no longer exists. Prevents stale entries after workload deletion.

- [ ] **Authentication & multi-tenancy** — Enable OpenWebUI auth, scope LiteLLM API keys per team, add namespace-per-team with RBAC and resource quotas. Prevent unauthorized access and runaway GPU usage.

- [ ] **Security hardening** — Add `securityContext` (runAsNonRoot, drop capabilities) to all containers. Enforce Pod Security Standards on namespaces. Add network policies for LiteLLM/OpenWebUI. Restrict EKS public endpoint to specific CIDRs.

- [ ] **Inference observability** — vLLM/Ray metrics (TTFT, tokens/sec, queue depth, GPU utilization) exported to Prometheus. Grafana dashboards for model performance. Alerts on latency spikes and GPU OOM.

- [ ] **Scope KRO RBAC** — Replace ClusterAdmin with a custom ClusterRole granting access only to the specific API groups KRO needs (ray.io, apps, core). Least-privilege.

## P2 — Production hardening

- [ ] **ECR pull-through cache** — Mirror `anyscale/ray-llm` and `vllm/vllm-openai` to ECR. Faster pulls, no Docker Hub rate limits, image scanning.

- [ ] **S3 model cache + private model support** — Download model weights to S3 once, load from S3 instead of HuggingFace at runtime. Faster startup (same-region S3 vs internet), no HF token dependency, version-controlled. Use ACK to create the S3 bucket. vLLM supports `s3://bucket/model-path` natively — requires IRSA/Pod Identity for S3 access. Combined with EBS image cache, cold start drops to ~3-4 min. Also enables proprietary/fine-tuned models that can't be on HuggingFace.

- [ ] **Cost controls** — Resource quotas per namespace, Karpenter GPU limits with budget alerts, auto-shutdown idle models after configurable timeout, spot instance support for dev/test workloads.

- [ ] **Model lifecycle** — Canary rollouts (deploy new model version alongside old, shift traffic gradually), health checks beyond "pod is running" (validate model output quality), rollback on failure.

- [ ] **Single NAT gateway option** — VPC module creates 3 NAT gateways by default ($100+/month). Add a flag for single NAT gateway for dev/test environments.

## P3 — Scale & differentiation

- [ ] **DRA (Dynamic Resource Allocation)** — Right-size GPU nodes precisely. Enable MIG for running multiple small models on one GPU. Significant cost savings at scale.

- [ ] **Additional KRO templates** — ChatAssistant (model + OpenWebUI config), RAG pipeline (model + vector DB + embeddings), Agentic stack (model + tools + orchestrator), Batch inference (Ray Job).

- [ ] **Multi-cluster / multi-region** — ArgoCD hub-spoke for deploying across clusters. Active-passive failover for inference endpoints.

- [ ] **Ingress with TLS** — Replace port-forwards with ALB Ingress + cert-manager + domain. Required for sharing the platform with teams beyond the operator's laptop.

- [ ] **Pre-warm GPU node pool** — Optional `minInstances: 1` on GPU NodePool for teams that need sub-minute model startup. Configurable per environment.
