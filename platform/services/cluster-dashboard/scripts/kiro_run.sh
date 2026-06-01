# kiro_run.sh — shared kiro-cli invocation with MCP setup, retry + model fallback.
#
# Sourced by investigate.sh and remediate.sh (both mount the same
# platform-health-agent-scripts ConfigMap at /scripts). Each wrapper first
# writes its MCP config to $HOME/.kiro/settings/mcp.json, then calls run_kiro.
#
# MCP loading (the thing that kept the agent tool-less):
#   - kiro-cli reads global MCP config from $HOME/.kiro/settings/mcp.json. The
#     wrappers used to write $HOME/.kiro/mcp.json, which kiro-cli ignores — so
#     the eks-mcp-server tools never registered and the agent silently fell back
#     to raw kubectl. The wrappers now write the correct path.
#   - In --no-interactive mode kiro-cli waits up to `mcp.noInteractiveTimeout`
#     (default 30s) for MCP servers, then proceeds WITHOUT the ones still
#     starting. The eks-mcp-server is a slow-cold-starting python entrypoint, so
#     kiro_prepare() bumps that budget. We deliberately do NOT pass
#     --require-mcp-startup: if MCP still isn't ready, letting kiro-cli proceed
#     lets the LLM fall back to /tools/kubectl (RBAC-bounded, still safe) and
#     complete the task rather than hard-failing.
#
# Model retry/fallback: kiro-cli occasionally degrades at startup to where the
# only model it sees is 'auto', rejecting the configured model with
# "Model '<name>' does not exist. Available models: auto" and writing no output.
# So run_kiro retries the configured model, then falls back to --model auto.
#
#   kiro_prepare                                   # once, before run_kiro
#   run_kiro <label> <model> <out> <prompt> <log>  # 0 iff <out> is non-empty
#
# Tunables (env): KIRO_MAX_ATTEMPTS (default 3),
#                 KIRO_MCP_TIMEOUT_MS (default 120000).

kiro_prepare() {
    # Give slow MCP servers room to register before the prompt runs in
    # non-interactive mode. Best-effort: first-run/settings quirks must not
    # abort the investigation.
    _to="${KIRO_MCP_TIMEOUT_MS:-120000}"
    /tools/kiro-cli settings mcp.noInteractiveTimeout "$_to" >/dev/null 2>&1 \
        && echo "[kiro] set mcp.noInteractiveTimeout=${_to}ms" \
        || echo "[kiro] WARN: could not set mcp.noInteractiveTimeout (continuing)" >&2
    # Diagnostic: log which MCP servers kiro-cli sees configured.
    echo "[kiro] configured MCP servers:"
    /tools/kiro-cli mcp list 2>&1 | sed 's/^/  /' || true
}

run_kiro() {
    _label="$1"; _model="$2"; _out="$3"; _prompt_file="$4"; _log="$5"
    _max="${KIRO_MAX_ATTEMPTS:-3}"
    _prompt_text="$(cat "$_prompt_file")"

    _n=1
    while [ "$_n" -le "$_max" ]; do
        echo "[$_label] running kiro-cli model=${_model} (attempt ${_n}/${_max})…"
        rm -f "$_out"
        # Pipeline exit status is tee's (always 0); we judge success by whether
        # kiro-cli actually wrote the output file, which is what we ultimately need.
        /tools/kiro-cli chat --no-interactive \
            --model "${_model}" \
            --trust-all-tools \
            "$_prompt_text" 2>&1 | tee "$_log" || true
        if [ -s "$_out" ]; then
            return 0
        fi
        # Model reported unavailable (the degraded-startup case) → retrying the
        # same model won't help; break straight to the 'auto' fallback below.
        if grep -q "does not exist. Available models" "$_log" 2>/dev/null; then
            echo "[$_label] model ${_model} reported unavailable — switching to fallback." >&2
            break
        fi
        echo "[$_label] attempt ${_n} produced no ${_out}; retrying." >&2
        _n=$((_n + 1))
        sleep $(( _n * 2 ))
    done

    # Fallback: 'auto' is always present per kiro-cli. One shot, if not already tried.
    if [ "${_model}" != "auto" ]; then
        echo "[$_label] falling back to --model auto." >&2
        rm -f "$_out"
        /tools/kiro-cli chat --no-interactive \
            --model auto \
            --trust-all-tools \
            "$_prompt_text" 2>&1 | tee "$_log" || true
        if [ -s "$_out" ]; then
            return 0
        fi
    fi
    return 1
}
