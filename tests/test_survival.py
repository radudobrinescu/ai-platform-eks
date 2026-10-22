"""Survival compare logic on recorded snapshots (design section 4)."""

import copy

import survival

BEFORE = {
    "team": {"id": "team-1", "alias": "e2e", "max_budget": 25.0},
    "key": {"alias": "e2e-kro-managed", "spend": 0.0012, "team_id": "team-1"},
    "key_authenticates": True,
    "spend_log_count": 4,
    "models": {"nova-lite": {"listed": True, "chat": 200}, "qwen2-5-0-5b-instruct": {"listed": True, "chat": 200}},
    "langfuse_traces": 5,
    "openwebui_user_ids": {"admin@example.com": "u1", "developer@example.com": "u2", "user@example.com": "u3"},
    "cognito": {"pool_id": "eu-central-1_AAAA", "user_count": 3},
}


def _result(after):
    return {name: ok for name, ok, _ in survival.compare(BEFORE, after)}


def test_identical_state_passes_every_rule():
    res = _result(copy.deepcopy(BEFORE))
    assert len(res) == 9 and all(res.values()), res


def test_growth_is_fine():
    after = copy.deepcopy(BEFORE)
    after["key"]["spend"] = 0.01
    after["spend_log_count"] = 9
    after["langfuse_traces"] = 12
    assert all(_result(after).values())


def test_new_database_is_caught():
    after = copy.deepcopy(BEFORE)
    after["openwebui_user_ids"]["admin@example.com"] = "u99"
    after["team"]["id"] = "team-2"
    after["spend_log_count"] = 0
    after["langfuse_traces"] = 0
    res = _result(after)
    for rule in ("openwebui-users-kept", "team-survived", "spend-logs-kept", "langfuse-traces-kept"):
        assert res[rule] is False, rule


def test_replaced_cognito_pool_is_caught():
    after = copy.deepcopy(BEFORE)
    after["cognito"] = {"pool_id": "eu-central-1_BBBB", "user_count": 3}
    assert _result(after)["cognito-pool-kept"] is False


def test_model_not_answering_is_caught():
    after = copy.deepcopy(BEFORE)
    after["models"]["qwen2-5-0-5b-instruct"]["chat"] = 404
    assert _result(after)["seeded-models-answer"] is False


def test_budget_change_and_dead_key_are_caught():
    after = copy.deepcopy(BEFORE)
    after["team"]["max_budget"] = 100.0
    after["key_authenticates"] = False
    res = _result(after)
    assert res["team-budget-unchanged"] is False and res["key-survived-and-authenticates"] is False


def test_missing_values_never_pass():
    res = _result({})
    assert not any(res.values()), res
    assert not any(ok for _, ok, _ in survival.compare({}, {}))

