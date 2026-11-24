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


def test_hostile_values_are_always_escaped():
    out = render(hostile_sections(), [hostile(INVESTIGATION)], search="x-pwn")
    written = out["written"]
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
    last = {w["id"]: w["html"] for w in written}
    assert "unavailable since" in last["teams-grid"] and "as of" in last["gpu-bars"]


def test_plain_values_render_unchanged():
    out = render(store_sections()[1])
    models = next(w["html"] for w in out["written"] if w["id"] == "models-body")
    assert 'data-model="qwen2-5-3b-instruct"' in models and ">qwen2-5-3b-instruct<" in models


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
