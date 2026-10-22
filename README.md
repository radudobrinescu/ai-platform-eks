# lifecycle: update machinery for the AI platform

This orphan branch holds everything that keeps the platform's component versions
current and proves them on real clusters. It holds no product code: the product
lives on `main` (and its `release/*` branches), which never contain anything from
here.

| Path | What |
|---|---|
| `renovate.json` | Renovate config (read from the default branch), targets `main` only |
| `COMPATIBILITY.md` | anchors, domains, coupling notes, validated sets, release procedure |
| `harness/` | E2E harness: `e2e.py` (entry), `checks.py` (21 smoke checks), `sso.py`, `survival.py`, `tfvars.py`, `versions.py`, `subject.py`, `common.py` |
| `tools/release.py` | cut a release branch; write release notes, manifest, validated-versions row |
| `tools/shellcheck_workflows.py` | shellcheck every workflow `run:` step |
| `terraform/github-oidc/` | GitHub OIDC provider + E2E provisioning role (local state, applied once) |
| `tests/` | unit tests: harness, tools, workflows, Renovate coverage of `main` |
| `.github/workflows/` | `ci.yaml` (static checks), `e2e.yaml` (dispatch-only E2E), `coverage.yaml` (weekly coverage guard) |

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

Env names are `e2e-<m><p>-<run number>` (`m` = f/u, `p` = d/m), cluster
`ai-platform-e2e-ud-17`. Reports (`reports/<env>.md|json`) and the streamed
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
     `GITHUB_TOKEN` cannot push)
   - variable `E2E_REGION` (optional, default `eu-central-1`)
   - variables `E2E_IDC_INSTANCE_ARN`, `E2E_IDC_REGION`, `E2E_IDC_GROUP_ID`
     (Identity Center instance and the group mapped to Argo CD ADMIN, profile `managed`)
   - environment `e2e` with a required reviewer
3. Make `lifecycle` the default branch (Renovate reads its config there, and
   GitHub only offers `workflow_dispatch` for workflows on the default branch).

Until `E2E_ROLE_ARN` and `E2E_PUSH_TOKEN` exist, `e2e.yaml` finishes green
without calling AWS.

## Dispatch a run and read the result

Actions -> e2e -> Run workflow: `mode` (fresh | upgrade), `subject` (the ref
under test; in upgrade mode the candidate), `base` (upgrade mode, default
`main`), `profiles` (default `default,managed`), `keep`, `with_llmd`. A reviewer
approves the `e2e` environment, then one job per profile runs in parallel
(each env uses 3 Elastic IPs; the account quota is 15).

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

## Cut a release

See COMPATIBILITY.md "Releases". In short:

```bash
python3 tools/release.py --repo subject cut v2026.11.0 renovate/infra renovate/gateway
python3 tools/release.py --repo subject notes v2026.11.0 --base main      # first release
# E2E: fresh + upgrade (base = previous tag, or main), both profiles
python3 tools/release.py --repo subject record v2026.11.0 --github OWNER/REPO     # after all 4 runs pass
```

`notes` writes `docs/releases/<v>.md`, `releases/<v>.json` (the manifest
`./platformctl upgrade` reads) and the `docs/validated-versions.md` row, and
commits them. It refuses a version that is not `vYYYY.MM.N`, a version not newer
than the base release, a Kubernetes set that drops a version of the previous
release or adds more than one newer minor, and any text that would mention
Renovate, this branch, or an account id.

### Recording the E2E results (decision A)

The upgrade E2E needs `releases/<v>.json` in the candidate, so `notes` runs
before E2E and the row says "not run". After all four runs pass:

```bash
python3 tools/release.py --repo subject record v2026.11.0 --github OWNER/REPO
```

`record` is the one allowed change after E2E (BR2 exception). It refuses unless
all four `e2e/*` statuses are `success` on the tested head; writes only the E2E
cell of that release's row as a single commit (`Tested commit: <sha>` in the
message); checks the commit changes nothing else; pushes; and posts the four
statuses on the new head, each linking the original run and naming the tested
SHA. `--no-push` commits and verifies only. Any other change needs a rerun.

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
