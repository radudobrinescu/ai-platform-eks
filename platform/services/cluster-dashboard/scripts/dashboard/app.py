"""Wiring: watch caches, polled sources, the derive loop and health.

Data flow: watch caches and polled sources update their own state and set
`dirty`; the derive loop recomputes every section at most once per second
(and at least every 5 s, to refresh source status) and writes changed
sections to the store; the API serves the store.
"""
from __future__ import annotations

import functools
import logging
import threading
import time

from dashboard import dcgm as ddcgm
from dashboard import derive, litellm, prom
from dashboard.approvals import Approvals
from dashboard.config import Settings
from dashboard.deps import Deps
from dashboard.k8s import (ApiError, KubeClient, WatchCache, project_argo, project_configmap, project_cr,
                           project_event, project_ingress, project_node, project_pod)
from dashboard.models import SERVING_KINDS
from dashboard.snapshot import Store
from dashboard.sources import PolledSource, Scheduler, SourceStatus

log = logging.getLogger("dashboard.app")

KUBE_STALE_AFTER = 360       # s; a quiet watch can go 300 s without an event
KUBE_LIVENESS_LIMIT = 600    # s of stale or failing Kubernetes data before liveness fails
EVENT_RETENTION = 3600       # s
EVENT_MAX = 500
DERIVE_MIN_GAP = 1.0         # s between recomputes
DERIVE_MAX_GAP = 5.0         # s; recompute anyway (source status ages)
LOG_TAILS_PER_RUN = 3
LATENCY_Q = "histogram_quantile(0.95, sum by (le,pool,role) (rate({}_bucket[5m])))"


def cache_specs(s: Settings) -> list[tuple]:
    """(name, path, projection, query, retention, max_items) for every watch."""
    label_keys = tuple(dict.fromkeys(("app", s.dcgm_pod_selector.split("=", 1)[0],
                                      s.litellm_pod_selector.split("=", 1)[0])))
    specs = [
        ("nodes", "/api/v1/nodes", project_node, None, None, None),
        ("pods", "/api/v1/pods", functools.partial(project_pod, label_keys=label_keys), None, None, None),
        ("bedrockmodels", "/apis/bedrock.ai-platform/v1alpha1/bedrockmodels", project_cr, None, None, None),
        ("hpas", "/apis/autoscaling/v2/namespaces/inference/horizontalpodautoscalers", project_cr, None, None, None),
        ("argocd", "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications", project_argo, None, None, None),
        ("ingress", f"/apis/networking.k8s.io/v1/namespaces/{s.links_ingress_namespace}/ingresses", project_ingress,
         {"fieldSelector": f"metadata.name={s.links_ingress_name}"}, None, None),
        ("cm-litellm-config", f"/api/v1/namespaces/{s.litellm_namespace}/configmaps", project_configmap,
         {"fieldSelector": f"metadata.name={s.litellm_config_cm}"}, None, None),
        ("cm-node-prices", f"/api/v1/namespaces/{s.litellm_namespace}/configmaps", project_configmap,
         {"fieldSelector": f"metadata.name={s.node_prices_cm}"}, None, None),
        ("events-nodeclaim", "/api/v1/events", project_event,
         {"fieldSelector": "involvedObject.kind=NodeClaim"}, EVENT_RETENTION, EVENT_MAX),
        ("events-inference", "/api/v1/namespaces/inference/events", project_event, None, EVENT_RETENTION, EVENT_MAX),
    ]
    for plural, mode in SERVING_KINDS:
        specs.append((mode, f"/apis/kro.run/v1alpha1/namespaces/inference/{plural}", project_cr, None, None, None))
    return specs


class App:
    def __init__(self, settings: Settings, deps: Deps, *, kube: KubeClient | None = None, kube_error: str | None = None,
                 remediator_job=None, yaml_load=None, boto3_client=None, clock=time.time):
        self.s, self.deps, self.kube, self._clock = settings, deps, kube, clock
        # Why there is no Kubernetes client (package missing, or no in-cluster config).
        self.kube_error = kube_error or ("dependency missing: kubernetes" if kube is None else None)
        self.started = clock()
        self.store = Store(clock)
        self.dirty = threading.Event()
        self.stop = threading.Event()
        self._inputs: dict = {}
        self._inputs_lock = threading.Lock()
        self._yaml_load = yaml_load
        self._derive_tick = clock()
        self.caches = {name: WatchCache(name, path, proj, query=q, on_change=lambda _k: self.dirty.set(),
                                        retention_s=ret, max_items=mx, clock=clock)
                       for name, path, proj, q, ret, mx in cache_specs(settings)}
        connect_agent = self._connector(settings.db_name) if deps.has("psycopg") and settings.db_configured else None
        self.approvals = Approvals(settings, connect_agent, self._create_job, remediator_job or (lambda i: {}), clock)
        # LiteLLM through its API and per-pod metrics with the read-only key.
        self.viewer_key = (litellm.ViewerKey(kube.get, settings.litellm_namespace, settings.litellm_key_secret,
                                             clock) if kube is not None else None)
        self.litellm_get = litellm.http_get          # replaced in tests
        self.litellm_api = litellm.LiteLLMApi(settings.litellm_api_url, self.viewer_key,
                                              lambda url, key, where: self.litellm_get(url, key, where))
        self.traffic = litellm.Traffic()
        self._live_prices = None
        if boto3_client is not None:
            from dashboard.pricing import LivePrices
            self._live_prices = LivePrices(boto3_client)
        self.sources = self._polled_sources()
        self.scheduler = Scheduler(self.sources, clock)

    # -- inputs ------------------------------------------------------------------
    def set_input(self, key: str, value) -> None:
        with self._inputs_lock:
            self._inputs[key] = value
        self.dirty.set()

    def inputs(self) -> dict:
        with self._inputs_lock:
            return dict(self._inputs)

    def view(self) -> dict:
        return {name: c.items() for name, c in self.caches.items()}

    # -- polled sources -----------------------------------------------------------
    def _connector(self, dbname: str):
        from pha import db as pha_db
        s = self.s
        return lambda: pha_db.connect(host=s.db_host, port=s.db_port, dbname=dbname, user=s.db_user,
                                      password=s.db_password, connect_timeout=5)

    def _create_job(self, spec: dict):
        if self.kube is None:
            raise ApiError(503, "kubernetes client unavailable")
        return self.kube.post(f"/apis/batch/v1/namespaces/{self.s.agent_namespace}/jobs", spec)

    def _polled_sources(self) -> list[PolledSource]:
        c = self._clock
        no_kube = self.kube_error
        no_db = (None if self.deps.has("psycopg") and self.s.db_configured
                 else "dependency missing: psycopg" if not self.deps.has("psycopg")
                 else "database credentials not configured")
        defs = [
            ("k8s-usage", 30, self._run_usage, None, no_kube),
            ("litellm-api", 30, self._run_litellm_api, 90, no_kube),
            ("litellm-metrics", 15, self._run_litellm_metrics, 45, no_kube),
            ("dcgm", 15, self._run_dcgm, None, no_kube),
            ("autoscaling", 15, self._run_latency, None, None),
            ("pricing", 60, self._run_pricing, 3 * 3600, None),
            ("approvals", 10, self._run_approvals, None, no_db),
            ("diagnostics", 15, self._run_log_tails, None, no_kube),
        ]
        out = []
        for name, interval, fn, stale, disabled in defs:
            src = PolledSource(name, interval, fn, stale, c)
            if disabled:
                src.tracker.disable(disabled)
            out.append(src)
        return out

    def _run_usage(self):
        try:
            self.set_input("usage", self.kube.get("/apis/metrics.k8s.io/v1beta1/nodes").get("items", []))
        except ApiError as e:
            if e.status != 404:
                raise
            self.set_input("usage", None)
            return "metrics-server not installed"
        return None

    def _run_litellm_api(self):
        data, detail = litellm.fetch_api(self.litellm_api, self._clock(), self.inputs().get("litellm_api"))
        self.set_input("litellm_api", data)
        return detail

    def _run_litellm_metrics(self):
        pods = litellm.litellm_pods(self.caches["pods"].items(), self.s.litellm_namespace,
                                    self.s.litellm_pod_selector)
        figures, detail = litellm.fetch_metrics(self.traffic, pods, self.viewer_key, self.s.litellm_metrics_port,
                                                self._clock(), self.litellm_get)
        self.set_input("litellm_traffic", figures)
        return detail

    def _run_dcgm(self):
        key, _, val = self.s.dcgm_pod_selector.partition("=")
        urls = [f"http://{p['status']['podIP']}:{self.s.dcgm_port}/metrics"
                for p in self.caches["pods"].items()
                if p["metadata"].get("namespace") == self.s.dcgm_namespace
                and (p["metadata"].get("labels") or {}).get(key) == val
                and p["status"].get("phase") == "Running" and p["status"].get("podIP")]
        util, errors = ddcgm.scrape(urls, self.s.dcgm_url)
        if errors and not util:
            raise RuntimeError("; ".join(errors)[:300])
        self.set_input("dcgm", util)
        return ("some exporters failed: " + "; ".join(errors))[:300] if errors else None

    def _run_latency(self):
        from dashboard.models import quantile_map
        ttft = quantile_map(prom.query(self.s.autoscaling_prom_url, LATENCY_Q.format("vllm:time_to_first_token_seconds")))
        itl = quantile_map(prom.query(self.s.autoscaling_prom_url, LATENCY_Q.format("vllm:inter_token_latency_seconds")))
        self.set_input("latency", (ttft, itl))

    def _run_pricing(self):
        if self._live_prices is None:
            return "static price table"
        from dashboard.cost import detect_region, priced_instances
        from dashboard.models import node_records
        nodes, _ = node_records(self.caches["nodes"].items())
        region = detect_region(nodes, self.s.aws_region)
        failed = self._live_prices.refresh(region, priced_instances(nodes))
        self.set_input("live_prices", {region: self._live_prices.get(region)})
        return f"no live price for: {', '.join(failed)}" if failed else None

    def _run_approvals(self):
        self.set_input("approvals", (True, self.approvals.pending_count()))

    def _run_log_tails(self):
        """Log tails for serving pods that are Running but not Ready (model
        still loading), at most LOG_TAILS_PER_RUN per run."""
        not_running = {ep["metadata"]["name"] for _p, mode in SERVING_KINDS for ep in self.caches[mode].items()
                       if str((ep.get("status") or {}).get("ready", "False")) != "True"}
        logs, fetched = {}, 0
        for p in self.caches["pods"].items():
            meta, st = p["metadata"], p["status"]
            if meta.get("namespace") != "inference" or st.get("phase") != "Running":
                continue
            if not any(meta["name"].startswith(n + "-") for n in not_running) or "-register" in meta["name"]:
                continue
            cs = st.get("containerStatuses") or []
            if not cs or any(c.get("ready") for c in cs) or fetched >= LOG_TAILS_PER_RUN:
                continue
            logs[meta["name"]] = self.kube.get_text(f"/api/v1/namespaces/inference/pods/{meta['name']}/log",
                                                    {"tailLines": "14", "container": cs[0]["name"]})
            fetched += 1
        self.set_input("logs", logs)

    # -- status and health -----------------------------------------------------------
    def kube_status(self) -> SourceStatus:
        if self.kube is None:
            return SourceStatus("kubernetes", "disabled", None, None, self.kube_error, 0, KUBE_STALE_AFTER)
        now = self._clock()
        errors = [c.error for c in self.caches.values() if c.error]
        stale = [n for n, c in self.caches.items()
                 if c.last_success is None or now - c.last_success > KUBE_STALE_AFTER]
        last = [c.last_success for c in self.caches.values() if c.last_success]
        oldest = min(last) if len(last) == len(self.caches) else None
        state = "error" if errors else ("stale" if stale else "ok")
        detail = ("not installed: " + ", ".join(n for n, c in self.caches.items() if not c.installed)
                  if any(not c.installed for c in self.caches.values()) else None)
        if stale and not errors:
            detail = "no recent data: " + ", ".join(stale)
        return SourceStatus("kubernetes", state, oldest, "; ".join(errors)[:300] or None, detail, 0,
                            KUBE_STALE_AFTER)

    def statuses(self) -> list[SourceStatus]:
        return [self.kube_status()] + [s.tracker.status() for s in self.sources]

    def health(self) -> tuple[bool, bool, dict]:
        now = self._clock()
        reasons = [f"source loop stopped: {n}" for n in self.scheduler.dead_loops()]
        if now - self._derive_tick > 60:
            reasons.append("derive loop stopped")
        if self.kube is not None:
            # Only when NO watch has succeeded for the limit (API unreachable or
            # a wedged client): a restart can fix that. One kind failing (for
            # example RBAC denied) shows in the UI instead, it would not.
            newest = max((c.last_success or 0 for c in self.caches.values()), default=0) or self.started
            if now - newest > KUBE_LIVENESS_LIMIT:
                reasons.append(f"no Kubernetes data for {int(now - newest)} s")
        # Ready once every watch has listed or reported why it cannot, so one
        # denied resource never keeps the dashboard out of its Service.
        ready = self.kube is None or all(c.synced or c.error for c in self.caches.values())
        body = {"live": not reasons, "ready": ready, "reasons": reasons, "degraded": self.deps.degraded(),
                "sources": [st.to_dict() for st in self.statuses()]}
        if not ready:
            body["reasons"] = reasons + ["waiting for the first Kubernetes list"]
        return not reasons, ready, body

    # -- derive -----------------------------------------------------------------------
    def derive_once(self) -> None:
        self._derive_tick = self._clock()
        sections, notes = derive.compute(self.view(), self.inputs(), self.s, self._yaml_load)
        statuses = self.statuses()
        by = {st.name: st.state for st in statuses}
        kube_ok = by["kubernetes"] in ("ok", "stale")
        sections["status"] = {
            "sources": [st.to_dict() for st in statuses],
            "degraded": self.deps.degraded(),
            "notes": notes,
            # Pre-rebuild `sources` booleans, kept for /data.json readers.
            "legacy": {"litellm": by["litellm-api"] in ("ok", "stale"), "bedrock": kube_ok,
                       "dcgm": by["dcgm"] in ("ok", "stale"),
                       "argocd": kube_ok and self.caches["argocd"].installed,
                       "autoscaling": by["autoscaling"] in ("ok", "stale")},
        }
        for name, data in sections.items():
            self.store.put(name, data)

    def derive_loop(self) -> None:
        last = 0.0
        while not self.stop.is_set():
            self.dirty.wait(DERIVE_MAX_GAP)
            gap = self._clock() - last
            if gap < DERIVE_MIN_GAP:
                self.stop.wait(DERIVE_MIN_GAP - gap)
            self.dirty.clear()
            last = self._clock()
            try:
                self.derive_once()
            except Exception:  # a bug in derivation must not stop the loop; log it with the stack
                log.exception("derive failed")

    def start(self) -> None:
        if self.kube is not None:
            for c in self.caches.values():
                threading.Thread(target=c.run, args=(self.kube, self.stop), daemon=True,
                                 name=f"watch-{c.kind}").start()
        self.scheduler.start()
        threading.Thread(target=self.derive_loop, daemon=True, name="derive").start()
