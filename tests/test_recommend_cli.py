"""`platformctl new-model` end to end, offline.

`cli.main` runs with the Hugging Face model config and the EC2 prices stubbed,
so these tests see what a user sees: the printed report and the manifest that
`--deploy` would commit.
"""

import re
import shlex

import pytest
import yaml

import recommend_instance
from recommend_instance import catalog, cli, render, scaling
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


def _help_examples() -> list[str]:
    """Every `./platformctl new-model ...` command line in the --help epilog."""
    epilog = recommend_instance.__doc__.split("Examples:", 1)[1]
    return [ln.strip() for ln in epilog.splitlines()
            if "./platformctl new-model" in ln and not ln.lstrip().startswith("#")]


class TestHelpExamples:
    """The examples in --help are commands users copy: each one must parse."""

    def test_the_epilog_has_examples(self):
        assert len(_help_examples()) >= 10
        assert "Examples:" not in cli.build_parser().epilog and "./platformctl new-model" in cli.build_parser().epilog

    @pytest.mark.parametrize("line", _help_examples())
    def test_every_example_parses(self, line, capsys):
        argv = shlex.split(line)
        argv = argv[argv.index("new-model") + 1:]
        try:
            args = cli.build_parser().parse_args(argv)   # parses only; runs nothing
        except SystemExit:
            pytest.fail(f"--help example does not parse: {line}\n{capsys.readouterr().err}")
        assert args.model or args.undeploy

    def test_every_workload_preset_has_a_routing_profile_and_no_other(self):
        assert set(render.ROUTING_PROFILE_BY_WORKLOAD) == set(scaling.WORKLOAD_PRESETS)


def _big_model(num_heads: int) -> ModelSpec:
    """70B: needs two GPUs. 25 attention heads do not divide by 2, so the two
    GPUs can only be used with pipeline parallelism (PP=2)."""
    return ModelSpec(model_id="org/m", params=int(70e9), num_layers=80, hidden_size=8192,
                     num_heads=num_heads, num_kv_heads=8, head_dim=128, vocab_size=128256,
                     max_position=131072, architecture="LlamaForCausalLM")


class TestMinReplicasEdgeCases:
    @pytest.mark.parametrize("argv,called", [
        (["--source", "bedrock", "amazon.nova-lite-v1:0", "--min-replicas", "0"], "bedrock.run_bedrock"),
        (["--undeploy", "some-model", "--min-replicas", "0"], "gitops.undeploy_model"),
    ])
    def test_it_is_an_error_where_it_cannot_apply(self, argv, called, monkeypatch, capsys):
        import recommend_instance.bedrock
        import recommend_instance.gitops
        ran = []
        module, func = called.split(".")
        monkeypatch.setattr(getattr(recommend_instance, module), func, lambda *a, **k: ran.append(called) or 0)
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2
        assert "--min-replicas" in capsys.readouterr().err
        assert ran == []   # refused before anything is enrolled or deleted

    @pytest.mark.parametrize("argv,called", [
        (["--source", "bedrock", "amazon.nova-lite-v1:0"], "bedrock.run_bedrock"),
        (["--undeploy", "some-model"], "gitops.undeploy_model"),
    ])
    def test_those_paths_still_run_without_it(self, argv, called, monkeypatch):
        import recommend_instance.bedrock
        import recommend_instance.gitops
        ran = []
        module, func = called.split(".")
        monkeypatch.setattr(getattr(recommend_instance, module), func, lambda *a, **k: ran.append(called) or 0)
        assert cli.main(argv) == 0 and ran == [called]

    def test_the_reason_for_a_scale_to_zero_model_is_scale_to_zero(self, new_model):
        _code, out, _err = new_model(["org/m", "--min-replicas", "0"], model=_model(3.0))
        tier = next(ln for ln in out.splitlines() if ln.startswith("Serving tier:"))
        assert "LLMDEndpoint" in tier and "scale to zero" in tier and "llm-d" in tier
        assert "fleet of 2+ replicas" not in tier
        assert _manifest(out)["spec"]["minReplicas"] == 0

    def test_the_reason_for_a_fleet_is_unchanged(self, new_model):
        _code, out, _err = new_model(["org/m", "--users", "2000"])
        assert _fleet_headline(out)[0] >= 2
        tier = next(ln for ln in out.splitlines() if ln.startswith("Serving tier:"))
        assert "LLMDEndpoint" in tier and "fleet of 2+ replicas" in tier

    @pytest.mark.parametrize("argv,serving", [
        (["org/m", "--min-replicas", "0"], {"tier": "llm-d", "kind": "LLMDEndpoint", "min_replicas": 0}),
        (["org/m", "--tier", "llm-d"], {"tier": "llm-d", "kind": "LLMDEndpoint", "min_replicas": 1}),
        (["org/m"], {"tier": "vllm", "kind": "VLLMEndpoint", "min_replicas": None}),
    ])
    def test_json_reports_the_serving_tier_and_min_replicas(self, new_model, argv, serving):
        import json
        code, out, _err = new_model(argv + ["--json"], model=_model(3.0))
        assert code == 0
        assert json.loads(out)["serving"] == serving

    @pytest.mark.parametrize("extra", [[], ["--json"]])
    def test_scale_to_zero_is_refused_when_the_model_needs_pipeline_parallelism(self, new_model, extra):
        # --min-replicas forces the llm-d tier, which cannot run pipeline
        # parallelism: no manifest must come out of this.
        code, out, err = new_model(["org/m", "--min-replicas", "0"] + extra, model=_big_model(num_heads=25))
        assert code == 1
        assert "--min-replicas" in err and "pipeline parallelism" in err and "g7e.12xlarge" in err
        assert out == ""

    def test_scale_to_zero_works_when_the_model_runs_tensor_parallel(self, new_model):
        code, out, _err = new_model(["org/m", "--min-replicas", "0"], model=_big_model(num_heads=64))
        assert code == 0
        spec = _manifest(out)["spec"]
        assert (spec["gpuCount"], spec["tensorParallelSize"], spec["minReplicas"]) == (2, 2, 0)

    def test_pipeline_parallelism_without_min_replicas_stays_on_vllm(self, new_model):
        code, out, _err = new_model(["org/m"], model=_big_model(num_heads=25))
        assert code == 0
        doc = _manifest(out)
        assert doc["kind"] == "VLLMEndpoint" and doc["spec"]["pipelineParallelSize"] == 2


def _deploy_command(out: str) -> list[str]:
    """The arguments of the printed "Deploy it" command, after `new-model`."""
    line = next(ln for ln in out.splitlines() if "Deploy it:" in ln)
    words = shlex.split(line.split("./platformctl new-model", 1)[1])
    assert words[-1] == "--deploy", line
    return words[:-1]


class TestTheDeployCommandIsTheOneThatWasPreviewed:
    """The preview ends with the command to deploy it. Running that command must
    deploy what was previewed: it used to drop most options, so a
    `--min-replicas 0` preview told the user to deploy an always-on model."""

    @pytest.mark.parametrize("argv", [
        ["org/m"],
        ["org/m", "--min-replicas", "0"],
        ["org/m", "--tier", "llm-d", "--users", "120", "--target-tok-s", "25", "--avg-context", "2048"],
        ["org/m", "--tp", "2", "--worker-memory", "40Gi", "--tool-call-parser", "hermes"],
        ["org/m", "--quant", "fp8", "--seq", "16384", "--workload", "rag", "--users", "30"],
        ["org/m", "--extra-arg=--enable-prefix-caching", "--extra-arg", '["--kv-cache-dtype","fp8"]'],
        ["org/m", "--instance-type", "g6.2xlarge", "--tier", "vllm", "--in-cluster-only", "--max-price", "5"],
        ["org/m", "--model-name", "my-alias", "--safety-margin", "0.2", "--utilization", "0.5"],
    ])
    def test_the_printed_command_reproduces_the_manifest(self, new_model, argv):
        code, out, _err = new_model(argv)
        assert code == 0
        code_again, out_again, _err = new_model(_deploy_command(out))
        assert code_again == 0
        assert _manifest(out_again) == _manifest(out)

    def test_a_token_is_never_printed(self, new_model):
        code, out, err = new_model(["org/m", "--hf-token", "hf_secretvalue123", "--min-replicas", "0"])
        assert code == 0
        assert "hf_secretvalue123" not in out + err
        words = _deploy_command(out)
        assert "--hf-token" in words and "--min-replicas" in words

    def test_a_token_given_with_an_equals_sign_is_never_printed(self, new_model):
        code, out, err = new_model(["org/m", "--hf-token=hf_secretvalue123"])
        assert code == 0
        assert "hf_secretvalue123" not in out + err


class TestPipelineParallelismNeverReachesTheLlmdTier:
    """The llm-d tiers run a model on one node with tensor parallelism only. A
    layout that needs pipeline parallelism must stay on vLLM, also when it is the
    fleet's layout and not only the single-instance pick."""

    MODEL = dict(params_b=70.0, num_heads=25)      # 25 heads: no tensor-parallel split fits

    def test_an_automatic_fleet_stays_on_vllm(self, new_model):
        code, out, _err = new_model(["org/m", "--users", "100"], model=_model(**self.MODEL))
        assert code == 0
        assert "PP=" in out, "the fixture model was expected to need pipeline parallelism"
        assert _manifest(out)["kind"] == "VLLMEndpoint"

    @pytest.mark.parametrize("tier", ["llm-d", "llm-d-disagg"])
    def test_an_explicit_llmd_tier_is_refused(self, new_model, tier):
        code, out, err = new_model(["org/m", "--tier", tier, "--users", "100"], model=_model(**self.MODEL))
        chose_pp = "PP=" in out or "pipeline" in (out + err).lower()
        assert chose_pp, "the fixture model was expected to need pipeline parallelism"
        assert code != 0
        assert "pipeline parallelism" in err
        assert "You'll deploy this" not in out
