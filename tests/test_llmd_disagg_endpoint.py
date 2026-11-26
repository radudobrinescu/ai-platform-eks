"""LLMDDisaggEndpoint (prefill/decode pools) next to the other serving tiers.

What has to hold, without a cluster:
  * its status reports availableReplicas like the other two tiers (the health
    agent reads that field for every endpoint kind);
  * the status fields it already had are unchanged (kro refuses a breaking
    schema change in place; adding a field is not one);
  * scale to zero stays an LLMDEndpoint feature: both pools keep a minimum of 1.
"""

import os

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KRO = os.path.join(REPO_ROOT, "platform/config/kro")
PREFILL_AVAILABLE = "prefilldeployment.status.?availableReplicas.orValue(0)"
DECODE_AVAILABLE = "decodedeployment.status.?availableReplicas.orValue(0)"


def _rgd(name: str) -> dict:
    with open(os.path.join(KRO, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def disagg() -> dict:
    return _rgd("llmd-disagg-endpoint.yaml")


class TestStatus:
    @pytest.mark.parametrize("name", ["vllm-endpoint.yaml", "llmd-endpoint.yaml", "llmd-disagg-endpoint.yaml"])
    def test_every_tier_reports_available_replicas(self, name):
        assert "availableReplicas" in _rgd(name)["spec"]["schema"]["status"]

    def test_available_replicas_counts_both_pools(self, disagg):
        # the dashboard counts a disaggregated model's replicas the same way
        assert disagg["spec"]["schema"]["status"]["availableReplicas"] == \
            "${" + PREFILL_AVAILABLE + " + " + DECODE_AVAILABLE + "}"

    def test_existing_status_fields_are_unchanged(self, disagg):
        status = disagg["spec"]["schema"]["status"]
        assert status["ready"] == "${" + DECODE_AVAILABLE + " > 0 ? 'True' : 'False'}"
        assert status["prefillReplicas"] == "${" + PREFILL_AVAILABLE + "}"
        assert status["decodeReplicas"] == "${" + DECODE_AVAILABLE + "}"
        assert status["routerHealth"] == "${routerapp.status.?health.?status.orValue('Unknown')}"


class TestNoScaleToZero:
    @pytest.mark.parametrize("field", ["prefillMinReplicas", "decodeMinReplicas"])
    def test_pool_minimum_stays_one(self, disagg, field):
        assert disagg["spec"]["schema"]["spec"][field].replace(" ", "") == "integer|default=1minimum=1"
