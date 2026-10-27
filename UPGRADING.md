# Upgrading to a new release

Releases are git tags named `vYYYY.MM.N` (for example `v2026.11.0`). Each one
ships a manifest, `releases/<version>.json`, and release notes,
`docs/releases/<version>.md`. You upgrade one environment at a time, one
release at a time.

## The short version

On the branch Argo CD syncs from (usually `main`), with a clean working tree:

```bash
git fetch upstream --tags
git merge v2026.11.0            # the next release after the one you run; do NOT push
./platformctl upgrade <env>
```

`upstream` is a git remote pointing at the repository you forked from
(`git remote add upstream <url>` once). If you edited files that the release
also changed, resolve the merge conflicts and commit before you run the command.

Merge with plain git first, then run the command. The `platformctl` that runs
is the one from the new release, so this works even when your current
version does not have the `upgrade` command yet.

Do not push the merge yourself. Argo CD syncs with prune and self-heal, so a
push would roll out the new charts and manifests before Terraform has created
the IAM roles, CRDs and ConfigMaps they need. `upgrade` pushes for you, after
Terraform.

To see what would change without changing anything:

```bash
./platformctl upgrade <env> --plan-only
```

## What `upgrade` does

1. **Checks.** The working tree is clean (gitignored files such as your
   tfvars do not count). The branch has an upstream, HEAD contains it and is
   ahead of it. The kubectl context `<env>` is current and its API is
   reachable (skipped with `--plan-only`).
2. **Release path.** The deployed release is the newest manifest on the
   upstream branch (what Argo CD syncs), or `baseline` if it has none. The
   target is the newest manifest in HEAD. Exactly one release apart is
   accepted.
3. **Kubernetes.** `kubernetes_version` in `<env>.tfvars` must be in the
   release's supported list.
4. **tfvars.** Every variable the release lists in `required_tfvars` must be
   set in `<env>.tfvars`. Missing ones are printed with the line to copy from
   `example.tfvars`.
5. **Release notes.** Prints the notes and any manual steps, then asks you to
   type `yes` (`--yes` skips the question).
6. **Plan guard pass.** Plans every Terraform stage in order and runs the plan
   guard on each. Nothing is applied in this pass. `--plan-only` stops here
   (exit 0 if clean, 3 if the guard found something).
7. **Apply.** Stage by stage: a fresh saved plan, the guard again on that
   plan, then Terraform applies exactly that saved plan.
8. **Push.** Pushes HEAD to the upstream branch and asks Argo CD to refresh
   the `bootstrap` application.
9. **Converge.** Waits up to 30 minutes until every Argo CD Application is
   Synced and Healthy on the pushed commit.
10. **Check.** Runs `./platformctl status <env> --check`.
11. **Kubernetes hint.** If the release recommends a newer Kubernetes
    version than yours, prints how to move to it.

## The plan guard

The guard reads each stage's plan (`terraform show -json`) and stops on any
**delete or replace** of these resource types:

| Kind | Types |
|---|---|
| Cluster and nodes | `aws_eks_cluster`, `aws_eks_node_group`, `aws_eks_addon`, `aws_eks_capability` |
| Network | `aws_vpc`, `aws_subnet`, `aws_nat_gateway`, `aws_eip`, `aws_internet_gateway` |
| Data and keys | `aws_s3_bucket`, `aws_dynamodb_table`, `aws_kms_key`, `aws_cognito_user_pool` |
| Identity | `aws_iam_openid_connect_provider` |
| In-cluster | `kubernetes_namespace`, `kubernetes_persistent_volume_claim`, `helm_release`, `kubectl_manifest` |

`kubectl_manifest` is included because it holds the Argo CD `bootstrap`
Application and the Karpenter NodePools; deleting either removes running
workloads. Creates and in-place updates always pass, and so do deletes of
types not in the table.

### When the plan guard stops

The output names each blocked change:

```
stage 30.eks/30.cluster: 1 to create, 1 to update, 0 to delete, 2 to replace
  GUARDED  replace  aws_s3_bucket.model_cache[0]  data and keys (aws_s3_bucket)
```

Nothing has been applied at that point. Find out why the resource is
replaced: the release notes, or `terraform show` on the saved plan in
`terraform/tf-plans/` (these files contain secrets in plain text; keep them
local). A replace is often caused by a tfvars value that differs from what
the release expects, and changing the value removes it.

If the change is intended (for example the resource holds only data you can
regenerate), rerun with the flag the guard printed:

```bash
./platformctl upgrade <env> --allow-replace '30.eks/30.cluster:aws_s3_bucket.model_cache[0]'
```

`--allow-replace` takes `STAGE:ADDRESS` (that stage only) or `ADDRESS` (any
stage), and can be repeated. Quote it: addresses contain brackets.

A release that replaces a protected resource on purpose lists it under
`acknowledged_replacements` in its manifest, with the reason. The guard then
shows it as `allowed` with that reason, and you do not need the flag.

## Several releases behind

Upgrade through each release in order. `upgrade` refuses HEAD when it is more
than one release ahead of what is deployed, and prints the order. For example,
from `v2026.10.0` to `v2026.12.0`:

```bash
git merge v2026.11.0 && ./platformctl upgrade <env>
git merge v2026.12.0 && ./platformctl upgrade <env>
```

If you already merged the newest tag, undo that merge first with
`git reset --hard @{u}`. The command requires a clean tree before it starts,
so this discards only the merge.

## Kubernetes versions

Each release supports the Kubernetes versions of the previous release plus at
most one newer minor version. So if your cluster runs a version the previous
release supported, you can take the next release without changing Kubernetes
first.

Move Kubernetes after an upgrade, one minor version at a time (EKS upgrades
the control plane one minor at a time):

1. Set `kubernetes_version` in `cluster_config` in `<env>.tfvars` to the next
   minor version, for example `"1.36"` to `"1.37"`.
2. Review the plan of the cluster stage:
   `AWS_REGION=<region> make -C terraform plan MODULE=30.eks/30.cluster ENVIRONMENT=<env>`
   (the `region` from your tfvars).
   The cluster and node groups must show as updated in place, not replaced.
3. Apply: `./platformctl up <env>`, then `./platformctl status <env> --check`.

Repeat until you reach the release's recommended version. If `upgrade` says
your version is too old for the new release, undo the merge
(`git reset --hard @{u}`), move Kubernetes up on the release you run now, and
merge again.

## If a step fails

The command is safe to rerun. Stages that were already applied plan no
changes the second time.

| Where it stopped | State | What to do |
|---|---|---|
| Checks, release notes (steps 1 to 5) | Nothing changed | Fix what the message says, rerun |
| Plan guard (step 6 or 7) | Nothing applied in that stage | See [When the plan guard stops](#when-the-plan-guard-stops) |
| Terraform plan or apply of a stage (step 7) | Earlier stages applied, nothing pushed. Argo CD still runs the previous release's manifests | Fix the cause (often credentials, quotas or the API allowlist: `./platformctl allow-ip <env>`), rerun `upgrade` |
| Push rejected (step 8) | Terraform applied, nothing pushed | Someone pushed in the meantime. `git pull --no-rebase`, `git push`, then `./platformctl status <env> --check` |
| Argo CD not converged or `status --check` failed (steps 9, 10) | Everything applied and pushed | Inspect the listed applications (`kubectl get applications -n argocd`). Rerunning `upgrade` reports nothing to upgrade, because the release is already pushed |

There is no automatic rollback. Undoing the git merge does not undo Terraform
changes that were already applied, so fix forward and rerun.
