"""Compute every dashboard section from the Kubernetes view and the latest
source inputs. Pure: the same view and inputs always give the same sections,
which is what the /data.json golden test checks.

`view` maps a cache name to its projected objects (see app.CACHES);
`inputs` holds the polled sources' latest results (missing keys mean "no data
yet").
"""
from __future__ import annotations

from dashboard import cost as dcost
from dashboard import models as m


def _first(view: dict, name: str) -> dict:
    items = view.get(name) or []
    return items[0] if items else {}


def compute(view: dict, inputs: dict, settings, yaml_load=None) -> tuple[dict, list[str]]:
    """(sections, notes). `notes` are data problems worth surfacing (for
    example a malformed price ConfigMap); they never stop the computation."""
    notes = []
    nodes, alerts = m.node_records(view.get("nodes", []))
    m.apply_usage(nodes, inputs.get("usage"))
    pods = m.pod_records(view.get("pods", []))
    endpoints = m.endpoint_records({mode: view.get(mode, []) for _p, mode in m.SERVING_KINDS})

    try:
        tables = dcost.merge_prices((_first(view, "cm-node-prices").get("data") or {}).get("prices.json"))
    except ValueError as e:
        tables = dcost.merge_prices(None)
        notes.append(f"cluster-dashboard-node-prices: invalid prices.json ({e})")
    region = dcost.detect_region(nodes, settings.aws_region)
    live = (inputs.get("live_prices") or {}).get(region, {})
    prices = dict(tables.get(region, {}), **live)
    cost = dcost.compute_cost(nodes, region, prices, bool(live))

    static = []
    if yaml_load is not None:
        raw = (_first(view, "cm-litellm-config").get("data") or {}).get("config.yaml", "")
        try:
            static = m.bedrock_from_config(yaml_load(raw) or {}) if raw else []
        except Exception as e:  # malformed YAML in litellm-config
            notes.append(f"litellm-config: cannot parse config.yaml ({type(e).__name__})")
    bedrock = m.merge_bedrock(static, m.bedrock_from_crs(view.get("bedrockmodels", [])))

    met = dict(m.EMPTY_LITELLM, **(inputs.get("litellm_api") or {}), perModel=inputs.get("litellm_traffic") or {})
    models = m.build_models(endpoints, bedrock, met, pods, nodes)
    dcgm = inputs.get("dcgm")
    gpu = m.gpu_records(nodes, pods, endpoints, dcgm) if dcgm is not None else []

    events = view.get("events-inference", [])
    provisioning = m.provisioning_warnings(m.events_of_kind(view.get("events-nodeclaim", []), "NodeClaim"))
    pods_by_name = {(p["metadata"].get("namespace"), p["metadata"]["name"]): p for p in view.get("pods", [])}
    logs = inputs.get("logs") or {}
    m.deploy_diagnostics(
        models, pods, m.events_of_kind(events, "Pod"), provisioning,
        pod_status=lambda name: (pods_by_name.get(("inference", name)) or {}).get("status"),
        log_tail=lambda name, _container: logs.get(name, ""))
    ext = m.cost_ext(models, met)

    hpas = view.get("hpas", [])
    ttft, itl = inputs.get("latency") or ({}, {})
    autoscaling = (m.autoscaling_status(endpoints, hpas, m.events_of_kind(events, "HorizontalPodAutoscaler"),
                                        ttft, itl) if hpas else [])
    m.attach_scale(models, autoscaling)

    lb = ((_first(view, "ingress").get("status") or {}).get("loadBalancer") or {}).get("ingress") or [{}]
    links = m.build_links(lb[0].get("hostname", "") or "", settings.open_webui_url, settings.litellm_url,
                          settings.langfuse_url, settings.argocd_url)
    available, pending = inputs.get("approvals") or (False, 0)

    return {
        "nodes": nodes, "pods": pods, "endpoints": endpoints, "nodeAlerts": alerts, "cost": cost,
        "models": models, "teams": met.get("teams", []), "gpu": gpu,
        "argocd": m.argocd_records(view.get("argocd", [])), "autoscaling": autoscaling,
        "costExt": ext, "provisioning": provisioning,
        "nodePools": m.nodepool_records(view.get("nodepools", [])),
        "nodeClaims": m.nodeclaim_records(view.get("nodeclaims", [])),
        "approvals": {"available": available, "pending": pending},
        "meta": {"cluster": settings.cluster_name, "region": cost.get("region", "") or settings.aws_region,
                 "links": links},
    }, notes
