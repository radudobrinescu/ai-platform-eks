"""`platformctl new-model` end to end, offline.

`cli.main` runs with the Hugging Face model config and the EC2 prices stubbed,
so these tests see what a user sees: the printed report and the manifest that
`--deploy` would commit.
"""

import re

import pytest
import yaml

from recommend_instance import catalog, cli
from recommend_instance.model import ModelSpec

STATIC_PRICES = {i.name: i.price_usd_h for i in catalog.INSTANCES}


def _model(params_b: float = 8.0, num_heads: int = 32) -> ModelSpec:
    return ModelSpec(model_id="org/m", params=int(params_b * 1e9), num_layers=32, hidden_size=4096,
                     num_heads=num_heads, num_kv_heads=8, head_dim=128, vocab_size=128256,
                     max_position=131072, architecture="LlamaForCausalLM")


@pytest.fixture
def new_model(monkeypatch, capsys):
    """Run `new-model` with a stubbed model and price table. Returns
    (exit code, stdout, stderr); a `sys.exit("error: ...")` is reported as
    exit code 1 with the message in stderr, as the shell would see it."""
    monkeypatch.setenv("NO_COLOR", "1")

    def run(argv, model=None, prices=None, price_src="static catalog (us-east-1)"):
        monkeypatch.setattr(cli, "fetch_model", lambda _id, _token: model or _model())
        monkeypatch.setattr(cli, "detect_region", lambda explicit: explicit or "us-east-1")
        monkeypatch.setattr(cli, "resolve_prices",
                            lambda _region, _refresh, _verbose: (dict(prices or STATIC_PRICES), price_src))
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            out, err = capsys.readouterr()
            if isinstance(e.code, int):
                return e.code, out, err
            return 1, out, err + str(e.code)
        out, err = capsys.readouterr()
        return code, out, err

    return run


def _manifest(out: str) -> dict:
    """The manifest in the "You'll deploy this" box."""
    box = [ln.split("│ ", 1)[1] for ln in out.splitlines() if "│ " in ln]
    assert box, "no manifest in the output"
    return yaml.safe_load("\n".join(box))


def _fleet_headline(out: str) -> tuple[int, str]:
    m = re.search(r"RECOMMENDED FLEET[^:]*: (\d+)× (\S+)", out)
    assert m, "no fleet headline in the output"
    return int(m.group(1)), m.group(2)


class TestPinnedInstanceTypeFleet:
    """--instance-type pins the fleet too: the replica count is the one the
    pinned type needs, and the headline and the manifest say the same."""

    ARGV = ["org/m", "--tier", "vllm", "--instance-type", "g6.2xlarge", "--users", "200"]

    def test_fleet_is_sized_on_the_pinned_type(self, new_model):
        code, out, _err = new_model(self.ARGV)
        assert code == 0
        replicas, instance = _fleet_headline(out)
        assert instance == "g6.2xlarge"
        assert replicas == 10   # 20 users per L4 at 70% utilisation -> 200 users

    def test_manifest_matches_the_headline(self, new_model):
        _code, out, _err = new_model(self.ARGV)
        replicas, instance = _fleet_headline(out)
        spec = _manifest(out)["spec"]
        assert spec["instanceType"] == instance == "g6.2xlarge"
        assert spec["replicas"] == replicas
        assert spec["gpuCount"] == 1

    def test_no_other_instance_type_is_selected(self, new_model):
        _code, out, _err = new_model(self.ARGV)
        assert "Selected: 10× g6.2xlarge" in out
        assert "g7e.2xlarge" not in out.split("Cost levers")[0]

    def test_json_agrees(self, new_model):
        import json
        _code, out, _err = new_model(self.ARGV + ["--json"])
        doc = json.loads(out)
        assert doc["recommended"]["instance"] == "g6.2xlarge"
        assert doc["scaling"]["recommended"]["instance"] == "g6.2xlarge"
        assert doc["scaling"]["recommended"]["replicas"] == 10
        assert [a["instance"] for a in doc["scaling"]["alternatives"]] == ["g6.2xlarge"]

    def test_without_a_pin_the_cheapest_fleet_still_wins(self, new_model):
        _code, out, _err = new_model(["org/m", "--tier", "vllm", "--users", "200"])
        replicas, instance = _fleet_headline(out)
        assert (replicas, instance) == (1, "g7e.2xlarge")
        spec = _manifest(out)["spec"]
        assert spec["replicas"] == 1 and "instanceType" not in spec


class TestTypesTheNodePoolsRefuse:
    """g4dn (family list) and p4d/p4de (generation floor) are in the catalog
    but the GPU NodePools never launch them: a pod pinned there stays Pending."""

    SMALL_FP16 = ["org/m", "--quant", "fp16"]   # fp16 runs natively on a T4, the cheapest fit

    def test_the_cheapest_fit_is_not_recommended_when_karpenter_cannot_launch_it(self, new_model):
        code, out, _err = new_model(self.SMALL_FP16, model=_model(3.0))
        assert code == 0
        assert "RECOMMENDED: g6.xlarge" in out
        assert "instanceType" not in _manifest(out)["spec"]

    def test_the_table_marks_it_out_of_cluster(self, new_model):
        _code, out, _err = new_model(self.SMALL_FP16 + ["--verbose", "--limit", "40"], model=_model(3.0))
        rows = [ln for ln in out.splitlines() if re.match(r"\s+(g4dn|p4de?)\.", ln)]
        assert rows and all("out-of-cluster" in ln for ln in rows), rows
        assert not [ln for ln in out.splitlines() if re.match(r"\s+g6\.xlarge ", ln) and "out-of-cluster" in ln]

    def test_in_cluster_only_hides_it(self, new_model):
        _code, out, _err = new_model(self.SMALL_FP16 + ["--in-cluster-only", "--verbose", "--limit", "40"],
                                     model=_model(3.0))
        assert "RECOMMENDED: g6.xlarge" in out
        assert not re.search(r"\b(g4dn|p4de?)\.", out)

    def test_the_fleet_table_marks_it_out_of_cluster(self, new_model):
        _code, out, _err = new_model(["org/m", "--users", "200", "--limit", "40"])
        rows = [ln for ln in out.splitlines() if re.match(r"\s+(g4dn|p4de?)\.", ln)]
        assert rows and all("out-of-cluster" in ln for ln in rows), rows

    @pytest.mark.parametrize("pinned,rule", [
        ("g4dn.xlarge", "instance-family NotIn"),
        ("g4dn.12xlarge", "instance-family NotIn"),
        ("p4d.24xlarge", "instance-generation Gte 5"),
        ("p4de.24xlarge", "instance-generation Gte 5"),
    ])
    def test_pinning_it_is_refused_with_the_nodepool_rule(self, new_model, pinned, rule):
        code, out, err = new_model(self.SMALL_FP16 + ["--instance-type", pinned], model=_model(3.0))
        assert code == 1
        assert f"--instance-type {pinned}" in err and "gpu-inference" in err and rule in err
        assert "You'll deploy this" not in out and "RECOMMENDED" not in out

    def test_pinning_an_allowed_type_still_works(self, new_model):
        code, out, _err = new_model(self.SMALL_FP16 + ["--instance-type", "g5.xlarge"], model=_model(3.0))
        assert code == 0 and "RECOMMENDED: g5.xlarge" in out
        assert _manifest(out)["spec"]["instanceType"] == "g5.xlarge"

    def test_the_banner_warns_when_only_a_refused_type_fits(self, new_model, monkeypatch):
        # Not reachable with today's catalog (every refused type has an allowed
        # twin of the same shape), so narrow the catalog to the refused type.
        from recommend_instance import recommend
        t4 = [i for i in catalog.INSTANCES if i.name == "g4dn.xlarge"]
        monkeypatch.setattr(recommend, "INSTANCES", t4)
        code, out, _err = new_model(self.SMALL_FP16, model=_model(3.0))
        assert code == 0 and "g4dn.xlarge" in out
        assert "cannot be launched by this cluster" in out and "instance-family NotIn" in out
