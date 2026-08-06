# Playbook Harness: project-scoped command logging (zsh)
# Sourced from ~/.zshenv — logs non-interactive commands to .agent/bash_history
# Purpose: forensic post-mortem record ("what did the agent actually run?")

# Only log non-interactive shells (agent invocations, not user terminals)
if [[ $- == *i* || -z "$ZSH_EXECUTION_STRING" ]]; then
    return 0 2>/dev/null || true
fi

# Walk up from $PWD looking for .agent/ directory
_cpb_log_dir="$PWD"
while [[ "$_cpb_log_dir" != "/" ]]; do
    if [[ -d "$_cpb_log_dir/.agent" ]]; then
        # Extract actual command from CC's eval wrapper:
        #   ... && eval 'CMD' ... && pwd -P >| TMPFILE   (single quotes)
        #   ... && eval "CMD" ... && pwd -P >| TMPFILE   (double quotes, when CMD has single quotes)
        # Anchor on '&& pwd -P >|' (always present, never in user commands)
        if [[ "$ZSH_EXECUTION_STRING" == *"eval '"*"&& pwd -P >|"* ]]; then
            _cpb_log_cmd="${ZSH_EXECUTION_STRING#*eval \'}"
            _cpb_log_cmd="${_cpb_log_cmd%\' *&& pwd -P >|*}"
        elif [[ "$ZSH_EXECUTION_STRING" == *'eval "'*"&& pwd -P >|"* ]]; then
            _cpb_log_cmd="${ZSH_EXECUTION_STRING#*eval \"}"
            _cpb_log_cmd="${_cpb_log_cmd%\" *&& pwd -P >|*}"
        else
            _cpb_log_cmd="$ZSH_EXECUTION_STRING"
        fi
        _cpb_log_cmd="${_cpb_log_cmd//$'\n'/\\n}"
        print -r -- "$(date '+%Y-%m-%d %H:%M:%S') | AGENT | $_cpb_log_cmd" >> "$_cpb_log_dir/.agent/bash_history"
        break
    fi
    _cpb_log_dir="${_cpb_log_dir:h}"
done
unset _cpb_log_dir _cpb_log_cmd
