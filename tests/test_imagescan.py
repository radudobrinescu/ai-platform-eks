"""Image scan (harness/imagescan.py): which images, what counts, how it reports."""

import json
import subprocess

import pytest

import e2e
import imagescan


def _pod(ns, containers, statuses, init=()):
    return {"metadata": {"namespace": ns},
            "spec": {"containers": [{"name": n, "image": i} for n, i in containers],
                     "initContainers": [{"name": n, "image": i} for n, i in init]},
            "status": {"containerStatuses": [{"name": n, "imageID": d} for n, d in statuses]}}


D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


def test_running_images_scan_the_pulled_digest_once_per_image():
    pods = {"items": [
        _pod("argocd", [("redis", "ecr-public.aws.com/docker/library/redis:8.6.4-alpine")],
             [("redis", f"ecr-public.aws.com/docker/library/redis@{D1}")]),
        _pod("ai-platform", [("db", "postgres:16.15-alpine")], [("db", f"docker.io/library/postgres@{D2}")],
             init=[("init", "python:3.15-slim")]),
        _pod("inference", [("db", "postgres:16.15-alpine")], []),  # pending: no imageID yet
        _pod("kube-system", [("k", "public.ecr.aws/karpenter/controller:1.15.1@" + D1)], [("k", "")]),
    ]}
    got = imagescan.running_images(pods)
    assert got == [
        ("ecr-public.aws.com/docker/library/redis:8.6.4-alpine", f"ecr-public.aws.com/docker/library/redis@{D1}", ["argocd"]),
        ("postgres:16.15-alpine", f"postgres@{D2}", ["ai-platform", "inference"]),
        ("public.ecr.aws/karpenter/controller:1.15.1@" + D1, "public.ecr.aws/karpenter/controller:1.15.1@" + D1, ["kube-system"]),
        ("python:3.15-slim", "python:3.15-slim", ["ai-platform"]),  # init container, not started: scanned by tag
    ]


def test_repo_strips_tag_and_digest_but_keeps_a_registry_port():
    assert imagescan._repo("registry:5000/team/app:1.2") == "registry:5000/team/app"
    assert imagescan._repo("redis@" + D1) == "redis"


TRIVY_DOC = {"Metadata": {"OS": {"Family": "alpine", "Name": "3.23.5"}}, "Results": [
    {"Target": "redis (alpine 3.23.5)", "Vulnerabilities": [
        {"VulnerabilityID": "CVE-1", "PkgName": "musl", "InstalledVersion": "1.2.5-r23", "FixedVersion": "1.2.5-r24", "Severity": "HIGH"},
        {"VulnerabilityID": "CVE-1", "PkgName": "musl", "InstalledVersion": "1.2.5-r23", "FixedVersion": "1.2.5-r24", "Severity": "HIGH"},
        {"VulnerabilityID": "CVE-2", "PkgName": "zlib", "InstalledVersion": "1.3.2-r0", "FixedVersion": "1.3.2-r1", "Severity": "CRITICAL"},
        {"VulnerabilityID": "CVE-3", "PkgName": "busybox", "InstalledVersion": "1.37", "Severity": "CRITICAL"},  # no fix
        {"VulnerabilityID": "CVE-4", "PkgName": "x", "InstalledVersion": "1", "FixedVersion": "2", "Severity": "MEDIUM"}]},
    {"Target": "usr/local/bin/gosu", "Vulnerabilities": None}]}


def test_parse_trivy_keeps_fixable_high_and_critical_once_worst_first():
    os_name, vulns = imagescan.parse_trivy(TRIVY_DOC)
    assert os_name == "alpine 3.23.5"
    assert [(v["id"], v["severity"], v["pkg"]) for v in vulns] == [("CVE-2", "CRITICAL", "zlib"), ("CVE-1", "HIGH", "musl")]


def test_scan_one_runs_trivy_remote_by_ref_and_reports_failures():
    calls = []

    def fake(cmd, **k):
        calls.append(cmd)
        if cmd[-1] == "bad":
            return subprocess.CompletedProcess(cmd, 1, "", "FATAL\nunable to find the specified image \"bad\"\n")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(TRIVY_DOC), "")
    assert imagescan.scan_one("redis@" + D1, run=fake)[2] == ""
    assert "--ignore-unfixed" in calls[0] and calls[0][calls[0].index("--image-src") + 1] == "remote"
    assert "--skip-db-update" not in calls[0]
    _os, vulns, err = imagescan.scan_one("bad", skip_db_update=True, run=fake)
    assert vulns == [] and err == 'unable to find the specified image "bad"' and "--skip-db-update" in calls[1]


def _results():
    _os, vulns = imagescan.parse_trivy(TRIVY_DOC)
    return [imagescan.ImageResult("redis:8.6.4-alpine", "redis@" + D1, ["argocd"], "alpine 3.23.5", vulns),
            imagescan.ImageResult("redis:8.6.8-alpine", "redis@" + D2, ["argocd"], "alpine 3.23.8", []),
            imagescan.ImageResult("602401143452.dkr.ecr.eu-central-1.amazonaws.com/eks/coredns:v1", "x", ["kube-system"],
                                  error="401 Unauthorized")]


def test_scan_cluster_downloads_both_databases_first_then_never_updates(tmp_path):
    seen, dl = [], []
    pods = {"items": [_pod("a", [("x", "a:1")], []), _pod("b", [("y", "b:1")], [])]}
    imagescan.scan_cluster(pods=pods, cache_dir=str(tmp_path), workers=1, download=lambda t, d: dl.append(d),
                           scan=lambda ref, **k: (seen.append((ref, k["skip_db_update"])), ("", [], ""))[1])
    assert dl == [str(tmp_path)] and seen == [("a:1", True), ("b:1", True)]


def test_download_dbs_fetches_the_java_db_too_and_fails_loudly():
    calls = []

    def fake(cmd, **k):
        calls.append(cmd[3])
        return subprocess.CompletedProcess(cmd, 1 if cmd[3] == "--download-java-db-only" else 0, "", "rate limited")
    with pytest.raises(RuntimeError, match="download-java-db-only failed: rate limited"):
        imagescan.download_dbs("trivy", "/c", run=fake)
    assert calls == ["--download-db-only", "--download-java-db-only"]


def test_report_lists_the_worst_images_first_and_the_ones_not_scanned():
    md = "\n".join(imagescan.render(_results()))
    assert "2/3 images scanned: 1 critical and 1 high fixable vulnerabilities in 1 image(s); 1 not scanned." in md
    rows = [ln for ln in md.splitlines() if ln.startswith("| `")]
    assert rows[0].startswith("| `redis:8.6.4-alpine` | argocd | alpine 3.23.5 | 1 | 1 | CVE-2 zlib 1.3.2-r0 -> 1.3.2-r1; CVE-1 musl")
    assert "| none |" in rows[1] and "not scanned: 401 Unauthorized" in rows[2]


@pytest.mark.parametrize("fail_on, ok", [("CRITICAL", False), ("HIGH", False)])
def test_enforce_fails_on_the_chosen_severity(fail_on, ok):
    r = imagescan.enforce(_results(), fail_on)
    assert r.ok is ok and r.name == f"no-fixable-{fail_on.lower()}-cves" and "redis:8.6.4-alpine" in r.detail


def test_enforce_passes_when_only_lower_severities_remain():
    _os, vulns = imagescan.parse_trivy(TRIVY_DOC)
    only_high = [imagescan.ImageResult("a", "a", ["x"], "", [v for v in vulns if v["severity"] == "HIGH"])]
    assert imagescan.enforce(only_high, "CRITICAL").ok
    assert not imagescan.enforce(only_high, "HIGH").ok


def test_the_scan_is_report_only_and_a_broken_scanner_never_fails_the_run(tmp_path, monkeypatch):
    a = e2e.parse_args(["fresh", "main", "--dry-run", "--image-scan", "--report-dir", str(tmp_path)])
    assert a.image_scan and a.image_scan_fail_on is None and a.trivy == "trivy"
    rep = e2e.Report("fresh", "default", "e2efd1", {"subject": "main"})
    monkeypatch.setattr(e2e, "log", lambda *_: None)
    monkeypatch.setattr(imagescan, "log", lambda *_: None)
    monkeypatch.setattr(imagescan, "scan_cluster", lambda **k: _results())
    e2e.run_image_scan(a, rep)
    assert rep.results == [] and len(rep.image_scan) == 3
    assert json.loads((tmp_path / "e2efd1-image-scan.json").read_text())[0]["image"] == "redis:8.6.4-alpine"
    assert "### Image scan (Trivy, fixable HIGH/CRITICAL)" in e2e.render_markdown(rep)

    def boom(**k):
        raise FileNotFoundError("trivy")
    monkeypatch.setattr(imagescan, "scan_cluster", boom)
    rep2 = e2e.Report("fresh", "default", "e2efd1", {"subject": "main"})
    e2e.run_image_scan(a, rep2)
    assert rep2.results == [] and "Not run: FileNotFoundError: trivy" in e2e.render_markdown(rep2)


def test_fail_on_adds_a_security_check(tmp_path, monkeypatch):
    a = e2e.parse_args(["check", "--env", "dev", "--subject-dir", ".", "--image-scan",
                        "--image-scan-fail-on", "CRITICAL", "--report-dir", str(tmp_path)])
    monkeypatch.setattr(e2e, "log", lambda *_: None)
    monkeypatch.setattr(imagescan, "scan_cluster", lambda **k: _results())
    rep = e2e.Report("check", "default", "dev", {"env": "dev"})
    e2e.run_image_scan(a, rep)
    assert [(r.domain, r.ok) for r in rep.results] == [("security", False)]


def test_dry_run_plans_the_scan_after_the_checks(capsys):
    e2e.main(["fresh", "main", "--dry-run", "--image-scan"])
    out = capsys.readouterr().out
    assert out.index("- smoke suite") < out.index("- image scan: trivy image") < out.index("- teardown")


def test_parallel_scans_never_share_a_cache_and_update_the_database_once(tmp_path):
    import threading
    import time
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "trivy.db").write_text("x")
    busy, lock, calls, clash = set(), threading.Lock(), [], []

    def scan(ref, **k):
        with lock:
            if k["cache_dir"] in busy:
                clash.append(ref)
            busy.add(k["cache_dir"])
            calls.append((ref, k["skip_db_update"]))
        time.sleep(0.01)
        with lock:
            busy.discard(k["cache_dir"])
        return "", [], ""
    pods = {"items": [_pod("ns", [(f"c{i}", f"img{i}:1")], []) for i in range(10)]}
    (tmp_path / "java-db").mkdir()
    out = imagescan.scan_cluster(pods=pods, scan=scan, cache_dir=str(tmp_path), workers=3, download=lambda t, d: None)
    assert len(out) == 10 and not clash and all(skip for _r, skip in calls)
    assert (tmp_path / "w1" / "java-db").is_dir()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["db", "java-db", "w0", "w1", "w2"]
    assert (tmp_path / "w2" / "db" / "trivy.db").read_text() == "x"


def test_trivy_errors_drop_the_log_prefix():
    def fake(cmd, **k):
        return subprocess.CompletedProcess(cmd, 1, "", "2026-11-16T17:07:16+02:00\tFATAL\tFatal error\trun error: image scan error\n")
    assert imagescan.scan_one("x", run=fake)[2] == "run error: image scan error"


def test_the_report_redacts_account_ids(monkeypatch):
    rep = e2e.Report("check", "default", "dev", {"env": "dev"})
    rep.image_scan = [imagescan.ImageResult("123456789012.dkr.ecr.eu-central-1.amazonaws.com/app:1", "x", ["ns"])]
    assert "123456789012" not in e2e.render_markdown(rep)
