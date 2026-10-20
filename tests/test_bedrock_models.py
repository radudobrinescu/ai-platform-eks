"""Tests for how the dashboard discovers Bedrock models.

Bedrock models enrolled with `platformctl new-model --source bedrock` are
BedrockModel CRs that litellm-sync registers via the LiteLLM API; they are NOT in
the litellm-config ConfigMap. The dashboard must list them anyway, and still
honour static ConfigMap entries.
"""

import backend


def _cr(name, model, model_name=None):
    spec = {"model": model}
    if model_name is not None:
        spec["modelName"] = model_name
    return {"metadata": {"name": name}, "spec": spec}


class TestBedrockModels:
    def test_lists_bedrock_model_crs(self, monkeypatch):
        monkeypatch.setattr(backend, "_bedrock_models_from_config", lambda: [])
        monkeypatch.setattr(
            backend, "k8s_get",
            lambda path: {"items": [_cr("nova-lite", "bedrock/eu.amazon.nova-lite-v1:0", "nova-lite")]})
        assert backend._bedrock_models() == [
            {"name": "nova-lite", "model": "bedrock/eu.amazon.nova-lite-v1:0"}]

    def test_falls_back_to_metadata_name_when_no_model_name(self, monkeypatch):
        monkeypatch.setattr(backend, "_bedrock_models_from_config", lambda: [])
        monkeypatch.setattr(
            backend, "k8s_get", lambda path: {"items": [_cr("nova-pro", "bedrock/x")]})
        assert [m["name"] for m in backend._bedrock_models()] == ["nova-pro"]

    def test_merges_static_config_and_crs_deduped_by_name(self, monkeypatch):
        monkeypatch.setattr(
            backend, "_bedrock_models_from_config",
            lambda: [{"name": "a", "model": "bedrock/old"}, {"name": "b", "model": "bedrock/b"}])
        monkeypatch.setattr(
            backend, "k8s_get", lambda path: {"items": [_cr("a", "bedrock/new", "a")]})
        got = {m["name"]: m["model"] for m in backend._bedrock_models()}
        assert got == {"a": "bedrock/new", "b": "bedrock/b"}

    def test_no_crs_or_api_error_yields_only_static(self, monkeypatch):
        # k8s_get returns None on any non-200 (e.g. CRD absent, RBAC denied).
        monkeypatch.setattr(
            backend, "_bedrock_models_from_config", lambda: [{"name": "s", "model": "bedrock/s"}])
        monkeypatch.setattr(backend, "k8s_get", lambda path: None)
        assert backend._bedrock_models() == [{"name": "s", "model": "bedrock/s"}]

    def test_skips_crs_without_any_name(self, monkeypatch):
        monkeypatch.setattr(backend, "_bedrock_models_from_config", lambda: [])
        monkeypatch.setattr(
            backend, "k8s_get", lambda path: {"items": [{"metadata": {}, "spec": {"model": "bedrock/x"}}]})
        assert backend._bedrock_models() == []
