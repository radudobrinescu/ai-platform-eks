# lifecycle: update machinery for the AI platform

This orphan branch holds everything that keeps the platform's component versions
current and proves them on real clusters. It holds no product code: the product
lives on `main` (and its `release/*` branches), which never contain anything from
here.

| Path | What |
|---|---|
| `renovate.json` | Renovate config (read from the default branch), targets `main` only |
| `COMPATIBILITY.md` | anchors, domains, coupling notes, validated sets, release procedure |
| `harness/` | E2E harness: `e2e.py` (entry), `checks.py` (22 smoke checks; `e2e.py check` runs them alone against a running env), `sso.py`, `survival.py`, `tfvars.py`, `versions.py`, `subject.py`, `common.py` |
| `tools/release.py` | cut a release branch; write release notes, manifest, validated-versions row |
| `tools/release_candidate.py` | decide what the factory does next: start a cycle, wait, retry, finish or abandon it (used by `release-candidate.yaml`) |
| `tools/shellcheck_workflows.py` | shellcheck every workflow `run:` step |
| `terraform/github-oidc/` | GitHub OIDC provider + E2E provisioning role (local state, applied once) |
| `tests/` | unit tests: harness, tools, workflows, Renovate coverage of `main` |
| `.github/workflows/` | `ci.yaml` (static checks), `e2e.yaml` (dispatch-only E2E), `coverage.yaml` (weekly coverage guard), `release-candidate.yaml` (twice a day: one step of the factory), `release-finish.yaml` (after a green E2E run: merge; tag and publish a release), `watchdog.yaml` (weekly: tokens, disabled workflows, stalled cycles), `main-schedules.yaml` (runs `main`'s scheduled workflows, since `schedule` fires only on the default branch) |

## How a run works

The harness never edits the product it tests. It checks out the ref under test
into `subject/` and drives it only through that ref's own `./platformctl`
(`up`, `down`, `new-model ... --deploy --yes`, `new-model --undeploy`,
`onboard-team ... --deploy -y`, `upgrade`), plus kubectl, the AWS CLI and the
services' HTTP APIs.

1. Branch `e2e/<env>` is created at the ref and pushed; Argo CD tracks it.
   Commits that `./platformctl` makes (models, teams, the upgrade merge) land
   there. It is deleted at teardown.
2. The env tfvars are rendered from the subject's own `example.tfvars` (what a
   forker copies) with only `region`, the runner's IP in
   `cluster_endpoint_public_access_cidrs`, `gitops_repo_url` and
   `gitops_revision` changed. Profile `managed` also turns on EKS Managed
   Capabilities with the Identity Center values from repository variables.
3. **fresh**: `./platformctl up`, smoke suite, teardown.
   **upgrade**: `up` the base, seed state (a team with key and spend, a Bedrock
   model, a small vLLM model, Langfuse traces, Open WebUI users), snapshot it,
   `git merge` the candidate locally, fill new required tfvars from the new
   `example.tfvars`, run the candidate's `./platformctl upgrade --yes`, verify the
   seeded state survived, smoke suite, then the Kubernetes minor bump if the
   release recommends one.
4. Teardown: `./platformctl down` (the env name is typed on stdin, as on
   `main`), the env-scoped orphan sweep, branch deletion. Skipped with `keep`.

Env names are `e2e<m><p><run number>` (`m` = f/u, `p` = d/m, run number mod
10000), cluster `ai-platform-e2eud17`: EKS caps the system node group's name
prefix at 37 characters. Reports (`reports/<env>.md|json`) and the streamed
command output redact account ids, ARNs and AWS hostnames.

## One-time setup (Unit 4, by an admin)

1. Apply the OIDC role with local state:
   ```bash
   terraform -chdir=terraform/github-oidc init
   terraform -chdir=terraform/github-oidc apply -var 'github_repositories=["OWNER/REPO"]'
   ```
   Set `create_oidc_provider=false` if the account already has the GitHub provider.
2. Repository settings:
   - variable `E2E_ROLE_ARN` = the `role_arn` output
   - secret `E2E_PUSH_TOKEN`: fine-grained PAT on this repo with Contents and
     Workflows write (release candidates change `.github/workflows/*`, which
     `GITHUB_TOKEN` cannot push), and Pull requests write so the scheduled
     release PR is opened as that user and main's CI runs on it
   - variables `CYCLE_DAYS` (7), `PUBLISH_DAYS` (90), `WAIT_DAYS` (5): all optional, see
     "The factory: cycles and releases"
   - label `hold` (keeps a Renovate PR out of the next release)
   - variable `E2E_REGION` (optional, default `eu-central-1`)
   - variables `E2E_IDC_INSTANCE_ARN`, `E2E_IDC_REGION`, `E2E_IDC_GROUP_ID`
     (Identity Center instance and the group mapped to Argo CD ADMIN, profile `managed`)
   - environment `e2e` with a required reviewer
3. Make `lifecycle` the default branch (Renovate reads its config there, and
   GitHub only offers `workflow_dispatch` for workflows on the default branch).

Until `E2E_ROLE_ARN` and `E2E_PUSH_TOKEN` exist, `e2e.yaml` finishes green
without calling AWS.

## Dispatch a run and read the result

Actions -> e2e -> Run workflow: `modes` (fresh | upgrade | fresh,upgrade),
`subject` (the ref under test; in upgrade mode the candidate), `base` (upgrade
mode, default `main`), `profiles` (default `default,managed`), `keep`,
`with_llmd`. The run is named after these, e.g. `e2e fresh,upgrade ·
release/v2026.11.0 (from main) · default,managed`, so the approval screen says
what it tests. One approval of the `e2e` environment starts every
(mode, profile) job of the run in parallel (each env uses 3 Elastic IPs; the
account quota is 15).

Each job posts commit status `e2e/<mode>-<profile>` on the subject SHA (upgrade:
the candidate SHA): pending at start, success or failure at the end, linking to
the run. The report is in the job summary and the `e2e-report-*` artifact.

## Run from a laptop

```bash
git worktree add --detach subject origin/main      # the product ref under test
export AWS_PROFILE=<provisioning profile>            # credentials for the E2E account
python3 harness/e2e.py fresh main --profile default --run-number 1 \
  --repo-url https://github.com/OWNER/REPO.git \
  --operator-cidr "$(curl -fsS https://checkip.amazonaws.com)/32"
```

The subject's `origin` must accept pushes (the run pushes `e2e/<env>`). On macOS
the harness holds a sleep assertion (`caffeinate`) for the run.

Dry run (offline, no git, AWS or cluster calls): prints every planned command
and the rendered tfvars.

```bash
python3 harness/e2e.py upgrade main release/v2026.11.0 --profile managed --dry-run
```

### Checks only, against an env that already runs

To try a harness or check change in minutes instead of a full release run:
no provision, upgrade or teardown. `--subject-dir` is the checkout the env was
deployed from (it holds `<env>.tfvars`, is clean, and tracks the branch Argo CD
follows). The model checks deploy and remove their test models with commits on
that branch, so on dev those commits stay in `env/dev`'s history.

```bash
python3 harness/e2e.py check --env dev --subject-dir ../dev --only argocd-applications-converged,litellm-ready,cluster-dashboard-healthz
python3 harness/e2e.py check --env dev --subject-dir ../dev --with-llmd   # all checks, llm-d and KEDA too
```

`--survival` also seeds a team, snapshots twice and compares (it leaves the
team in place). The report is written to `reports/<env>-check.md`.

GPU models for the GPU checks are started right after the GitOps checks, so
their nodes come up while the other checks run.

### Image scan

Fresh jobs end with a Trivy scan of every image running in the cluster, by the
digest the nodes pulled (`--image-scan`; `e2e.py check --image-scan` does the
same against dev). The report lists the HIGH and CRITICAL vulnerabilities that
have a fix, worst image first; the full list is `reports/<env>-image-scan.json`.
It is report-only: `--image-scan-fail-on CRITICAL` turns it into a failing
check `security/no-fixable-critical-cves` (not enabled yet). It catches what
Renovate cannot see: tags that are never rebuilt and chart lines that ended
with frozen images. Trivy is pinned and checksummed in `e2e.yaml`.

## The factory: cycles and releases

`release-candidate.yaml` runs twice a day and does one step per run; `tools/release_candidate.py plan` decides
which. Nobody has to watch it: the only thing a person does is approve the `e2e` environment.

**A cycle** (every `CYCLE_DAYS`, default 7) cuts `release/<version>` = `main` + the eligible Renovate group
branches + the notes and manifest of the next release, opens its PR and dispatches E2E: a fresh install, and an
upgrade **from the last published release** (the newest tag), both profiles. When all four `e2e/*` statuses are
green, the last job of the E2E run starts `release-finish.yaml`, which merges the candidate into `main`.

**A release** is a cycle whose PR carries the label `publish`: after the merge it is also tagged and gets its
GitHub Release. A cycle gets the label when the newest tag is `PUBLISH_DAYS` old (default 90), when it includes
a PR labelled `security`, or when dispatched with `publish`. Add or remove the label on the PR to change that
before E2E is green. All other cycles are integrations: `main` moves forward, proven, with no tag. So `main`
always holds a state that was tested as one upgrade from the last published release, the version name
(`vYYYY.MM.N`) is reused until a cycle is published, and the notes always describe "since the last release".

**When E2E fails** the failed jobs are dispatched once more (one approval). If they fail again, the candidate
is abandoned: PR closed, branch deleted, and the suspects marked with commit statuses. One suspect: it failed
alone (`candidate/solo`) and stays out until its branch gets a newer commit. Several: each is marked
`candidate/combined` and the next cycles, one a day, test one suspect at a time, `main` first if it had commits
no cycle had tested. If `main` fails alone, cycles stop until `main` moves. A candidate that nobody approves
for `WAIT_DAYS` (default 5) is abandoned without marks and cut again, so an approver never approves a stale
one. A group that does not merge cleanly is left out of that cycle. At most one of the anchor updates (vLLM,
the EKS Kubernetes version) goes into a cycle.

**What you are approving.** The E2E run's name starts with what the candidate changes on top of `main`
(for example `cycle v2026.11.8: 2 updates: vllm v0.34.1, open-webui v0.13.5 · e2e fresh,upgrade · ...`), so the
approval request says it. The run's summary page, where the approval button is, shows the candidate's pull
request: a table of the changed components (anchor, major and CI-only ones marked), whether it is a release,
whether it includes a security update, and what happens if you do not approve (nothing: after `WAIT_DAYS` the
candidate is replaced by a fresh one, so letting an unimportant cycle wait is safe).

**Leaks and quiet failures.** Every E2E job starts with a preflight (`harness/leftovers.py`): E2E
environments older than 8 hours are reported in the issue "E2E environments left behind" (they bill until a
person tears them down), and a job that would not get its 3 Elastic IPs is marked "not run" instead of
"failed", so a full account is never blamed on the updates under test. A job whose own cluster or VPC is
still there after its teardown says so in the same issue at once (`leftovers.py --own`). `watchdog.yaml` runs weekly without
any cloud credentials (`tools/watchdog.py`): it warns 30 days before a push token expires, when a factory
workflow is disabled, when the scheduled run stopped, when no cycle was merged for 21 days, and when the
attention issue stays open. It keeps the issue "Factory status" up to date.

The issue "Release candidate needs attention" is opened or commented on when a step fails, when a candidate
is abandoned and when `main` fails alone; `release-finish.yaml` closes it when a cycle is merged.

By hand:

```bash
gh workflow run release-candidate.yaml -f dry_run=true           # what would the next run do?
gh workflow run release-candidate.yaml -f force=true             # start a cycle now
gh workflow run release-candidate.yaml -f force=true -f publish=true   # start a cycle that is a release
python3 tools/release.py --repo subject finish v2026.11.3 --github OWNER/REPO [--dry-run] [--publish|--no-publish]
```

### An environment that was left behind

The issue "E2E environments left behind" names a cluster (`<prefix>-<env>`, for example `ai-platform-e2eud129`).
It bills until a person removes it; the factory never deletes a cluster it does not own in the current job.
With credentials for the E2E account and the Terraform version that `e2e.yaml` installs:

1. Check out the product at the newest release, not at the ref the job ran. An old `down` may be what left
   the cluster (the September `down` reported success with the cluster running).
2. Recreate the env's variables the way the harness does (`harness/tfvars.py`): copy
   `terraform/00.global/vars/example.tfvars` to `<env>.tfvars` in the same directory, set the region, put your
   public IP (`a.b.c.d/32`) in `cluster_endpoint_public_access_cidrs`, and for an env whose name has `m` as its
   fifth letter (profile managed) set `eks_capabilities = true` and the `capabilities_config` block.
3. `./platformctl down <env>`. It asks for the env name, destroys the Terraform modules in reverse order and
   sweeps what the controllers created. It is safe to run again.
4. Check: `aws eks describe-cluster --name <prefix>-<env>` says the cluster does not exist, and no VPC has
   that name. Then close the issue.

What stopped the destroy on 2026-11-18, and what cleared it:

- The cluster's KMS key was scheduled for deletion while the cluster still existed, and EKS refused every
  change. `aws kms cancel-key-deletion` and `aws kms enable-key` on that key; the sweep schedules it again
  once the cluster is gone.
- The job's role had created the cluster and its runner was gone, so nobody could reach the Kubernetes API.
  An EKS access entry for your own identity with `AmazonEKSClusterAdminPolicy`, plus your IP in the
  cluster's public access list.
- The namespace `inference` waited on a finalizer whose controller was already deleted. Remove the
  finalizers of what is left in it (`bedrockmodels`, the Argo CD applications, `nodeclaims`), then destroy
  again.

### Publishing to the public repository

A release reaches the public repository as **one commit**: the release's tree on top of the public `main`,
the tag, and the GitHub Release (titled with the Kubernetes version). None of this repository's history goes
with it. `release-finish.yaml` does it for the newest release when these are set, and does nothing otherwise:

- variable `PUBLIC_REPO` (`OWNER/REPO`), variable `PUBLIC_SINCE` (the first version to publish; older, internal
  releases are never published), secret `PUBLIC_PUSH_TOKEN` (contents write on the public repository).

It refuses when the public `main` is not where the previous release left it (something was merged there
directly: bring it into `main` here, so it ships with a release). A publication that failed is repeated: the
factory starts `release-finish` every day while the newest release is not public. By hand:

```bash
export PUBLIC_URL=https://github.com/OWNER/PUBLIC-REPO.git      # with a credential helper, or a token in the URL
python3 tools/release.py --repo subject publish-public v2027.01.0 --url-env PUBLIC_URL --github OWNER/PUBLIC-REPO [--dry-run]
```

**The first public release** is cut by hand, once, because it does not upgrade from a release of this
repository but from what the public repository holds today:

```bash
git -C subject fetch https://github.com/OWNER/PUBLIC-REPO.git main      # FETCH_HEAD = the public main; note its SHA
python3 tools/release.py --repo subject cut v2027.01.0                  # from main; add group branches if wanted
python3 tools/release.py --repo subject notes v2027.01.0 --base <public main SHA> \
    --kubernetes-supported <what public forks may run>,<today's default> --kubernetes-recommended <today's default>
#   -> one manifest, `previous: baseline`; the manifests and notes of the internal releases are removed
#   Add --manual-step "..." for every manual step of the internal releases that still applies to someone
#   coming from the public state (read their manifests first: only the newest one is carried over).
git -C subject push -u origin release/v2027.01.0
gh pr create --base main --head release/v2027.01.0 --title "release: v2027.01.0" --label publish --body "First public release"
gh workflow run e2e.yaml --ref lifecycle -f modes=fresh,upgrade -f subject=release/v2027.01.0 \
    -f base=<public main SHA> -f profiles=default,managed
```

From there it is an ordinary cycle (the factory sees `release/v2027.01.0` in flight): approve E2E, and when it is
green `release-finish` merges, tags and, with the three settings above and `PUBLIC_SINCE=v2027.01.0`, publishes.
The upgrade jobs of that run are the test of the jump existing public forks will make. The long-lived dev
environment sits on the internal releases and cannot take this one with `upgrade`; recreate it from the tag.

`finish` commits nothing after E2E: what was tested is what is merged and tagged. It

1. refuses unless the four statuses on the release head are green **and** come from the release's own run:
   upgrade jobs started from the previous release, every job ran the llm-d tier (the status text carries
   `base=` and `llmd=`; a manual run with other inputs on the same commit does not count);
2. refuses when `main` moved since the cut (`main` must be an ancestor of the release head, so the merge
   result is the tested tree);
3. waits for the PR's required checks, merges the PR at exactly that head, and checks that the merge
   commit's tree is the tested tree;
4. for a release (label `publish`): tags the merge commit and creates the GitHub Release from the notes;
5. deletes the release branch.

It resumes: if an earlier run stopped after the merge or after the tag, run it again and it continues from
there. `release.py publish <v> --github OWNER/REPO` creates the GitHub Release
for an existing tag. The notes list the platform changes (PRs merged into main
since the previous release, grouped as fixes, improvements and other changes;
no PR numbers, because the notes travel to the public repo). See COMPATIBILITY.md "Releases". The steps by hand:

```bash
python3 tools/release.py --repo subject cut v2026.11.0 renovate/infra renovate/gateway
python3 tools/release.py --repo subject notes v2026.11.0 --base main      # first release
# E2E: fresh + upgrade (base = previous tag, or main), both profiles, llm-d on
python3 tools/release.py --repo subject finish v2026.11.0 --github OWNER/REPO     # after all 4 runs pass
```

`notes` writes `docs/releases/<v>.md`, `releases/<v>.json` (the manifest
`./platformctl upgrade` reads) and the `docs/validated-versions.md` row, and
commits them. Files of a version that was never tagged are drafts from earlier
cycles: it removes them and carries what was written into their manifest by
hand (`manual_steps`, `acknowledged_replacements`) into the new one. The row has no E2E column: a release reaches `main`, and so that
table, only through `finish`. `notes` refuses a version that is not `vYYYY.MM.N`, a version not newer
than the base release, a Kubernetes set that drops a version of the previous
release or adds more than one newer minor, and any text that would mention
Renovate, this branch, or an account id.

Any push to the release branch after E2E needs a new E2E run (BR2, no exceptions): the statuses are on
the tested commit only.

## Checks

```bash
python3 -m pytest -q             # needs ./subject (a checkout of main); CI sets REQUIRE_SUBJECT=1
ruff check .
python3 tools/shellcheck_workflows.py .github/workflows/*.yaml
npx --yes renovate-config-validator --strict renovate.json
terraform -chdir=terraform/github-oidc init -backend=false && terraform -chdir=terraform/github-oidc validate
```

Renovate's own extraction against `main` (needs Node 24; no token, no network
lookups):

```bash
d=$(mktemp -d) && git -C subject archive HEAD | tar -x -C "$d" && cp renovate.json "$d"/ \
  && (cd "$d" && git init -q && git add -A && git -c user.name=x -c user.email=x@x commit -qm x \
  && npx --yes -p node@24 -p renovate -c 'renovate --platform=local --dry-run=extract --report-type=file --report-path=/tmp/renovate-extract.json')
```
