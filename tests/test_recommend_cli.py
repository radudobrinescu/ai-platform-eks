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


class TestTypesWithoutARegionalPrice:
    """Prices came from the Pricing API (or its cache) and the region has none
    for a type: it is flagged, and not recommended while a priced type fits."""

    REGION = "eu-north-1"
    SRC = "AWS Pricing API (eu-north-1)"
    FLAG = "no price in eu-north-1 — may not be offered there"

    def _prices(self, *unpriced_families):
        """The regional price table: every type priced, except these families."""
        return {name: (None if name.split(".")[0] in unpriced_families else price)
                for name, price in STATIC_PRICES.items()}

    def _run(self, new_model, argv, *unpriced_families, **kw):
        return new_model(argv + ["--region", self.REGION], prices=self._prices(*unpriced_families),
                         price_src=self.SRC, **kw)

    def test_the_cheapest_type_is_not_recommended_without_a_regional_price(self, new_model):
        # g6.xlarge is the cheapest fit at its us-east-1 price
        code, out, _err = self._run(new_model, ["org/m"], "g6", model=_model(3.0))
        assert code == 0
        assert "RECOMMENDED: g5.xlarge" in out
        assert self.FLAG not in out.split("Alternatives")[0]   # nothing to flag about the pick

    def test_it_is_flagged_in_the_table(self, new_model):
        _code, out, _err = self._run(new_model, ["org/m", "--verbose", "--limit", "40"], "g6", model=_model(3.0))
        g6 = [ln for ln in out.splitlines() if re.match(r"\s+g6\.", ln)]
        assert g6 and all(self.FLAG in ln for ln in g6), g6
        assert not [ln for ln in out.splitlines() if re.match(r"\s+g5\.", ln) and self.FLAG in ln]

    def test_it_is_not_the_fleet_pick_and_is_flagged_in_the_fleet_table(self, new_model):
        # g7e.2xlarge is the cheapest fleet for 200 users at its us-east-1 price
        _code, out, _err = self._run(new_model, ["org/m", "--users", "200", "--limit", "40"], "g7e")
        assert _fleet_headline(out) == (2, "g6e.xlarge")
        g7e = [ln for ln in out.splitlines() if re.match(r"\s+g7e\.", ln)]
        assert g7e and all(self.FLAG in ln for ln in g7e), g7e

    def test_it_is_recommended_with_a_warning_when_nothing_priced_fits(self, new_model):
        families = {name.split(".")[0] for name in STATIC_PRICES}
        code, out, _err = self._run(new_model, ["org/m"], *families, model=_model(3.0))
        assert code == 0 and "RECOMMENDED: g6.xlarge" in out
        assert self.FLAG in out.split("Why:")[0]

    def test_pinning_it_warns_in_the_banner(self, new_model):
        code, out, _err = self._run(new_model, ["org/m", "--instance-type", "g6.xlarge"], "g6", model=_model(3.0))
        assert code == 0 and "RECOMMENDED: g6.xlarge" in out
        assert self.FLAG in out.split("Why:")[0]

    def test_json_notes_carry_the_flag(self, new_model):
        import json
        _code, out, _err = self._run(new_model, ["org/m", "--json", "--limit", "40"], "g6", model=_model(3.0))
        doc = json.loads(out)
        assert doc["recommended"]["instance"] == "g5.xlarge"
        notes = {o["instance"]: o["notes"] for o in doc["options"]}
        assert any("no price in the requested region" in n for n in notes["g6.xlarge"])
        assert not any("no price" in n for n in notes["g5.xlarge"])

    def test_with_static_prices_nothing_is_flagged(self, new_model):
        _code, out, _err = new_model(["org/m", "--verbose", "--limit", "40"], model=_model(3.0))
        assert "no price in" not in out
