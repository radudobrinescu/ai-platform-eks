# Release manifests

Each release adds one file, `releases/<version>.json`, next to its notes in
`docs/releases/<version>.md`. `./platformctl upgrade` reads the manifest to
decide whether an environment can move to the release and what to check
first (see [UPGRADING.md](../UPGRADING.md)). The files are plain JSON, so the
command needs only the Python standard library.

## Format

```json
{
  "version": "v2026.11.0",
  "previous": "v2026.10.0",
  "kubernetes": { "supported": ["1.36", "1.37"], "recommended": "1.37" },
  "required_tfvars": ["some_new_variable"],
  "manual_steps": ["Free text shown before anything changes."],
  "acknowledged_replacements": [
    { "stage": "30.eks/30.cluster", "address": "aws_s3_bucket.model_cache[0]",
      "reason": "bucket name now includes the account id; regenerable cache" }
  ]
}
```

| Field | Meaning |
|---|---|
| `version` | Release tag, `vYYYY.MM.N`. Must match the file name. `N` counts releases within the month, from 0. |
| `previous` | The release this one upgrades from, or `baseline` for the first release. `upgrade` refuses to run unless the deployed release is exactly this one. |
| `kubernetes.supported` | Kubernetes minor versions (`1.NN`) the release is tested on. It keeps every version of the previous release and adds at most one newer minor. |
| `kubernetes.recommended` | One of `supported`. After the upgrade, `upgrade` suggests moving to it when the cluster runs an older version. |
| `required_tfvars` | Variable names that must be set in `<env>.tfvars` (anywhere outside comments, including inside objects such as `cluster_config`). Use it for new variables without a safe default. |
| `manual_steps` | Steps the operator must do before the upgrade. Printed before anything changes. Empty list if none. |
| `acknowledged_replacements` | Deletes or replaces of protected resources that this release makes on purpose. `stage` is the directory under `terraform/` (for example `30.eks/30.cluster`); `address` is the Terraform address exactly as in the plan; `reason` is shown to the operator. Empty list if none. |

All keys are required. Use empty lists rather than leaving keys out.

## Rules

- Each release has exactly one manifest, and published manifests are not
  edited afterwards. The deployed release is detected from them.
- Acknowledge a replacement only when the data it holds can be lost or
  regenerated, and say so in `reason`.

## Validate

```bash
python3 ops/lib/release_manifest.py validate releases/*.json
```
