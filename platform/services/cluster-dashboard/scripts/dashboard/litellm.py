"""LiteLLM usage, spend and team data.

Temporary: reads LiteLLM's database tables directly, as the pre-rebuild
backend did. U3 replaces this with LiteLLM's REST API and per-pod metrics.
"""
from __future__ import annotations

from collections.abc import Callable


def fetch(connect: Callable) -> dict:
    """Per-model 5-minute traffic, model health, team budgets and spend, the
    spend trend and per-user cost. The core per-model query must succeed; the
    others are optional (a LiteLLM version may lack a table) and their absence
    is reported in `missing`."""
    out = {"perModel": {}, "teams": [], "trend": [], "health": {}, "byTeam": [], "byUser": [], "missing": []}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          SELECT model_group, custom_llm_provider, count(*)/5.0,
                 percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM ("endTime"-"startTime"))*1000),
                 percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM ("endTime"-"startTime"))*1000),
                 COALESCE(sum(total_tokens),0)/300.0, COALESCE(sum(spend),0),
                 100.0*avg(CASE WHEN status='success' THEN 0 ELSE 1 END)
          FROM "LiteLLM_SpendLogs" WHERE "startTime" > now() - interval '5 minutes'
          GROUP BY model_group, custom_llm_provider""")
        for r in cur.fetchall():
            out["perModel"][r[0]] = {"provider": r[1], "rpm": round(r[2], 1),
                                     "p50": round(r[3]) if r[3] is not None else None,
                                     "p95": round(r[4]) if r[4] is not None else None,
                                     "tps": round(r[5], 1), "spend5m": float(r[6]),
                                     "errPct": round(float(r[7]), 1) if r[7] is not None else 0}

        def optional(name, sql, handle):
            try:
                cur.execute(sql)
                handle(cur.fetchall())
            except Exception as e:  # missing table or column in this LiteLLM version
                # (autocommit connection: a failed statement does not poison the next)
                out["missing"].append(f"{name}: {type(e).__name__}")

        optional("health", 'SELECT DISTINCT ON (model_name) model_name,status,response_time_ms '
                           'FROM "LiteLLM_HealthCheckTable" ORDER BY model_name,checked_at DESC',
                 lambda rows: out["health"].update(
                     {r[0]: {"status": r[1], "rt": float(r[2]) if r[2] is not None else None} for r in rows}))

        teams: dict = {}

        def set_teams(rows):
            for r in rows:
                teams[r[0]] = {"id": r[0], "name": r[1], "budget": round(float(r[3]), 2),
                               "limit": float(r[2]) if r[2] else None, "reqsToday": int(r[4]),
                               "failed": int(r[5]), "spendToday": round(float(r[6]), 2),
                               "tokens": int(r[7]), "keys": 0}

        optional("teams", """
              SELECT t.team_id, COALESCE(t.team_alias, left(t.team_id::text,8)), t.max_budget, COALESCE(t.spend,0),
                     COALESCE(sum(s.api_requests),0), COALESCE(sum(s.failed_requests),0),
                     COALESCE(sum(s.spend),0), COALESCE(sum(s.prompt_tokens+s.completion_tokens),0)
              FROM "LiteLLM_TeamTable" t
              LEFT JOIN "LiteLLM_DailyTeamSpend" s ON s.team_id=t.team_id AND s.date=CURRENT_DATE::text
              GROUP BY t.team_id, t.team_alias, t.max_budget, t.spend""", set_teams)
        if teams:
            def set_keys(rows):
                for tid, c in rows:
                    if tid in teams:
                        teams[tid]["keys"] = int(c)
            optional("keys", 'SELECT team_id,count(*) FROM "LiteLLM_VerificationToken" '
                             'WHERE team_id IS NOT NULL GROUP BY team_id', set_keys)
        out["teams"] = list(teams.values())
        out["byTeam"] = sorted([[t["name"], t["spendToday"]] for t in out["teams"] if t["spendToday"] > 0],
                               key=lambda x: -x[1])

        optional("trend", 'SELECT date::text, COALESCE(sum(spend),0) FROM "LiteLLM_DailyTeamSpend" '
                          'GROUP BY date ORDER BY date DESC LIMIT 14',
                 lambda rows: out.__setitem__("trend", [[r[0], round(float(r[1]), 2)] for r in rows][::-1]))

        def set_users(rows):
            agg: dict = {}
            for u, model, reqs, toks, spend in rows:
                e = agg.setdefault(u, {"user": u, "requests": 0, "tokens": 0, "spend": 0.0, "models": {}})
                e["requests"] += int(reqs)
                e["tokens"] += int(toks)
                e["spend"] += float(spend)
                e["models"][model] = e["models"].get(model, 0.0) + float(spend)
            out["byUser"] = sorted(
                [{"user": e["user"], "requests": e["requests"], "tokens": e["tokens"], "spend": round(e["spend"], 4),
                  "models": sorted([{"model": m, "spend": round(v, 4)} for m, v in e["models"].items()],
                                   key=lambda x: -x["spend"])}
                 for e in agg.values()], key=lambda x: -x["spend"])[:50]

        # Open WebUI forwards the signed-in user as an x-openwebui-user-email
        # request tag; direct API callers are attributed by key and team only.
        optional("users", """
              SELECT split_part(tag, ': ', 2) AS u, COALESCE(model_group, '?'),
                     count(*), COALESCE(sum(total_tokens),0), COALESCE(sum(spend),0)
              FROM "LiteLLM_SpendLogs", jsonb_array_elements_text(request_tags) tag
              WHERE "startTime" > now() - interval '30 days'
                AND tag LIKE 'x-openwebui-user-email:%'
              GROUP BY 1, 2""", set_users)
    return out
