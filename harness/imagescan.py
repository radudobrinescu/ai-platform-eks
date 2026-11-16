"""Vulnerability scan of every image running in the environment (Trivy).

Renovate sees version pins, not what an image contains. A chart can pin an image
tag that is never rebuilt (argo-cd's redis:8.6.4-alpine), or a whole chart line
can end with frozen images (Langfuse 1.x with bitnamilegacy subcharts); both
kept known CVEs on dev with no update to propose. This scans what the cluster
actually runs, by digest, after the smoke checks, and lists the HIGH and
CRITICAL vulnerabilities that have a fix, i.e. the ones an image update clears.

Report-only by default: the scan is a section of the report and never fails the
run. With `fail_on` set, a check `security/no-fixable-<severity>-cves` fails
when any image has one of those (for later, once a few runs are known).

Images that only run during a check (the vLLM test model) are gone when the
scan runs and are not scanned; neither are images pulled from a registry the
runner cannot read (reported as "not scanned").
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from checks import CheckResult
from common import kubectl_json, log, redact

SEVERITIES = ("CRITICAL", "HIGH")
TRIVY_TIMEOUT = "10m"
SHOW_PER_IMAGE = 5
# Parallel scans: the large images (Open WebUI, LiteLLM, Langfuse) take minutes
# each to download; 4 at a time keeps the scan to a few minutes on a runner.
WORKERS = 4


@dataclass
class ImageResult:
    image: str          # what the pod spec says (tag)
    ref: str            # what was scanned (repo@digest when known)
    namespaces: list[str] = field(default_factory=list)
    os: str = ""
    vulns: list[dict] = field(default_factory=list)   # fixable HIGH/CRITICAL, one per (id, package)
    error: str = ""

    def count(self, sev: str) -> int:
        return sum(1 for v in self.vulns if v["severity"] == sev)


def _repo(image: str) -> str:
    """Repository part of an image reference (no tag, no digest)."""
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name[: len(name) - len(last)] + last.split(":", 1)[0]


def running_images(pods: dict) -> list[tuple[str, str, list[str]]]:
    """(image, ref to scan, namespaces) for every distinct image in a `kubectl get pods -A`
    result. The ref pins the digest the node actually pulled (status.imageID), so the scan
    reads the running content even when a tag has been re-pushed since."""
    seen: dict[str, tuple[str, set[str]]] = {}
    for p in pods.get("items", []):
        ns = p["metadata"]["namespace"]
        st = p.get("status", {})
        ids = {s.get("name"): s.get("imageID", "") for s in
               st.get("containerStatuses", []) + st.get("initContainerStatuses", [])}
        for c in p["spec"].get("containers", []) + p["spec"].get("initContainers", []):
            image = c["image"]
            iid = ids.get(c["name"], "")
            digest = iid.rsplit("@", 1)[1] if "@sha256:" in iid else ""
            ref = f"{_repo(image)}@{digest}" if digest else image
            prev = seen.get(image)
            if prev is None or ("@sha256:" not in prev[0] and digest):
                seen[image] = (ref, (prev[1] if prev else set()) | {ns})
            else:
                prev[1].add(ns)
    return [(img, ref, sorted(nss)) for img, (ref, nss) in sorted(seen.items())]


def parse_trivy(doc: dict) -> tuple[str, list[dict]]:
    """OS and the fixable vulnerabilities of one `trivy image --format json` result."""
    meta_os = (doc.get("Metadata") or {}).get("OS") or {}
    os_name = f"{meta_os.get('Family', '')} {meta_os.get('Name', '')}".strip()
    found: dict[tuple[str, str], dict] = {}
    for r in doc.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            if v.get("Severity") not in SEVERITIES or not v.get("FixedVersion"):
                continue
            key = (v["VulnerabilityID"], v["PkgName"])
            found.setdefault(key, {"id": v["VulnerabilityID"], "severity": v["Severity"], "pkg": v["PkgName"],
                                   "installed": v.get("InstalledVersion", ""), "fixed": v["FixedVersion"],
                                   "target": r.get("Target", "")})
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return os_name, sorted(found.values(), key=lambda v: (order[v["severity"]], v["pkg"], v["id"]))


def scan_one(ref: str, trivy: str = "trivy", cache_dir: str | None = None, skip_db_update: bool = False,
             run=subprocess.run) -> tuple[str, list[dict], str]:
    """(os, vulns, error) for one image."""
    cmd = [trivy, "image", "--quiet", "--scanners", "vuln", "--severity", ",".join(SEVERITIES),
           "--ignore-unfixed", "--image-src", "remote", "--format", "json", "--timeout", TRIVY_TIMEOUT]
    if cache_dir:
        cmd += ["--cache-dir", cache_dir]
    if skip_db_update:
        cmd += ["--skip-db-update", "--skip-java-db-update"]
    try:
        r = run(cmd + [ref], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return "", [], "trivy timed out"
    if r.returncode != 0:
        lines = [x for x in (r.stderr or r.stdout).strip().splitlines() if x.strip()]
        # "2026-11-16T17:07:16+02:00\tFATAL\tFatal error\trun error: ..." -> "run error: ..."
        last = re.sub(r"^\S+\t(FATAL|ERROR)\t(Fatal error\t)?", "", lines[-1]) if lines else f"trivy exit {r.returncode}"
        return "", [], last[-300:]
    try:
        os_name, vulns = parse_trivy(json.loads(r.stdout))
    except (ValueError, KeyError) as e:
        return "", [], f"unreadable trivy output: {e}"[:200]
    return os_name, vulns, ""


def download_dbs(trivy: str, cache_dir: str, run=subprocess.run) -> None:
    """Fetch the vulnerability database and the Java database once, before any scan:
    a scan with --skip-java-db-update fails on the first image with a .jar in it when
    the Java DB is not there yet ("cannot be specified on the first run")."""
    for flag in ("--download-db-only", "--download-java-db-only"):
        r = run([trivy, "image", "--quiet", flag, "--cache-dir", cache_dir], capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"trivy {flag} failed: {(r.stderr or r.stdout).strip()[-300:]}")


def _worker_caches(base: str, n: int) -> list[str]:
    """One cache directory per worker (Trivy locks its cache), each with a copy of the
    vulnerability databases the first scan downloaded into `base`."""
    dirs = []
    for i in range(n):
        d = os.path.join(base, f"w{i}")
        for db in ("db", "java-db"):
            src = os.path.join(base, db)
            if os.path.isdir(src) and not os.path.isdir(os.path.join(d, db)):
                shutil.copytree(src, os.path.join(d, db))
        dirs.append(d)
    return dirs


def scan_cluster(trivy: str = "trivy", cache_dir: str | None = None, pods: dict | None = None,
                 scan=scan_one, workers: int = WORKERS, download=download_dbs) -> list[ImageResult]:
    pods = pods if pods is not None else kubectl_json("get", "pods", "-A")
    images = running_images(pods)
    log(f"image scan: {len(images)} images, {workers} at a time")
    base = cache_dir or tempfile.mkdtemp(prefix="trivy-cache-")

    def one(item, cache, skip_db):
        image, ref, nss = item
        os_name, vulns, err = scan(ref, trivy=trivy, cache_dir=cache, skip_db_update=skip_db)
        res = ImageResult(image, ref, nss, os_name, vulns, err)
        log(f"  {image}: " + (f"not scanned ({err})" if err else
                              f"{res.count('CRITICAL')} critical, {res.count('HIGH')} high (fixable)"))
        return res

    if not images:
        return []
    download(trivy, base)
    free: queue.Queue[str] = queue.Queue()
    for d in _worker_caches(base, min(workers, len(images))):
        free.put(d)

    def with_cache(item):
        d = free.get()  # a cache no other running scan holds
        try:
            return one(item, d, True)
        finally:
            free.put(d)
    with ThreadPoolExecutor(max_workers=free.qsize()) as pool:
        return list(pool.map(with_cache, images))


def summary(results: list[ImageResult]) -> str:
    scanned = [r for r in results if not r.error]
    crit = sum(r.count("CRITICAL") for r in scanned)
    high = sum(r.count("HIGH") for r in scanned)
    affected = sum(1 for r in scanned if r.vulns)
    s = (f"{len(scanned)}/{len(results)} images scanned: {crit} critical and {high} high fixable "
         f"vulnerabilities in {affected} image(s)")
    return s + (f"; {len(results) - len(scanned)} not scanned" if len(scanned) < len(results) else "")


def render(results: list[ImageResult]) -> list[str]:
    """Markdown lines for the report."""
    lines = ["### Image scan (Trivy, fixable HIGH/CRITICAL)", "", summary(results) + ".", "",
             "| Image | Namespace | OS | Critical | High | Examples (package installed -> fixed) |",
             "|---|---|---|---|---|---|"]
    order = sorted(results, key=lambda r: (bool(r.error), -r.count("CRITICAL"), -r.count("HIGH"), r.image))
    for r in order:
        if r.error:
            lines.append(f"| `{r.image}` | {', '.join(r.namespaces)} | - | - | - | not scanned: "
                         f"{r.error.replace('|', '/')} |")
            continue
        seen, ex = set(), []
        for v in r.vulns:  # the worst first: one example per package
            if v["pkg"] not in seen:
                seen.add(v["pkg"])
                ex.append(f"{v['id']} {v['pkg']} {v['installed']} -> {v['fixed']}")
        text = "; ".join(ex[:SHOW_PER_IMAGE]) if ex else "none"
        if len(ex) > SHOW_PER_IMAGE:
            text += f" (+{len(ex) - SHOW_PER_IMAGE} more packages)"
        lines.append(f"| `{r.image}` | {', '.join(r.namespaces)} | {r.os or '-'} | {r.count('CRITICAL')} | "
                     f"{r.count('HIGH')} | {text} |")
    return lines + [""]


def to_json(results: list[ImageResult]) -> str:
    return json.dumps([r.__dict__ for r in results], indent=2)


def enforce(results: list[ImageResult], fail_on: str) -> CheckResult:
    """A failing check when any scanned image has a fixable vulnerability at or above fail_on."""
    sevs = SEVERITIES[: SEVERITIES.index(fail_on) + 1]
    bad = [f"{r.image} ({', '.join(f'{r.count(s)} {s.lower()}' for s in sevs if r.count(s))})"
           for r in results if not r.error and any(r.count(s) for s in sevs)]
    name = f"no-fixable-{fail_on.lower()}-cves"
    if bad:
        return CheckResult("security", name, False, "update these images: " + "; ".join(bad))
    return CheckResult("security", name, True, summary(results))


def write(results: list[ImageResult], report_dir: str, env: str) -> None:
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, f"{env}-image-scan.json"), "w", encoding="utf-8") as fh:
        fh.write(redact(to_json(results)))
