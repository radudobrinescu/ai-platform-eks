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
