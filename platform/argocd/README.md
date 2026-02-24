# ArgoCD Cluster Secret for EKS Managed ArgoCD

This directory contains the cluster secret configuration required for EKS managed ArgoCD to deploy applications to the local cluster.

## Important Notes

**EKS Managed ArgoCD Requirements:**
- The `server` field must contain the **EKS cluster ARN**, not the API endpoint or `kubernetes.default.svc`
- Format: `arn:aws:eks:REGION:ACCOUNT:cluster/CLUSTER_NAME`
- This is different from self-managed ArgoCD installations

**Current Configuration:**
- Cluster: `ai-gitops-gitops-test`
- Region: `eu-central-1`
- Account: `802019299867`

## Deployment

This secret is automatically applied as part of the platform application sync. It enables ArgoCD to recognize the local cluster as a deployment target named `in-cluster`.

## Troubleshooting

If you see errors like "there are no clusters with this name: in-cluster", verify:
1. The cluster secret exists: `kubectl get secret in-cluster -n argocd`
2. The server field contains the correct EKS cluster ARN
3. The secret has the correct label: `argocd.argoproj.io/secret-type=cluster`
