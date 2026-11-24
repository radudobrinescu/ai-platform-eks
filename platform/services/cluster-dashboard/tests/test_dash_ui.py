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

from dash_testlib import load_fixture

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


def render(data, investigations=(), search=""):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, os.path.join(HERE, "ui_render.js"), HTML], check=True, capture_output=True,
                         text=True, timeout=60,
                         input=json.dumps({"data": data, "investigations": list(investigations),
                                           "search": search}))
    return json.loads(out.stdout)


INVESTIGATION = {"id": "0b6c2f3e-8a51-4f7d-9c1e-2a3b4c5d6e7f", "resource_kind": "Pod", "resource_name": "x",
                 "findings": {"summary": "s", "severity": "HIGH", "risk_assessment": "r",
                              "fix_commands": ["kubectl get pods"]}}


def test_hostile_values_are_always_escaped():
    data = hostile(load_fixture("data.json.golden"))
    data["links"] = [{"label": "LiteLLM Admin", "url": "javascript:alert(1)", "icon": "🔗", "desc": "d"}]
    written = render(data, [hostile(INVESTIGATION)], search="x-pwn")
    assert written, "nothing rendered"
    views = {w["id"] for w in written}
    assert {"kpis", "models-body", "teams-grid", "cost-by-user", "infra-pools", "health-alerts", "drawer",
            "q-res"} <= views
    for w in written:
        html = w["html"]
        assert "<x-pwn" not in html, f"unescaped value in #{w['id']}"
        assert html.count("x-pwn") == html.count("&lt;x-pwn a=&quot;1&quot; b=&#39;2&#39;&gt;&amp;"), \
            f"partly escaped value in #{w['id']}"
        for handler in re.findall(r'\bon[a-z]+="([^"]*)"', html):
            assert "x-pwn" not in handler, f"value inside an inline handler in #{w['id']}"
        assert "javascript:" not in html


def test_plain_values_render_unchanged():
    written = render(load_fixture("data.json.golden"))
    models = next(w["html"] for w in written if w["id"] == "models-body")
    assert 'data-model="qwen2-5-3b-instruct"' in models and ">qwen2-5-3b-instruct<" in models
