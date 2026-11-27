"""A Service must not select the pods of a Job.

A Job pod that carries the labels a Service selects becomes an endpoint of that
Service (and a load balancer target) for as long as it runs, although it listens
on nothing: requests routed to it fail. Seen on the dashboard's key CronJob and
on Open WebUI's key Job, whose pods carried the `app` label of the UI they serve.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIRS = ("platform/services", "platform/config")


def _documents() -> list[dict]:
    docs: list[dict] = []
    for directory in MANIFEST_DIRS:
        for path in sorted((ROOT / directory).rglob("*.yaml")):
            try:
                loaded = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            except yaml.YAMLError:
                continue                      # a template that is not plain YAML
            docs += [d for d in loaded if isinstance(d, dict) and d.get("kind")]
    return docs


def _job_pod_labels(doc: dict) -> dict:
    spec = doc.get("spec") or {}
    if doc["kind"] == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}


def test_no_job_pod_is_selected_by_a_service() -> None:
    docs = _documents()
    services = [d for d in docs if d["kind"] == "Service" and (d.get("spec") or {}).get("selector")]
    jobs = [d for d in docs if d["kind"] in ("Job", "CronJob")]
    assert services and jobs, "the manifests were not found"
    clashes = []
    for job in jobs:
        labels = _job_pod_labels(job)
        namespace = (job.get("metadata") or {}).get("namespace")
        for svc in services:
            if (svc.get("metadata") or {}).get("namespace") != namespace:
                continue
            selector = svc["spec"]["selector"]
            if all(labels.get(k) == v for k, v in selector.items()):
                clashes.append(f"{job['kind']} {job['metadata']['name']} is selected by Service {svc['metadata']['name']}")
    assert not clashes, "\n".join(clashes)
