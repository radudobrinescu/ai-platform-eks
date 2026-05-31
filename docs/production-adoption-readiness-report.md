# Production & Adoption Readiness Report — AI Platform on EKS

**Audience target assessed:** small businesses adopting this as a turnkey platform — auto-deployed and easy to operate (platform team), easy to use (developer / data-science teams).
**Date:** 2026-05-31
**Method:** deep multi-agent audit — 14 subsystems mapped at mechanism level, 200 unique findings, 124 material findings adversarially verified against the code (121 confirmed, 3 refuted), plus independent manual verification of the highest-risk components.

---

## Verdict

The platform is an **excellent demo and a strong architectural skeleton**, but it is **not production-ready and not adoption-ready for an unsupervised small-business team today.** The design *intent* around security and operability is consistently good (KMS at rest, IMDSv2, IRSA separation, a human-in-the-loop remediator, Langfuse-on-first-boot, scale-to-zero GPUs). But the **boundaries that actually matter — perimeter auth, the LLM remediator's blast radius, tenant isolation, data durability — are built on assumptions a non-specialist team cannot uphold,** and several fail catastrophically.

| Dimension | Readiness (1–5) | One-line |
|---|---|---|
| **Security** | **2** | Public plain-HTTP perimeter, no app-auth on a cluster-mutating UI, bypassable tenant isolation, shared DB superuser handed to an LLM. |
| **Resilience** | **2** | Single shared Postgres + single-replica everything, **no backups anywhere**, ArgoCD selfHeal fights manual recovery. |
| **Operator ease-of-use** | **2** | Turnkey to *stand up*, hand-babysat forever: no alerts, no upgrade/backup runbooks, scattered manual prereqs. |
| **Consumer ease-of-use** | **2** | Day-one path is broken; no input validation; a developer can't read their own key or reach the API without operator help. |
| **Simplification** | **2** | Grew rather than designed-lean: smeared sources of truth, 4× re-implemented Job/runtime-download pattern, dead code. |
| **Cost** | **2** | ~$280–350/mo idle before a single token; advertised cost controls are bypassable. |

**Bottom line:** roughly **6–10 weeks of hardening** stands between "great demo" and "a small business can safely run this." The good news: most fixes are cheap and the architecture doesn't need to change — they're defaults, auth, backups, and consolidation, not rewrites.

---

## 1. CRITICAL — fix before anyone runs this outside a sandbox (3)

These are independently re-verified against the code.

### C1. Public perimeter is plain HTTP with no TLS
`platform/config/ingress.yaml:24-28,49-53,73-78` + `cluster-dashboard/manifests.yaml:233-239`
All four operator surfaces — LiteLLM (admin **master key** as a bearer token on every call), Open WebUI (login + the master key as its upstream key), Langfuse (all prompt/response traces), Cluster Dashboard (**mutates the cluster**) — are published on one internet-facing ALB over **HTTP**, no `certificate-arn`, no `ssl-redirect`. Anyone on-path captures the god credential and every prompt.
**Fix:** terminate TLS at the ALB (ACM cert + HTTPS listeners + ssl-redirect). If no domain ships out of the box, default ingresses to `scheme: internal` and keep the SSM tunnel as the access path until a cert is supplied.

### C2 / C3. The cluster-mutating approval UI has no authentication
`backend.py:666-705` (literally: *"we rely on the ALB allowlist for 'auth'"*, approver = spoofable `X-Forwarded-For`), `manifests.yaml:233-239` (one hardcoded `/32`, HTTP).
`POST /investigations/<id>/approve` spawns a Job that runs an LLM with **write** access (delete pods; patch/scale Deployments/StatefulSets; patch ConfigMaps/HPAs) across `inference` + every `team-*` namespace. The only gate is a single IP allowlist on a public, unencrypted LB; the audit identity is forgeable; `DELETE /investigations` can wipe the audit trail. Anyone reaching the endpoint (shared office IP, VPN exit, compromised CI, in-cluster pod) can drive cluster writes.
**Fix:** real auth on approve/dismiss/delete (ALB OIDC or oauth2-proxy; at minimum a server-validated bearer token), bind approver to the authenticated principal, HTTPS only, treat the IP allowlist as one layer not *the* boundary.

> The README markets "autonomous incident response"; today the autonomy is gated only by an unauthenticated button on a public plain-HTTP page. This is the single highest-risk item.

---

## 2. HIGH — required before a paid/production launch (32; selected)

**Security**
- **Tenant isolation is bypassable by design** (`team-onboarding.yaml:96-99` + `capabilities.tf:434` + `inference-endpoint.yaml:109`): team egress is allowed to the whole `ai-platform`-labeled set; the `inference` namespace carries that label; vLLM services are registered `api_key:"no-key"`. **Any team pod can POST directly to any model's vLLM**, bypassing LiteLLM auth, budgets, rate-limits, and tracing. Defeats the entire multi-tenant cost-control story.
- **One shared master key is the only gateway auth and is handed to 5 components** including end-user-facing Open WebUI (`litellm.yaml:31-35`, `open-webui.yaml:27-31`, `litellm-cleanup.yaml`, `litellm-sync`). Compromise of any one = full gateway admin.
- **Shared Postgres *superuser* (`platform`) credential is injected into the LLM Investigator/Remediator pods** (`capabilities.tf:484-487`, `event_watcher.py:_agent_env`) — pods that run `kiro-cli --trust-all-tools` on context built from **untrusted pod logs** (classic prompt-injection → DB read of all LiteLLM keys + Langfuse data). The dashboard also uses this superuser creds (`manifests.yaml:142-144`).
- **Prompt-injection-to-cluster-write pipeline**: untrusted logs/events → LLM `--trust-all-tools` → `fix_commands` re-executed with `--allow-write`. RBAC is the only real boundary and it's cross-tenant (writer SA bound into *every* `team-*` ns, no per-incident scoping — `rbac.yaml:101-127`).
- **Remediator downloads `kubectl` + `kiro-cli` via `curl|bash`/unpinned `pip` at runtime, no checksum** (`backend.py:202-212`, `event_watcher.py:284`) — supply-chain RCE into a pod holding the writer token + DB superuser; also breaks in air-gapped nets.
- **EKS API server is public to `0.0.0.0/0`** in every shipped tfvars (`main.tf:21`, no `endpoint_public_access_cidrs`).
- **IAM personas are cosmetic**: all four roles share one trust policy (`20.iam-roles/main.tf:12-28`) defaulting to the **deployer's own ARN** (`main.tf:8`) — anyone who can assume View can assume ClusterAdmin; no break-glass.
- **GitOps tracks the moving `main` branch** with prune+selfHeal (`variables.tf:52-56`, `workloads.yaml:44`) — any push auto-applies to the live cluster, no review gate, no prod/dev revision separation.
- **Real Identity Center instance ARN committed to git history** (`gitops-test.tfvars`, reachable from `origin/main`); personal `radu-test.tfvars`/`cnd-demo.tfvars` carry real identifiers.
- **Open WebUI signup not locked** (`open-webui.yaml:32-33`): `WEBUI_AUTH=true` but no `ENABLE_SIGNUP=false`/`DEFAULT_USER_ROLE`/`WEBUI_SECRET_KEY` — first visitor to the public URL can claim admin (→ master key + paid Bedrock); sessions reset on every restart.
- **Bedrock policy is `Resource="*"`** (`bedrock.tf:50-62`) and chains with the two bypasses above into **unbounded Bedrock spend** with no per-team enforcement.
- **Highest-privilege pods have unrestricted internet egress** (LLM agent + fine-tuning trainer; no NetworkPolicy, endpoints disabled) — exfil path for the superuser creds / writer token / weights.

**Resilience**
- **No backups anywhere** (`no-backup-cronjob-anywhere`): single-replica `platform-db` (LiteLLM registry **and** Langfuse) on a 10Gi RWO gp3 PVC with `reclaim_policy=Delete`, EBS snapshotter force-disabled. No WAL/PITR/pg_dump. RPO/RTO are infinite. Accidental PVC delete or AZ loss = permanent loss of all keys/teams/registrations/traces.
- **Single-replica LiteLLM SPOF** (`litellm.yaml:9`) — the one ingress for *all* models; the `maxUnavailable:1` PDB on 1 replica is a no-op.
- **Langfuse stateful tier all single-replica, no backup** (ClickHouse/Redis/MinIO).
- **RWO PVCs pinned to one AZ** (`WaitForFirstConsumer`) — a lost AZ strands platform-db/Langfuse with no auto-recovery; the 3-AZ design buys the data tier nothing.
- **`scale-down.sh` deletes ALL InferenceEndpoints with no confirmation** (`ops/scale-down.sh:29`) — presented as a "pause."
- **`autoDeploy` creates an orphan InferenceEndpoint** (no ownerRef, invisible to ArgoCD — `fine-tuning-job.yaml:262-285`): `git rm` the FineTuneJob leaves a $1–4/hr GPU node running forever.

**Operator / Consumer / Cost**
- **README's first inference command is broken** (`test-model.sh claude-opus-4-8`): the script hard-requires a K8s InferenceEndpoint that Bedrock models never have — the headline zero-GPU demo fails on the very first command.
- **Zero alerting rules** (`rules.tf` has 88 recording rules, 0 alerts) — nothing ever pages an operator on GPU NotReady, OOM, crashloop, PVC-full, or 5xx spikes.
- **Welcome doc tells developers to curl an in-cluster DNS name** unreachable from a laptop, with no tunnel guidance (`team-onboarding.yaml:341,355`).
- **Only one generic Kubelet dashboard ships**; no GPU/model/cost dashboard despite paying for AMP+AMG+SSO (DCGM metrics are exported but never scraped).
- **No upgrade runbook and no backup/restore runbook** — the two most dangerous day-2 tasks have zero guidance; self-managed GPU NodePools have no `expireAfter` (AMIs go stale).
- **3 NAT gateways by default, no single-NAT knob; free S3 gateway endpoint gated off** — idle cost ~$280–350/mo before any inference; Langfuse/ClickHouse forces an always-on extra node.

---

## 3. Strengths (genuinely good — keep these)

- **GitOps spine is clean**: one bootstrap Application → two ApplicationSets; KRO RGDs (`InferenceEndpoint`/`AITeam`/`FineTuneJob`) are a well-chosen self-service API; the `InferenceEndpoint` Ready-status CEL correctly eliminates the rollout false-positive window.
- **Health-agent RBAC design is genuinely thoughtful**: three identities, scoped *write* via per-namespace RoleBindings (not ClusterRoleBinding), and uses `bind` permission instead of permission expansion — the *RBAC* is sound (it's the perimeter + DB superuser + curl|bash that undermine it).
- **litellm-sync finalizer** for clean model deregistration; **idempotent** register/onboard Jobs.
- **Langfuse headless-init + Bedrock day-one** remove real friction; secrets are Terraform-generated and never in git (the in-history ARN is a tfvars slip, not the secret-gen path).
- **`recommend-instance.py` + `compare-models.py`** are strong, differentiated consumer assets.
- Cold-start engineering (EBS snapshot + SOCI + S3 weight cache) is sophisticated and degrades gracefully on cache miss.
- 3 refuted findings show the codebase is *better* than a surface read suggests: the fine-tune validation gate **does** hold (errexit propagates), bundled MinIO **does** persist (default PVC via the gp3 default StorageClass), and `demo-failure.sh`'s SQL is correctly scoped.

---

## 4. Simplification / deduplication / streamlining (primary ask)

Accidental complexity clusters in four places; all fixable without losing capability.

**4.1 One-source-of-truth violations (render from one variable):**
- **Fork URL** edited in 3 places; shipped `sed` fixes only 2.
- **IP allowlist** copied byte-identical into 4 ingress blocks across 2 files.
- **LiteLLM master key** referenced under two Secret names (`litellm-secrets` vs `litellm-api-key`).
- **Region** set 3 inconsistent ways; **frontier model name** disagrees across CLAUDE.md ("Sonnet 4.6") vs shipped config (`claude-opus-4-8`) vs evolution-plan.
- **Lever:** the `platform-config` ConfigMap pattern already works for image tags — extend it to fork-URL/region/CIDR/secret-name via Terraform-emitted values + Kustomize replacements. Turns the "3-place fork edit" into one tfvar.

**4.2 Four components re-implement the same privileged-Job + runtime-download pattern** (litellm-sync, dashboard, investigator, remediator all `pip install`/`curl|bash` at boot; the Remediator Job spec is hand-duplicated between `backend.py` and `event_watcher.py`).
- **Lever:** one pinned, checksum-verified `platform-controller` base image in ECR (the unsloth-trainer already proves the build/push plumbing). Simultaneously kills cold-start fragility, the supply-chain RCE class, and air-gap breakage across all four.

**4.3 Logic that belongs in code lives in KRO/CEL:** the ~220-line `train.py` is a YAML block scalar (untestable, un-lintable) though a trainer Dockerfile already exists; field validation is hand-rolled shell `case` duplicated across two RGDs.
- **Lever:** move `train.py` into the trainer image; push validation into KRO schema enums/min/max (also fixes the consumer "no validation, fail at GPU-spin-up" problem and makes `kubectl explain` useful).

**4.4 Surface that "grew":** 9 tfvars (2 personal/event with real identifiers); two parallel NodePool systems (Auto Mode vs self-managed); dead ADOT collector + dead flag; ~100 lines of never-instantiated VPC-endpoint code; dead `attached_policies`; ~42KB of demo theatre mixed into `ops/`; no shared `ops/lib.sh`.
- **Lever:** collapse to 2 tracked tfvars + operator-private; quarantine demo scripts under `ops/demo/`; delete dead code.

---

## 5. Recommended remediation roadmap

**Phase 0 — Stop-the-bleed (days, before any non-sandbox use)**
1. TLS on the ALB (or flip ingresses to `scheme: internal`).
2. Auth on the dashboard's approve/dismiss/delete (OIDC or bearer); bind approver to real identity.
3. Lock EKS API to `endpoint_public_access_cidrs`; lock Open WebUI signup; mint a scoped WebUI key (never the master key).
4. Scrub the real IdC ARN from git history; delete personal tfvars from the tree.
5. Replace the placeholder IP allowlist with a sentinel that forces operators to set it.

**Phase 1 — Trust boundaries (1–2 weeks)**
6. Scope team egress to LiteLLM only (kill the vLLM bypass); add a key to vLLM backends.
7. Dedicated least-privilege Postgres role for the agent + dashboard (no superuser to LLM pods).
8. Per-incident namespace scoping for the Remediator; drop `--trust-all-tools` for an allowlist.
9. Build the pinned `platform-controller` image (kills curl|bash + 4× duplication at once).
10. Scope the Bedrock policy to the advertised model ARN; add an AWS Budgets/Bedrock alarm.

**Phase 2 — Durability & day-2 (2–4 weeks)**
11. `pg_dump` CronJob → S3 + restore runbook; offer opt-in RDS Multi-AZ + real S3 for Langfuse blobs.
12. ≥2 LiteLLM replicas + `minAvailable:1`; HA for KubeRay; on-demand (not spot) for serving by default.
13. Ship default alert rules → SNS/email; scrape DCGM/vLLM/LiteLLM; bundle GPU/model/cost dashboards.
14. `UPGRADE.md` + `BACKUP.md`; pin add-on chart versions; add `expireAfter` to GPU NodePools.
15. ownerRef on autoDeploy endpoints; confirmation/`--dry-run` on `scale-down.sh`; pin GitOps to a tag for prod.

**Phase 3 — Streamline & cost (2–3 weeks)**
16. Single-source fork-URL/region/CIDR/secret-name via `platform-config`; collapse tfvars; quarantine demo scripts; delete dead code.
17. Single-NAT default for dev/test; un-gate the free S3 gateway endpoint; cap the `default` NodePool; document idle cost + a "cheapest viable" profile.
18. Move `train.py` to the image; KRO schema validation; fix the broken `test-model.sh` first-run path; make the welcome doc laptop-reachable; add a "my model says Degraded — what now?" troubleshooting map.

**Cross-cutting gaps flagged by the completeness critic:** no CI/tests anywhere; no top-level LICENSE/SECURITY.md/SBOM (Open WebUI redistribution terms); no GDPR/PII/data-residency guidance (Langfuse stores full prompts); no EKS-EOL/version-currency story; no managed-capability (KRO/ACK/ArgoCD) failure runbook; no secret-rotation runbook; single-region only.

---

*Counts: 200 unique findings — 3 critical · 32 high · 70 medium · 78 low · 17 info. By dimension: security 56, resilience 41, operator-UX 25, simplification 24, consumer-UX 16, correctness 15, cost 13, docs 10. 124 material findings adversarially verified; 121 confirmed, 3 refuted.*
