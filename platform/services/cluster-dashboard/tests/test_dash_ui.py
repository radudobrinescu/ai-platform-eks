"""The single-file UI (cluster-topology.html).

Escaping: the page's own script is run in Node (tests/ui_render.js, a minimal
DOM stand-in) on the golden /data.json with every string replaced by a
hostile value, and every HTML string it writes is checked. Skipped when Node
is not installed; the static checks below always run.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from dash_testlib import fixture_app

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(os.path.dirname(HERE), "cluster-topology.html")
PAYLOAD = "<x-pwn a=\"1\" b='2'>&"


def page():
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def script():
    p = page()
    return p[p.index("<script>") + len("<script>"):p.rindex("</script>")]


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def test_one_escaping_helper_covering_five_characters():
    s = script()
    assert len(re.findall(r"\.replace\(\s*/\[[^\]]*<", s)) == 1, "exactly one escaping function"
    m = re.search(r"const esc = \(s\) =>(.*?);\n", s, re.S)
    assert m, "esc() not found"
    for ch, ent in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")):
        assert ent in m.group(1), f"esc() must escape {ch}"


def test_inline_handlers_never_build_javascript_from_values():
    """Values go into data-* attributes; an inline handler reads this.dataset.
    HTML escaping cannot protect a value inside a JavaScript string in an
    attribute: the browser decodes the entity before running the code."""
    offenders = re.findall(r'\bon[a-z]+="[^"]*\$\{[^"]*"', page())
    assert offenders == []


# ---------------------------------------------------------------------------
# Rendering hostile data
# ---------------------------------------------------------------------------

def hostile(v):
    if isinstance(v, str):
        return PAYLOAD + v
    if isinstance(v, list):
        return [hostile(x) for x in v]
    if isinstance(v, dict):
        return {k: hostile(x) for k, x in v.items()}
    return v


def render(sections, investigations=(), search=""):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, os.path.join(HERE, "ui_render.js"), HTML], check=True, capture_output=True,
                         text=True, timeout=60,
                         input=json.dumps({"sections": sections, "investigations": list(investigations),
                                           "search": search}, default=str))
    return json.loads(out.stdout)


def store_sections():
    app = fixture_app()
    app.derive_once()
    return app, app.store.get()[1]


INVESTIGATION = {"id": "0b6c2f3e-8a51-4f7d-9c1e-2a3b4c5d6e7f", "resource_kind": "Pod", "resource_name": "x",
                 "findings": {"summary": "s", "severity": "HIGH", "risk_assessment": "r",
                              "fix_commands": ["kubectl get pods"]}}


def hostile_sections():
    """Every string hostile, except source names and states (the page looks
    sources up by name); one source failing and one stale so the panel notes
    render with hostile reasons."""
    _, sections = store_sections()
    sources = sections["status"]["sources"]
    bad = hostile(sections)
    bad["status"]["sources"] = [dict(hostile(x), name=x["name"], state=x["state"]) for x in sources]
    for x in bad["status"]["sources"]:
        if x["name"] == "litellm-api":
            x.update(state="error", lastSuccess=1795168000.0)
        if x["name"] == "dcgm":
            x.update(state="stale", lastSuccess=1795168000.0)
    bad["status"]["degraded"] = [{"package": PAYLOAD, "effect": PAYLOAD}]
    bad["meta"]["links"] = [{"label": "LiteLLM Admin", "url": "javascript:alert(1)", "icon": "x", "desc": "d"}]
    return bad


def assert_escaped(written):
    views = {w["id"] for w in written}
    assert {"kpis", "models-body", "teams-grid", "cost-by-user", "infra-attn", "infra-gpu", "infra-platform",
            "infra-nodepools", "health-alerts", "drawer", "q-res", "src-pop", "degraded-banner"} <= views
    for w in written:
        html = w["html"]
        assert "<x-pwn" not in html, f"unescaped value in #{w['id']}"
        assert html.count("x-pwn") == html.count("&lt;x-pwn a=&quot;1&quot; b=&#39;2&#39;&gt;&amp;"), \
            f"partly escaped value in #{w['id']}"
        for handler in re.findall(r'\bon[a-z]+="([^"]*)"', html):
            assert "x-pwn" not in handler, f"value inside an inline handler in #{w['id']}"
        assert "javascript:" not in html


def test_hostile_values_are_always_escaped():
    out = render(hostile_sections(), [hostile(INVESTIGATION)], search="x-pwn")
    written = out["written"]
    assert_escaped(written)
    last = {w["id"]: w["html"] for w in written}
    assert "unavailable since" in last["teams-grid"] and "as of" in last["gpu-bars"]


def test_hostile_values_are_escaped_in_the_scale_to_zero_states():
    """Every string hostile except the model states and tiers, which pick the
    code that draws a model scaled to zero and one waking from zero (with a
    hostile state the page draws every model as "deploying")."""
    _, plain = store_sections()
    bad = hostile_sections()
    for m, was in zip(bad["models"], plain["models"]):
        m.update(status=was["status"], tier=was["tier"])
    for p, was in zip(bad["pods"], plain["pods"]):
        # A pod that waits for a node is drawn by its phase, its age and its empty node name.
        p.update(phase=was["phase"], created=was["created"], node=p["node"] if was["node"] else "")
    out = render(bad, [hostile(INVESTIGATION)], search="x-pwn")
    assert_escaped(out["written"])
    last = {w["id"]: w["html"] for w in out["written"]}
    assert '<span class="st zero">scaled to zero</span>' in last["models-body"]
    assert '<span class="st info">waking</span>' in last["models-body"]
    assert "1 scaled to zero · 1 waking" in last["kpis"]
    assert "Scaled to zero, holding no GPU" in last["infra-gpu"] and "is waking from zero" in last["infra-attn"]
    assert any(w["id"] == "drawer" and '<span class="stag zero">scaled to zero</span>' in w["html"]
               for w in out["written"])

    # The waking model's pod on a GPU node: its slot is drawn.
    node = next(n for n in bad["nodes"] if n["gpu"])
    node.update(gpu=4)
    for p, was in zip(bad["pods"], plain["pods"]):
        if was["name"].startswith("mistral-7b-llmd-"):
            p.update(node=node["name"], phase="Running")
    out = render(bad)
    assert_escaped(out["written"])
    assert "<small>waking from zero · " in {w["id"]: w["html"] for w in out["written"]}["infra-gpu"]


def test_plain_values_render_unchanged():
    out = render(store_sections()[1])
    models = next(w["html"] for w in out["written"] if w["id"] == "models-body")
    assert 'data-model="qwen2-5-3b-instruct"' in models and ">qwen2-5-3b-instruct<" in models


# ---------------------------------------------------------------------------
# Scale to zero: an llm-d model idle at zero replicas, and one waking from zero
# (fixture models phi-4-mini and mistral-7b)
# ---------------------------------------------------------------------------

def panels(sections):
    out = render(sections)
    last = {w["id"]: w["html"] for w in out["written"]}
    return last, [w["html"] for w in out["written"] if w["id"] == "drawer"]


def row(table, name):
    part = table[table.index(f'data-model="{name}"'):]
    return part[:part.index("</tr>")]


def drawer_of(drawers, name):
    return next(d for d in drawers if f'font-weight:800">{name}<' in d)


def with_model(sections, name, **changes):
    return dict(sections, models=[dict(m, **changes) if m["name"] == name else m for m in sections["models"]])


def test_models_view_words_scaled_to_zero_and_waking():
    """Before: a model at zero replicas read "running" with a green dot, and a
    model waking from zero read "deploying" in amber."""
    _, sections = store_sections()
    last, _ = panels(sections)
    zero, waking = row(last["models-body"], "phi-4-mini"), row(last["models-body"], "mistral-7b")
    assert '<span class="st zero">scaled to zero</span>' in zero and "running" not in zero
    assert 'title="autoscaling 0/2 replicas"' in zero
    assert '<span class="st info">waking</span>' in waking and "deploying" not in waking
    assert '<span class="st ok">running</span>' in row(last["models-body"], "qwen2-5-3b-instruct")
    assert '<span class="st warn">deploying</span>' in row(last["models-body"], "gemma-12b")
    assert '<span class="st bad">error</span>' in row(last["models-body"], "broken")
    # The Overview lists the six busiest models; the fixture has seven.
    self_hosted = dict(sections, models=[m for m in sections["models"] if m["tier"] != "bedrock"])
    overview = panels(self_hosted)[0]["ov-models"]
    assert '<span class="dot zero"></span><span class="m-name">phi-4-mini<' in overview
    assert '<span class="dot info"></span><span class="m-name">mistral-7b<' in overview
    assert '<span class="dot ok"></span><span class="m-name">qwen2-5-3b-instruct<' in overview


def test_overview_counts_only_serving_models_as_live():
    _, sections = store_sections()
    last, _ = panels(sections)
    assert ('<div class="kpi-val">7</div><div class="kpi-sub">3 live · 1 scaled to zero · 1 waking · 2 deploying</div>'
            in last["kpis"])
    assert "appeared → scaled to zero" in last["ov-feed"] and "appeared → waking" in last["ov-feed"]
    calm = dict(sections, models=[m for m in sections["models"] if m["status"] == "running"])
    assert '<div class="kpi-sub">3 live</div>' in panels(calm)[0]["kpis"]


def test_scaled_to_zero_and_waking_are_not_alerts():
    _, sections = store_sections()
    last, _ = panels(sections)
    for panel in ("health-alerts", "ov-health"):
        assert "phi-4-mini" not in last[panel] and "mistral-7b" not in last[panel]
    assert "gemma-12b · deploying" in last["health-alerts"] and "broken · error" in last["health-alerts"]
    count = int(re.search(r"⚠ (\d+)</span> alerts", last["top-health"]).group(1))

    # The backend turns "waking" into "deploying" once the router's hold has passed.
    late, _ = panels(with_model(sections, "mistral-7b", status="deploying"))
    assert "mistral-7b · deploying" in late["health-alerts"] and f"⚠ {count + 1}</span> alerts" in late["top-health"]
    assert '<span class="st warn">deploying</span>' in row(late["models-body"], "mistral-7b")

    # A stored LiteLLM health check of a model with no replica says nothing.
    unhealthy, _ = panels(with_model(with_model(sections, "phi-4-mini", health="unhealthy"),
                                     "mistral-7b", health="unhealthy"))
    assert "phi-4-mini" not in unhealthy["health-alerts"] and "mistral-7b" not in unhealthy["health-alerts"]
    # Its router must be up to hold the request that wakes it.
    router, _ = panels(with_model(sections, "phi-4-mini", router="Degraded"))
    assert "phi-4-mini · router Degraded" in router["health-alerts"]


def test_model_drawer_shows_the_state_the_real_minimum_and_the_signal():
    _, sections = store_sections()
    _, drawers = panels(sections)
    zero = drawer_of(drawers, "phi-4-mini")
    assert '<span class="dot zero"></span>' in zero and '<span class="st zero">scaled to zero</span>' in zero
    assert '<span class="stag zero">scaled to zero</span><span class="cnt">0 / 2 · min 0</span>' in zero
    assert "requests in flight" in zero and "queue depth" not in zero
    assert "Deployment</div>" not in zero, "nothing is deploying"
    waking = drawer_of(drawers, "mistral-7b")
    assert '<span class="dot info"></span>' in waking and '<span class="st info">waking</span>' in waking
    assert '<span class="stag info">waking</span><span class="cnt">1 / 2 · min 0</span>' in waking
    assert "Deployment</div>" in waking and "FailedScheduling" in waking, "what it waits for stays visible"
    running = drawer_of(drawers, "qwen2-5-3b-instruct")
    assert '<span class="dot ok"></span>' in running and '<span class="st ok">running</span>' in running
    steady = drawer_of(drawers, "gemma-12b")
    assert '<span class="st warn">deploying</span>' in steady and "· min 1</span>" in steady


def test_infra_draws_a_waking_model_as_in_progress_and_names_the_models_at_zero():
    _, sections = store_sections()
    last, _ = panels(sections)
    attn = last["infra-attn"]
    waking = attn[attn.index("mistral-7b-llmd-5b7c9d6f4-k8x2p") - 200:]
    waking = waking[waking.index('<div class="alert">'):]
    waking = waking[:waking.index("</div></div>")]
    assert '<span class="stag info">In progress</span>' in waking and "mistral-7b is waking from zero" in waking
    stuck = attn[attn.index('<div class="alert"><span class="stag warn">Warning</span><div style="min-width:0">'
                            '<div class="at"><span class="m-id">gemma-12b-llmd-7c9d-abcde</span> has waited'):]
    assert "needs a GPU" in stuck[:stuck.index("</div></div>")]
    assert ('Scaled to zero, holding no GPU: <b>phi-4-mini</b>. It starts again on its next request, '
            'which takes a few minutes.</div>') in last["infra-gpu"]

    # Its pod has a node and loads the model: the slot says so.
    node = next(n for n in sections["nodes"] if n["gpu"])
    placed = dict(sections, nodes=[dict(n, pool="gpu-shared", gpu=4) if n is node else n for n in sections["nodes"]],
                  pods=[dict(p, node=node["name"], phase="Running") if p["name"].startswith("mistral-7b-llmd-") else p
                        for p in sections["pods"]])
    gpu = panels(placed)[0]["infra-gpu"]
    assert '<b>mistral-7b</b><small>waking from zero · ' in gpu and gpu.count('class="gs used"') == 2

    late = with_model(sections, "mistral-7b", status="deploying")
    assert '<span class="m-id">mistral-7b-llmd-5b7c9d6f4-k8x2p</span> has waited' in panels(late)[0]["infra-attn"]
    none = dict(sections, models=[m for m in sections["models"] if m["status"] != "scaled-to-zero"])
    assert "Scaled to zero" not in panels(none)[0]["infra-gpu"]
    idle = dict(sections, nodes=[n for n in sections["nodes"] if not n["gpu"]], nodeClaims=[])
    gpu = panels(idle)[0]["infra-gpu"]
    assert "No GPU nodes are running, which costs nothing" in gpu and "Scaled to zero, holding no GPU" in gpu


# ---------------------------------------------------------------------------
# Infrastructure view
# ---------------------------------------------------------------------------

def infra_panels(sections):
    out = render(sections)
    return {w["id"]: w["html"] for w in out["written"] if w["id"].startswith("infra-")}


def test_infra_shows_who_holds_each_gpu_slot_and_what_is_free():
    """The old view drew one bar per slot with the node's utilisation, so one
    model on a shared GPU looked like four idle GPUs."""
    _, sections = store_sections()
    gpu = infra_panels(sections)["infra-gpu"]
    assert '<b class="m-id" style="color:var(--text)">gpu-inference</b> · 1 node · 1 of 1 GPUs in use · none free' in gpu
    assert 'class="gs used"' in gpu and "<b>qwen2-5-3b-instruct</b>" in gpu and "/hr</small>" in gpu
    assert "1 of 1 GPUs in use. Full." in gpu

    shared = dict(sections, nodes=[dict(n, pool="gpu-shared", gpu=4) if n["gpu"] else n for n in sections["nodes"]])
    gpu = infra_panels(shared)["infra-gpu"]
    assert gpu.count('class="gs free"') == 3 and gpu.count('class="gs used"') == 1
    assert "1 of 4 slices in use" in gpu and "<b>3 more small models fit</b> here at no extra cost" in gpu


def test_infra_lists_pools_that_have_no_node_with_their_limit():
    _, sections = store_sections()
    pools = infra_panels(sections)["infra-nodepools"]
    row = pools[pools.index(">gpu-shared<"):]
    row = row[:row.index("</tr>")]
    assert "No nodes. Starts one when a model needs it." in row and "0 of 32 slices started" in row
    assert "g4dn, g7, g7e" in row, "the instance families the pool refuses"
    assert ">managed node group<" in pools and "Whole cluster, nodes only" in pools


def test_infra_needs_attention_names_a_node_that_joined_without_its_gpu():
    """The case the release tests hit: Karpenter does not replace such a node."""
    _, sections = store_sections()
    attn = infra_panels(sections)["infra-attn"]
    assert '<span class="stag crit">Problem</span>' in attn and "never became ready" in attn
    assert "kubectl delete nodeclaim gpu-shared-x2bfn" in attn and ">Copy command<" in attn
    assert "The cloud had no capacity for a node" in attn, "Karpenter's capacity events"
    assert "Show pool" not in attn and "Open node" not in attn
    calm = infra_panels(dict(sections, nodeClaims=[], provisioning=[], nodeAlerts=[], pods=[], nodes=[]))
    assert "Nothing needs attention" in calm["infra-attn"]
    assert "No GPU nodes are running, which costs nothing" in calm["infra-gpu"]


def test_infra_does_not_warn_twice_about_a_node_that_is_still_starting():
    """A new node is NotReady for a minute or two. Its claim already reads "on
    its way"; a second line calling it a warning is noise on every scale-up."""
    _, sections = store_sections()
    node = "ip-10-10-40-1.eu-central-1.compute.internal"
    claim = dict(sections["nodeClaims"][0], name="gpu-shared-new", node=node, ready=False, launched=True,
                 registered=True, initialized=False, created="2099-01-01T00:00:00Z", waitingOn="Initialized", message="")
    starting = dict(sections, nodeClaims=[claim], nodeAlerts=[{"node": node, "issue": "NotReady", "sev": "warn"}])
    attn = infra_panels(starting)["infra-attn"]
    assert "is on its way" in attn and "NotReady" not in attn
    gone = dict(sections, nodeClaims=[], nodeAlerts=[{"node": node, "issue": "NotReady", "sev": "warn"}])
    assert "NotReady" in infra_panels(gone)["infra-attn"], "a NotReady node nobody is starting is still a warning"


def test_infra_puts_a_gpu_pool_node_without_a_gpu_under_gpu_nodes():
    _, sections = store_sections()
    node = next(n for n in sections["nodes"] if n["gpu"])
    broken = dict(sections, nodes=[dict(n, gpu=0) if n is node else n for n in sections["nodes"]])
    panels = infra_panels(broken)
    assert node["name"].split(".")[0] in panels["infra-gpu"] and "no GPU reported by this node" in panels["infra-gpu"]
    assert node["name"].split(".")[0] not in panels["infra-platform"]


def test_page_builds_the_same_document_as_data_json():
    """The page maps store sections to its D object as Store.data_json does."""
    app, sections = store_sections()
    keys = set(render(sections)["keys"])
    assert keys == set(app.store.data_json()) - {"mts"}


def test_page_uses_only_the_versioned_api_and_fonts():
    urls = re.findall(r"\b(?:fetch|EventSource|mutate)\(\s*['\"`]([^'\"`]+)", script())
    assert urls and all(u.startswith(("/api/v1/", "/fonts/")) for u in urls), urls
    assert "/data.json'" not in script() and "'/investigations" not in script()


# ---------------------------------------------------------------------------
# Same origin only
# ---------------------------------------------------------------------------

def test_page_loads_nothing_from_another_origin():
    p = page()
    absolute = r"(?:https?:)?//"
    assert not re.findall(r"<(?:link|script|img|iframe|source|video|audio)\b[^>]*\b(?:src|href)=[\"']?" + absolute, p)
    assert not re.findall(r"url\(\s*[\"']?" + absolute, p)
    assert not re.findall(r"@import\s+(?:url\()?[\"']?" + absolute, p)
    assert not re.findall(r"\b(?:fetch|EventSource|XMLHttpRequest|WebSocket)\s*\(\s*[`\"']" + absolute, p)
    assert "fonts.googleapis.com" not in p and "gstatic" not in p


def test_every_font_the_page_uses_is_shipped():
    fonts = os.path.join(os.path.dirname(HERE), "fonts")
    for url in re.findall(r"url\((/fonts/[^)]+)\)", page()):
        assert os.path.isfile(os.path.join(fonts, url[len("/fonts/"):])), url
    with open(os.path.join(os.path.dirname(HERE), "kustomization.yaml"), encoding="utf-8") as f:
        kust = f.read()
    for name in os.listdir(fonts):
        if name.endswith((".woff2", ".txt")):
            assert f"fonts/{name}" in kust, f"{name} missing from the cluster-dashboard-fonts generator"
