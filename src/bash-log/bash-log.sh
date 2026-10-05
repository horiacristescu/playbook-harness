# Playbook Harness: project-scoped command logging (bash)
# Sourced via BASH_ENV — logs commands in .agent/ projects to .agent/bash_history
# Purpose: forensic post-mortem record ("what did the agent actually run?")

# This function runs as a DEBUG trap, so its exit status is the trap's exit
# status. A bare `return` reuses $? from the command that ran just before the
# trap fired. Under `set -e` (every Playbook hook script), a filtered command
# such as `[ -n "$X" ]` following a failed `if` test made the trap return 1 and
# bash exited the hook silently with status 1: no stdout, no stderr. Claude Code
# rendered that as "PostToolUse hook error / No stderr output" on every tool
# call. Every exit path below must return 0. Regression test: tests/test_gate_echo.sh.
_cpb_log_cmd() {
    # Filter shell internals and CC infrastructure noise
    case "$BASH_COMMAND" in
        *shell-snapshots*|"pwd -P"*|"case \$- in"*|return|"[["*) return 0 ;;
        "[ -d "*|"[ -f "*|"[ -n "*|"[ -z "*|"[ ! "*) return 0 ;;
        HIST*=*|PATH=*|"set -o"*|"shopt "*|"trap "*|"export PATH"*) return 0 ;;
        source*|.) return 0 ;;
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
    return 0
}
set -o history
trap '_cpb_log_cmd' DEBUG
