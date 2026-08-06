# Playbook Harness: project-scoped command logging (bash)
# Sourced via BASH_ENV — logs commands in .agent/ projects to .agent/bash_history
# Purpose: forensic post-mortem record ("what did the agent actually run?")

_cpb_log_cmd() {
    # Filter shell internals and CC infrastructure noise
    case "$BASH_COMMAND" in
        *shell-snapshots*|"pwd -P"*|"case \$- in"*|return|"[["*) return ;;
        "[ -d "*|"[ -f "*|"[ -n "*|"[ -z "*|"[ ! "*) return ;;
        HIST*=*|PATH=*|"set -o"*|"shopt "*|"trap "*|"export PATH"*) return ;;
        source*|.) return ;;
    esac

    # Walk up from $PWD looking for .agent/ directory
    local _dir="$PWD"
    while [[ "$_dir" != "/" ]]; do
        if [[ -d "$_dir/.agent" ]]; then
            local _cmd="${BASH_COMMAND//$'\n'/\\n}"
            echo "$(date '+%Y-%m-%d %H:%M:%S') | AGENT | $_cmd" >> "$_dir/.agent/bash_history"
            break
        fi
        _dir="$(dirname "$_dir")"
    done
}
set -o history
trap '_cpb_log_cmd' DEBUG
