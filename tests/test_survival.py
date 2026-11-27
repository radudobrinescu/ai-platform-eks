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



def test_trace_count_waits_until_langfuse_catches_up(monkeypatch):
    reads = iter([None, None, 2, 4, 9])
    monkeypatch.setattr(survival.checks, "langfuse_trace_count", lambda: next(reads))
    monkeypatch.setattr(survival, "wait_for", lambda desc, fn, timeout, interval=15: next(v for v in iter(fn, object()) if v))
    assert survival.settled_trace_count(4) == 4


def test_trace_count_returns_the_last_read_when_it_never_settles(monkeypatch):
    import itertools
    reads = itertools.chain([None, 2], itertools.repeat(3))
    monkeypatch.setattr(survival.checks, "langfuse_trace_count", lambda: next(reads))

    def wait(desc, fn, timeout, interval=15):
        for _ in range(5):
            if fn():
                return fn()
        raise TimeoutError(desc)
    monkeypatch.setattr(survival, "wait_for", wait)
    assert survival.settled_trace_count(4) == 3


def test_unreadable_before_count_still_fails_and_says_why():
    after = copy.deepcopy(BEFORE)
    before = copy.deepcopy(BEFORE)
    before["langfuse_traces"] = None
    res = {n: (ok, d) for n, ok, d in survival.compare(before, after)}
    ok, detail = res["langfuse-traces-kept"]
    assert not ok and "could not read the count before the upgrade" in detail


# --------------------------------------------------------------------------- #
# Planned resets: a release that drops a store on purpose                     #
# --------------------------------------------------------------------------- #
def _rule(results, name):
    return next(r for r in results if r[0] == name)


def test_langfuse_chart_1_to_2_is_a_planned_trace_reset():
    resets = survival.planned_resets({"chart/langfuse": "1.5.41"}, {"chart/langfuse": "2.2.4"})
    assert list(resets) == ["langfuse-traces-kept"]
    assert survival.planned_resets({"chart/langfuse": "1.5.41"}, {"chart/langfuse": "1.5.42"}) == {}
    assert survival.planned_resets({"chart/langfuse": "2.2.4"}, {"chart/langfuse": "2.3.0"}) == {}
    assert survival.planned_resets({}, {"chart/langfuse": "2.2.4"}) == {}


def test_a_planned_reset_needs_new_traces_instead_of_the_old_ones():
    before = copy.deepcopy(BEFORE)
    after = copy.deepcopy(BEFORE)
    after["langfuse_traces"] = 2
    resets = {"langfuse-traces-kept": "chart 2 starts empty"}
    name, ok, detail = _rule(survival.compare(before, after, resets), "langfuse-traces-kept")
    assert ok and "reset by design (chart 2 starts empty)" in detail and "traced" in detail
    assert not _rule(survival.compare(before, after), "langfuse-traces-kept")[1]
    after["langfuse_traces"] = 0
    name, ok, detail = _rule(survival.compare(before, after, resets), "langfuse-traces-kept")
    assert not ok and "NOT traced" in detail
    after["langfuse_traces"] = None
    assert not _rule(survival.compare(before, after, resets), "langfuse-traces-kept")[1]


# --------------------------------------------------------------------------- #
# After a planned trace reset the snapshot waits for the new traces, not for  #
# as many as before (release test of 2026-11-26: Langfuse 4 had just started,  #
# the snapshot waited 300 s for 4 traces, read 0, and the rule failed although #
# the next check found the new chats traced).                                  #
# --------------------------------------------------------------------------- #
def test_after_a_planned_reset_the_snapshot_waits_longer_for_one_trace(monkeypatch):
    waits = []
    monkeypatch.setattr(survival, "settled_trace_count", lambda expected, timeout=300: waits.append((expected, timeout)) or 1)
    monkeypatch.setattr(survival, "secret_value", lambda *a, **k: "team-1")
    for name in ("openwebui_ids", "cognito_state"):
        monkeypatch.setattr(survival, name, lambda *a, **k: None)

    class _PF:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(survival, "PortForward", lambda *a, **k: _PF())
    monkeypatch.setattr(survival, "_get", lambda *a, **k: (200, {}))
    monkeypatch.setattr(survival.checks, "models", lambda *a, **k: [])
    monkeypatch.setattr(survival.checks, "chat", lambda *a, **k: (200, ""))

    class _Ctx:
        def litellm_key(self):
            return "sk"
    seed = survival.Seed()
    seed.models, seed.team_key = ["nova-lite"], "sk-team"
    survival.snapshot(_Ctx(), seed)
    survival.snapshot(_Ctx(), seed, traces_reset=True)
    assert waits[0] == (survival.SEED_BEDROCK_CHATS + 1, 300)
    assert waits[1][0] == 1 and waits[1][1] >= survival.TRACES_AFTER_RESET_TIMEOUT >= 900


def test_the_upgrade_tells_the_snapshot_about_a_planned_reset():
    import pathlib
    text = (pathlib.Path(survival.__file__).parent / "e2e.py").read_text(encoding="utf-8")
    block = text[text.index("def do_compare():"):text.index('self.step("survival-snapshot", do_compare)')]
    assert block.index("survival.planned_resets(") < block.index("survival.snapshot(")
    assert 'traces_reset="langfuse-traces-kept" in resets' in block
