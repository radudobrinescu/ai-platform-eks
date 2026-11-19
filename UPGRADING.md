# Upgrading to a new release

Releases are git tags named `vYYYY.MM.N` (for example `v2026.11.0`). Each one
ships a manifest, `releases/<version>.json`, and release notes,
`docs/releases/<version>.md`. You upgrade one environment at a time, one
release at a time.

## The short version

On the branch Argo CD syncs from (usually `main`), with a clean working tree:

```bash
./platformctl upgrade <env>
```

That is the whole procedure. The command fetches the release tags from the
repository you forked from, works out which releases lie between the one your
environment runs and the newest one, and applies them in order. For each
release it merges the tag into your branch, shows the release notes, runs
Terraform, pushes, and waits until the platform is healthy before it starts
the next one.

It needs a git remote named `upstream` that points at the repository you
forked from. Add it once:

```bash
git remote add upstream <url of the repository you forked from>
```

Useful variations:

```bash
./platformctl upgrade <env> --plan-only        # show what the next release would change; change nothing
./platformctl upgrade <env> --to v2026.11.3    # stop at this release instead of the newest
./platformctl upgrade <env> --yes              # do not ask after each release's notes
```

If a release changed a file that you also edited, the merge stops with the
list of conflicting files. Resolve them, `git add -A && git commit`, and run
the same command again.

Do not push a release merge yourself. Argo CD syncs with prune and self-heal,
so a push would roll out the new charts and manifests before Terraform has
created the IAM roles, CRDs and ConfigMaps they need. `upgrade` pushes for
you, after Terraform.

### Merging by hand

You can still merge a release yourself and then run the command; it applies
exactly the release you merged:

```bash
git fetch upstream --tags
git merge v2026.11.0            # the next release after the one you run; do NOT push
./platformctl upgrade <env>
```

Use this for the first upgrade of an environment whose `platformctl` is older
than this procedure: the `platformctl` that runs is the one from the release
you merged. The merge must be a release **tag**. A branch (for example
upstream `main`, or a release candidate) is refused even when it carries a
release manifest, because only tagged releases passed the release tests.

## What `upgrade` does

For each release:

1. **Checks.** The working tree is clean (gitignored files such as your
   tfvars do not count). The branch has an upstream and HEAD contains it. The
   kubectl context `<env>` is current and its API is reachable (skipped with
   `--plan-only`).
2. **Release path.** The deployed release is the one the environment recorded
   at its last upgrade (ConfigMap `platform-release` in namespace
   `ai-platform`). Environments installed before that record existed fall back
   to the newest manifest on the upstream branch (what Argo CD syncs), or
   `baseline` if it has none. If no newer release is merged in HEAD, the next
   release tag is fetched and merged. The target is the newest manifest in
   HEAD: it must be exactly one release after the deployed one, and HEAD must
   contain its tag.
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
   Synced and Healthy on the pushed commit. While it waits it removes nodes
   that the new release no longer allows and that never became ready (for
   example a GPU instance type the node image cannot drive): Karpenter does
   not replace such a node itself, and a model waiting for it would wait for
   ever. A warning names each node removed.
10. **Check.** Runs `./platformctl status <env> --check`.
11. **Kubernetes hint.** If the release recommends a newer Kubernetes
    version than yours, prints how to move to it.
12. **Record.** Writes the release into the `platform-release` ConfigMap, then
    starts the next release if there is one.

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

Nothing special: `./platformctl upgrade <env>` prints the path (for example
`v2026.10.0 -> v2026.11.0 -> v2026.12.0`) and applies the releases one after
the other. Each release shows its own notes and manual steps, and each must
leave the platform healthy before the next one starts. If one stops, fix the
cause and run the command again; it continues from the release the
environment is on.

Releases cannot be skipped: every release is tested as an upgrade from the
one before it. If you merged several releases at once by hand, `upgrade`
refuses and tells you to undo the merge (`git reset --hard ORIG_HEAD` right
after the merge; that keeps commits of your own that are not pushed yet) and
let it apply them in order.

## Several environments

Give each environment its own branch (`gitops_revision` in its tfvars), for
example `env/dev` and `env/prod`, and upgrade them one after the other:

```bash
git switch env/dev  && ./platformctl upgrade dev
git switch env/prod && ./platformctl upgrade prod
```

With one branch for two environments, the push at the end of the first
upgrade would also roll the new manifests out to the second environment,
before its Terraform ran.

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
(`git reset --hard ORIG_HEAD`), move Kubernetes up on the release you run now, and
merge again.

## If a step fails

The command is safe to rerun. Stages that were already applied plan no
changes the second time.

| Where it stopped | State | What to do |
|---|---|---|
| Checks, release notes (steps 1 to 5) | Nothing changed in the environment. The release is merged in your checkout, not pushed | Fix what the message says, rerun: it continues with that release. To drop the merge instead: `git reset --hard ORIG_HEAD` |
| Plan guard (step 6 or 7) | Nothing applied in that stage | See [When the plan guard stops](#when-the-plan-guard-stops) |
| Terraform plan or apply of a stage (step 7) | Earlier stages applied, nothing pushed. Argo CD still runs the previous release's manifests | Fix the cause (often credentials, quotas or the API allowlist: `./platformctl allow-ip <env>`), rerun `upgrade` |
| Push rejected (step 8) | Terraform applied, nothing pushed | Someone pushed in the meantime. `git pull --no-rebase`, `git push`, then `./platformctl status <env> --check` |
| Argo CD not converged or `status --check` failed (steps 9, 10) | Everything applied and pushed; the environment is still recorded on the previous release | Inspect the listed applications (`kubectl get applications -n argocd`). When they are healthy, rerun `upgrade`: Terraform plans no changes, the checks run again, and the release is recorded |
| A later release of several (step 12) | The earlier releases are applied, pushed and recorded | Fix the cause, rerun `upgrade`; it continues with the release that stopped |

There is no automatic rollback. Undoing the git merge does not undo Terraform
changes that were already applied, so fix forward and rerun.
