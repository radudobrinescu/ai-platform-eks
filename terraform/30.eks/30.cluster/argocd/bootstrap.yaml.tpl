apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: bootstrap
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: ai-platform
    ai-platform/managed-by: terraform
spec:
  project: default
  source:
    repoURL: ${repo_url}
    targetRevision: ${revision}
    path: argocd/bootstrap
  destination:
    name: local-cluster
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
