"""Cluster dashboard backend.

Pure logic (cost, models, diagnostics) is separate from I/O (Kubernetes,
LiteLLM, Prometheus, pricing) so it can be tested with recorded inputs. Shipped
in the hashed `cluster-dashboard-backend` ConfigMap under flat keys
(`dashboard__cost.py`) and projected back to `dashboard/cost.py` in the pod.
"""
