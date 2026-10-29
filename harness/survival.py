"""Upgrade mode: seed state with the BASE ref's own commands, snapshot it, and
after the upgrade prove it survived (design section 4).

Seeded through the base's interfaces       Must hold after the upgrade
  onboard-team e2e --deploy -y               team + key exist; key authenticates; budget unchanged
  new-model --source bedrock nova-lite       key spend >= before; spend-log count >= before
    + 3 chats with the team key
  new-model Qwen2.5-0.5B-Instruct + 1 chat   both models still listed and answering
  (Langfuse traces from the chats)           trace count >= before
  SSO login of the 3 seed users (Open WebUI) same Open WebUI user ids (a new database issues new ids)
  (Cognito pool)                             same pool id; same user count

Together these cover every PVC-backed store: platform-db (LiteLLM, Langfuse
metadata), ClickHouse, Langfuse S3, Valkey, Open WebUI data.

`compare` is a pure function over two snapshots (unit-tested). The team key is
kept in memory only; snapshots never contain a secret.
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse

import checks
import sso
from common import NS_PLATFORM, PortForward, http, log, secret_value, wait_for

TEAM = "e2e"
TEAM_NS = f"team-{TEAM}"
TEAM_SECRET = f"{TEAM}-api-key"
TEAM_BUDGET = "25"
SEED_BEDROCK = checks.BEDROCK_SMOKE_MODEL            # default alias of nova-lite
SEED_GPU_MODEL = checks.GPU_SMOKE_MODEL
SEED_BEDROCK_CHATS = 3


class Seed:
    """What seeding produced (names + the team key, kept in memory only)."""

    def __init__(self):
        self.team_key: str | None = None
        self.models: list[str] = []


def _team_key() -> str:
    return secret_value(TEAM_NS, TEAM_SECRET, "api-key")


def seed(c: checks.Ctx) -> Seed:
    s = Seed()
    rc = c.subject.platformctl("onboard-team", TEAM, "--budget", TEAM_BUDGET, "--deploy", "-y")
    if rc != 0:
        raise RuntimeError(f"onboard-team exited {rc}")
    s.team_key = wait_for(f"secret {TEAM_NS}/{TEAM_SECRET}", _team_key, 900, 15)

    alias, _ = checks.deploy_bedrock(c)
    checks.wait_alias(c, alias, c.timeouts["bedrock"])
    gpu = checks.model_name_from_id(SEED_GPU_MODEL)
    checks.deploy_model(c, gpu, SEED_GPU_MODEL, "--tier", "vllm", "--users", "1", "--seq", "2048")
    checks.wait_endpoint_ready(c, "vllmendpoint", gpu)
    checks.wait_alias(c, gpu, c.timeouts["bedrock"])
    s.models = [alias, gpu]

    with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
        for _ in range(SEED_BEDROCK_CHATS):
            code, body = checks.chat(pf, s.team_key, alias)
            if code != 200:
                raise RuntimeError(f"seed chat on {alias} with the team key: HTTP {code}: {body[:120]}")
        code, body = checks.chat(pf, s.team_key, gpu)
        if code != 200:
            raise RuntimeError(f"seed chat on {gpu} with the team key: HTTP {code}: {body[:120]}")
    # Sign the seed users in once, so Open WebUI creates their accounts.
    openwebui_ids(c)
    return s


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #

def _get(pf: PortForward, path: str, key: str) -> tuple[int, dict | list | None]:
    code, body = http("GET", f"{pf.base}{path}", headers={"Authorization": f"Bearer {key}"})
    try:
        return code, json.loads(body) if code == 200 else None
    except ValueError:
        return code, None


def openwebui_ids(c: checks.Ctx) -> dict[str, str | None]:
    s = c.sso()
    mapping, stop = s.forwards()
    try:
        ids = {}
        for email, _group, pw in sso._users(s):
            try:
                ids[email] = sso.login_open_webui(sso.Browser(mapping), email, pw).get("id")
            except Exception as e:  # noqa: BLE001 - recorded as a missing id
                log(f"Open WebUI login for {email}: {type(e).__name__}: {e}")
                ids[email] = None
        return ids
    finally:
        stop()


def cognito_state(c: checks.Ctx) -> dict:
    pool = c.sso().pool_id()
    out = subprocess.run(["aws", "cognito-idp", "list-users", "--user-pool-id", pool, "--query", "length(Users)"],
                         capture_output=True, text=True, check=True, timeout=120).stdout.strip()
    return {"pool_id": pool, "user_count": int(out)}


def snapshot(c: checks.Ctx, s: Seed) -> dict:
    """Read the seeded state. Never contains the key itself."""
    snap: dict = {}
    master = c.litellm_key()
    team_id = secret_value(TEAM_NS, TEAM_SECRET, "team-id")
    with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
        _, info = _get(pf, f"/team/info?team_id={urllib.parse.quote(team_id)}", master)
        t = (info or {}).get("team_info", info or {}) if isinstance(info, dict) else {}
        snap["team"] = {"id": t.get("team_id"), "alias": t.get("team_alias"), "max_budget": t.get("max_budget")}
        _, kinfo = _get(pf, f"/key/info?key={urllib.parse.quote(s.team_key or '')}", master)
        k = (kinfo or {}).get("info", {}) if isinstance(kinfo, dict) else {}
        snap["key"] = {"alias": k.get("key_alias"), "spend": k.get("spend"), "team_id": k.get("team_id")}
        code, _ = _get(pf, "/v1/models", s.team_key or "")
        snap["key_authenticates"] = code == 200
        _, logs = _get(pf, f"/spend/logs?api_key={urllib.parse.quote(s.team_key or '')}", master)
        snap["spend_log_count"] = len(logs) if isinstance(logs, list) else None
        listed = checks.models(pf, master)
        snap["models"] = {m: {"listed": m in listed, "chat": checks.chat(pf, master, m)[0]} for m in s.models}
    snap["langfuse_traces"] = checks.langfuse_trace_count()
    snap["openwebui_user_ids"] = openwebui_ids(c)
    snap["cognito"] = cognito_state(c)
    return snap


# --------------------------------------------------------------------------- #
# Compare (pure)
# --------------------------------------------------------------------------- #

def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ge(after, before) -> bool:
    a, b = _num(after), _num(before)
    return a is not None and b is not None and a >= b


def compare(before: dict, after: dict) -> list[tuple[str, bool, str]]:
    """Return (check name, ok, detail) per survival rule."""
    out = []
    bt, at = before.get("team") or {}, after.get("team") or {}
    out.append(("team-survived",
                bool(bt.get("id")) and bt.get("id") == at.get("id") and bt.get("alias") == at.get("alias"),
                f"team {bt.get('alias')} id {'unchanged' if bt.get('id') == at.get('id') else 'CHANGED or missing'}"))
    out.append(("team-budget-unchanged",
                _num(bt.get("max_budget")) is not None and _num(bt.get("max_budget")) == _num(at.get("max_budget")),
                f"max_budget {bt.get('max_budget')} -> {at.get('max_budget')}"))
    bk, ak = before.get("key") or {}, after.get("key") or {}
    out.append(("key-survived-and-authenticates",
                bool(bk.get("alias")) and bk.get("alias") == ak.get("alias") and bool(after.get("key_authenticates")),
                f"key {bk.get('alias')} -> {ak.get('alias')}; authenticates={after.get('key_authenticates')}"))
    out.append(("key-spend-kept", _ge(ak.get("spend"), bk.get("spend")),
                f"spend {bk.get('spend')} -> {ak.get('spend')}"))
    out.append(("spend-logs-kept", _ge(after.get("spend_log_count"), before.get("spend_log_count")),
                f"spend logs {before.get('spend_log_count')} -> {after.get('spend_log_count')}"))
    bm, am = before.get("models") or {}, after.get("models") or {}
    bad = [m for m in bm if not (am.get(m, {}).get("listed") and am.get(m, {}).get("chat") == 200)]
    out.append(("seeded-models-answer", bool(bm) and not bad,
                f"{len(bm) - len(bad)}/{len(bm)} seeded models listed and answering" + (f"; failing: {bad}" if bad else "")))
    out.append(("langfuse-traces-kept", _ge(after.get("langfuse_traces"), before.get("langfuse_traces")),
                f"traces {before.get('langfuse_traces')} -> {after.get('langfuse_traces')}"))
    bo, ao = before.get("openwebui_user_ids") or {}, after.get("openwebui_user_ids") or {}
    changed = [e for e, i in bo.items() if not i or ao.get(e) != i]
    out.append(("openwebui-users-kept", bool(bo) and not changed,
                f"{len(bo) - len(changed)}/{len(bo)} Open WebUI user ids unchanged" + (f"; changed: {changed}" if changed else "")))
    bc, ac = before.get("cognito") or {}, after.get("cognito") or {}
    out.append(("cognito-pool-kept",
                bool(bc.get("pool_id")) and bc.get("pool_id") == ac.get("pool_id") and bc.get("user_count") == ac.get("user_count"),
                f"pool {'unchanged' if bc.get('pool_id') == ac.get('pool_id') else 'REPLACED'}; users {bc.get('user_count')} -> {ac.get('user_count')}"))
    return out
