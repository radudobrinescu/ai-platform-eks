# ArgoCD Cluster Configuration
This directory contains the cluster secret needed for EKS managed ArgoCD to connect to the cluster.

## Manual Setup Required
The `cluster-secret.yaml` needs to be updated with the actual EKS cluster endpoint:

```bash
# Get cluster endpoint
CLUSTER_ENDPOINT=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')

# Update the secret
sed -i "s|https://kubernetes.default.svc|$CLUSTER_ENDPOINT|g" platform/argocd/cluster-secret.yaml

# Apply the secret
kubectl apply -f platform/argocd/cluster-secret.yaml
```

This should be automated in the Terraform deployment process.
