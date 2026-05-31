# kiro_run.sh — shared kiro-cli invocation with retry + model fallback.
#
# Sourced by investigate.sh and remediate.sh (both mount the same
# platform-health-agent-scripts ConfigMap at /scripts).
#
# Why: kiro-cli occasionally fails to retrieve its MCP/auth settings at
# startup ("Failed to retrieve MCP settings…"). When that fetch fails it
# degrades to a state where the only model it sees is 'auto', and then
# rejects the configured model with:
#     error: Model '<name>' does not exist. Available models: auto
# …writing no output file. That's transient — a fresh attempt usually
# recovers. So we retry the configured model a few times, and if it still
# reports the model unavailable, fall back to --model auto (always present)
# so the run completes instead of surfacing a permanent Failed card.
#
#   run_kiro <label> <model> <output_file> <prompt_file> <log_file>
#     returns 0 iff <output_file> exists and is non-empty after the run.
#
# Tunable: KIRO_MAX_ATTEMPTS (env, default 3) — attempts on the configured
# model before the 'auto' fallback.

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
