"""Investigator and Remediator Job specs. These Jobs run with cluster read
(investigator) or scoped write (remediator) access, so their exact shape
(service account, security context, deadlines, mounts, env) is pinned here."""
from dash_testlib import check_golden

INV = "1234abcd-0000-0000-0000-000000000000"
PAYLOAD = {"trigger_kind": "CrashLoopBackOff", "kind": "Deployment",
           "namespace": "inference", "name": "qwen"}


def test_investigator_job(event_watcher):
    job = event_watcher.build_investigator_job(INV, PAYLOAD)
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "platform-health-agent-reader"
    assert job["spec"]["activeDeadlineSeconds"] == 600
    check_golden("investigator_job.json.golden", job)


def test_remediator_job(backend):
    job = backend.build_remediator_job(INV)
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "platform-health-agent-writer"
    assert job["metadata"]["name"] == "remediator-1234abcd"
    check_golden("remediator_job.json.golden", job)


def test_jobs_share_mounts_and_hardening(event_watcher, backend):
    inv = event_watcher.build_investigator_job(INV, PAYLOAD)["spec"]["template"]["spec"]
    rem = backend.build_remediator_job(INV)["spec"]["template"]["spec"]
    assert inv["volumes"] == rem["volumes"]
    assert inv["containers"][0]["volumeMounts"] == rem["containers"][0]["volumeMounts"]
    for c in inv["containers"] + rem["containers"]:
        sc = c["securityContext"]
        assert sc["runAsNonRoot"] and not sc["allowPrivilegeEscalation"]
