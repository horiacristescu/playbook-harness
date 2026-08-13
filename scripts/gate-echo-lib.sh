#!/bin/bash
# gate-echo-lib.sh
# Shared logic for hooks: project root detection + gate parsing.

PLAYBOOK_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# find_project_root
# Walk up from $PWD looking for .agent/tasks/ (legacy) or .agent/<user>/tasks/
# (multi-user) — the definitive playbook marker.
# CLAUDE.md and MIND_MAP.md alone are NOT sufficient — they exist in non-playbook
# projects and would cause hooks to fire where they shouldn't.
# Outputs the project root path, or empty string if not found.
find_project_root() {
    local dir="$PWD"
    while true; do
        # Legacy layout
        if [ -d "$dir/.agent/tasks" ]; then
            echo "$dir"
            return 0
        fi
        # Multi-user layout: .agent/<user>/tasks/
        if [ -d "$dir/.agent" ]; then
            local sub
            for sub in "$dir/.agent"/*/; do
                if [ -d "${sub}tasks" ]; then
                    echo "$dir"
                    return 0
                fi
            done
        fi
        local parent
        parent=$(dirname "$dir")
        if [ "$parent" = "$dir" ]; then
            break
        fi
        dir="$parent"
    done
    echo ""
    return 0  # "not found" communicated via empty output, not exit code (set -e safe)
}

# validate_native_session_id VALUE
# Native IDs become path components and shell environment values. Keep this in
# lockstep with provider.session_identity.validate_native_session_id().
validate_native_session_id() {
    local value="${1:-}"
    [ -n "$value" ] || return 1
    [ "${#value}" -le 200 ] || return 1
    echo "$value" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]*$'
}

# resolve_session_id
# Resolve command identity from native provider variables or the exact
# adapter-normalized transport. Never invent identity from PID/cwd/recency.
resolve_session_id() {
    local provider="${PLAYBOOK_PROVIDER:-}"
    local found="" value="" name=""
    for name in CLAUDE_CODE_SESSION_ID CODEX_THREAD_ID ANTIGRAVITY_CONVERSATION_ID; do
        eval "value=\${$name:-}"
        [ -z "$value" ] && continue
        validate_native_session_id "$value" || {
            echo "Error: malformed provider-native session ID" >&2
            return 1
        }
        if [ -n "$found" ] && [ "$found" != "$value" ]; then
            echo "Error: multiple provider-native session IDs disagree" >&2
            return 1
        fi
        found="$value"
    done

    local candidate=""
    case "$provider" in
        claude) candidate="${CLAUDE_CODE_SESSION_ID:-}" ;;
        codex) candidate="${CODEX_THREAD_ID:-}" ;;
        agy|antigravity) candidate="${ANTIGRAVITY_CONVERSATION_ID:-}" ;;
        pi|omp)
            if [ "${PLAYBOOK_BRIDGE_PROVIDER:-}" != "$provider" ]; then
                echo "Error: $provider normalized identity requires its declared bridge" >&2
                return 1
            fi
            candidate="${PLAYBOOK_SESSION_ID:-}"
            ;;
    esac
    if [ -n "$provider" ]; then
        case "$provider" in
            pi|omp)
                if [ -n "$found" ]; then
                    echo "Error: foreign provider-native session ID leaked into adapter transport" >&2
                    return 1
                fi
                ;;
        esac
        validate_native_session_id "$candidate" || {
            echo "Error: missing or malformed provider-native session ID" >&2
            return 1
        }
        echo "$candidate"
        return
    fi

    if [ -n "$found" ]; then
        echo "$found"
        return
    fi
    if [ "${PLAYBOOK_ROLE:-}" = "noninteractive" ] && \
       validate_native_session_id "${PLAYBOOK_SESSION_ID:-}"; then
        echo "$PLAYBOOK_SESSION_ID"
        return
    fi
    echo "Error: ambient PLAYBOOK_SESSION_ID is not interactive session authority" >&2
    return 1
}

# resolve_hook_session_id PROVIDER PAYLOAD
# Hook payload/context identity is authoritative and deliberately ignores
# ambient PLAYBOOK_SESSION_ID. Outputs one validated native ID.
resolve_hook_session_id() {
    local provider="$1" payload="$2"
    local hook_id
    hook_id=$(printf '%s' "$payload" | python3 -c '
import json, re, sys
provider = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit("Error: malformed hook payload")
field = "conversationId" if provider in ("agy", "antigravity") else "session_id"
value = payload.get(field)
if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value):
    raise SystemExit("Error: missing or malformed native session ID in hook payload")
print(value)
' "$provider") || return 1

    local command_id=""
    case "$provider" in
        claude) command_id="${CLAUDE_CODE_SESSION_ID:-}" ;;
        codex) command_id="${CODEX_THREAD_ID:-}" ;;
        agy|antigravity) command_id="${ANTIGRAVITY_CONVERSATION_ID:-}" ;;
        pi|omp) command_id="${PLAYBOOK_SESSION_ID:-}" ;;
    esac
    if [ -n "$command_id" ]; then
        validate_native_session_id "$command_id" || {
            echo "Error: malformed provider command session ID" >&2
            return 1
        }
        if [ "$hook_id" != "$command_id" ]; then
            echo "Error: hook and command native session IDs disagree" >&2
            return 1
        fi
    fi
    echo "$hook_id"
}

resolve_hook_provider() {
    if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
        echo claude
        return
    fi
    if [ -n "${CODEX_THREAD_ID:-}" ]; then
        echo codex
        return
    fi
    if [ -n "${ANTIGRAVITY_CONVERSATION_ID:-}" ]; then
        echo antigravity
        return
    fi
    case "${PLAYBOOK_BRIDGE_PROVIDER:-}" in
        pi|omp|antigravity) echo "$PLAYBOOK_BRIDGE_PROVIDER" ;;
        *) echo claude ;;
    esac
}

# canonical_session_provider PROVIDER
canonical_session_provider() {
    case "${1:-}" in
        claude|codex|pi|omp) echo "$1" ;;
        agy|antigravity) echo antigravity ;;
        *) echo "Error: provider has no session storage contract" >&2; return 1 ;;
    esac
}

# resolve_session_dir AGENT_DIR PROVIDER SESSION_ID
# Return a path confined beneath AGENT_DIR/sessions. Refuse symlinked session
# roots or entries so a valid provider ID can never redirect hook effects out
# of the Playbook state tree.
resolve_session_dir() {
    local agent_dir="$1" provider session_id="$3"
    provider=$(canonical_session_provider "$2") || return 1
    validate_native_session_id "$session_id" || {
        echo "Error: malformed provider-native session ID" >&2
        return 1
    }
    python3 - "$agent_dir" "$provider" "$session_id" <<'PY'
import os
import sys

agent = os.path.realpath(sys.argv[1])
sessions = os.path.join(sys.argv[1], "sessions")
target = os.path.join(sessions, f"{sys.argv[2]}-{sys.argv[3]}")

if os.path.lexists(sessions):
    if os.path.islink(sessions) or os.path.commonpath((agent, os.path.realpath(sessions))) != agent:
        raise SystemExit("Error: Playbook sessions root escapes agent directory")
if os.path.lexists(target):
    if os.path.islink(target):
        raise SystemExit("Error: Playbook session entry is a symlink")
    root = os.path.realpath(sessions)
    if os.path.commonpath((root, os.path.realpath(target))) != root:
        raise SystemExit("Error: Playbook session entry escapes sessions root")
print(target)
PY
}

# ensure_session_dir AGENT_DIR PROVIDER SESSION_ID
# Publish the shared Python session.json skeleton before any hook-local state.
ensure_session_dir() {
    local agent_dir="$1" provider session_id="$3" lib_dir
    provider=$(canonical_session_provider "$2") || return 1
    validate_native_session_id "$session_id" || return 1
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PYTHONPATH="$lib_dir/../src${PYTHONPATH:+:$PYTHONPATH}" \
        python3 - "$agent_dir" "$provider" "$session_id" <<'PY'
import sys
from pathlib import Path
from provider.session_state import ensure_session_record

record, _ = ensure_session_record(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
print(record.parent)
PY
}

# validate_task_authority AGENT_DIR PROVIDER SESSION_ID
# Echo the authoritative task.md only when current_state and the final
# provider-qualified Sessions entry agree. A missing cache is simply unbound;
# every disagreement is an explicit error carrying both paths/identities.
validate_task_authority() {
    local agent_dir="$1" provider session_id="$3" lib_dir
    provider=$(canonical_session_provider "$2") || return 1
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PYTHONPATH="$lib_dir/../src${PYTHONPATH:+:$PYTHONPATH}" \
        python3 - "$agent_dir" "$provider" "$session_id" <<'PY'
import sys
from pathlib import Path
from provider.session_state import SessionKey
from tasks.task_document import validate_task_claim

agent = Path(sys.argv[1])
key = SessionKey.from_values(sys.argv[2], sys.argv[3])
state = agent / "sessions" / key.directory_name / "current_state"
if not state.exists():
    raise SystemExit(3)
task_number = state.read_text(encoding="utf-8").strip()
print(validate_task_claim(agent, key, task_number))
PY
}

# resolve_agent_dir PROJECT_DIR
# Echoes the agent state directory:
#   absent .agent/current_user  → PROJECT_DIR/.agent        (legacy)
#   valid  .agent/current_user  → PROJECT_DIR/.agent/<user> (multi-user)
#   invalid content             → stderr + exit 1
resolve_agent_dir() {
    local project_dir="$1"
    local agent_root="$project_dir/.agent"
    local marker="$agent_root/current_user"
    if [ -L "$agent_root" ]; then
        echo "Error: project agent directory may not be a symlink: $agent_root" >&2
        return 1
    fi
    if [ -L "$marker" ]; then
        echo "Error: .agent/current_user may not be a symlink: $marker" >&2
        return 1
    fi
    if [ -e "$marker" ] && [ ! -f "$marker" ]; then
        echo "Error: .agent/current_user must be a regular file: $marker" >&2
        return 1
    fi
    if [ ! -f "$marker" ]; then
        echo "$agent_root"
        return 0
    fi
    local name
    name=$(sed 's/^[[:space:]]*//;s/[[:space:]]*$//' "$marker")
    # Validate: non-empty, not . or .., no slash, matches [a-zA-Z0-9][a-zA-Z0-9_.-]*
    if [ -z "$name" ] || [ "$name" = "." ] || [ "$name" = ".." ]; then
        echo "Error: .agent/current_user contains invalid username '${name}'. Must be non-empty and not . or .." >&2
        exit 1
    fi
    case "$name" in
        */*) echo "Error: .agent/current_user contains invalid username '${name}'. Slashes not allowed." >&2; exit 1 ;;
        [a-zA-Z0-9]*) ;;
        *) echo "Error: .agent/current_user contains invalid username '${name}'. Must start with a letter or digit." >&2; exit 1 ;;
    esac
    if ! echo "$name" | grep -qE '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$'; then
        echo "Error: .agent/current_user contains invalid username '${name}'. Use only letters, digits, hyphens, underscores, dots." >&2
        exit 1
    fi
    local selected="$agent_root/$name"
    if [ -L "$selected" ]; then
        echo "Error: selected agent directory may not be a symlink: $selected" >&2
        return 1
    fi
    python3 - "$project_dir" "$selected" <<'PY'
import os
import sys
project = os.path.realpath(sys.argv[1])
agent = os.path.realpath(os.path.join(sys.argv[1], ".agent"))
selected = os.path.realpath(sys.argv[2])
if os.path.commonpath((project, agent)) != project:
    raise SystemExit("Error: Playbook agent directory escapes project")
if os.path.commonpath((agent, selected)) != agent:
    raise SystemExit("Error: selected agent directory escapes .agent")
print(sys.argv[2])
PY
}

# agent_dir_writable PROJECT_DIR
# Returns 0 if the resolved agent dir exists and is writable, 1 otherwise.
# Use this before any hook that writes to .agent/ — in sandbox mode
# the directory may exist but be read-only.
agent_dir_writable() {
    local agent_dir
    agent_dir=$(resolve_agent_dir "$1")
    [ -d "$agent_dir" ] && [ -w "$agent_dir" ]
}

# get_gate_info TASK_FILE
# Outputs: done_count total_count gate_line gate_text
# If all done: gate_line and gate_text are empty
get_gate_info() {
    local task_file="$1"

    if [ ! -f "$task_file" ]; then
        echo "0 0 0 ''"
        return 1
    fi

    # Count total and done checkboxes (only at line start, not in backticks)
    # Pattern: only match [ ], [x], [X] — not [8] or [40] (reference links)
    local total
    total=$(grep -cE '^[[:space:]]*- \[( |x|X)\]' "$task_file" 2>/dev/null) || total=0
    local done
    done=$(grep -cE '^[[:space:]]*- \[[xX]\]' "$task_file" 2>/dev/null) || done=0

    # Find first unchecked gate
    local gate_line=""
    local gate_text=""

    while IFS= read -r line; do
        local lineno="${line%%:*}"
        local content="${line#*:}"
        if echo "$content" | grep -qE '^[[:space:]]*- \[ \]'; then
            gate_line="$lineno"
            gate_text=$(echo "$content" | sed 's/^[[:space:]]*- \[ \] *//')
            break
        fi
    done < <(grep -nE '^[[:space:]]*- \[ \]' "$task_file" 2>/dev/null)

    echo "$done $total $gate_line $gate_text"
}

# read_counter FILE KEY
# Read a key=value from the counter file. Outputs the value, or empty if missing.
read_counter() {
    local file="$1" key="$2"
    if [ -f "$file" ]; then
        sed -n "s/^${key}=//p" "$file" 2>/dev/null | head -1
    fi
}

# write_counter FILE KEY VALUE
# Set a key=value in the counter file. Creates file if missing, updates in-place if key exists.
# Uses grep-filter-append instead of sed to avoid delimiter collisions with gate text
# containing |, backticks, or other special characters.
write_counter() {
    local file="$1" key="$2" value="$3"
    local tmp="${file}.tmp.$$"
    if [ -f "$file" ]; then
        grep -v "^${key}=" "$file" > "$tmp" 2>/dev/null || true
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    mv "$tmp" "$file"
}

# reset_counters FILE
# Reset tools=0 and writes=0, preserving gate_* fields. Creates file if missing.
reset_counters() {
    local file="$1"
    if [ -f "$file" ]; then
        # Preserve gate_* lines, reset tools/writes
        local gate_lines
        gate_lines=$(grep '^gate_' "$file" 2>/dev/null || true)
        printf 'tools=0\nwrites=0\n' > "$file"
        if [ -n "$gate_lines" ]; then
            echo "$gate_lines" >> "$file"
        fi
    else
        printf 'tools=0\nwrites=0\n' > "$file"
    fi
}

# format_context TASK_NUM DONE TOTAL GATE_TEXT GATE_LINE REL_PATH
# Outputs the formatted context string for the hook
format_context() {
    local task_num="$1"
    local done="$2"
    local total="$3"
    local gate_text="$4"
    local gate_line="$5"
    local rel_path="$6"

    if [ -z "$gate_line" ]; then
        echo "# [${task_num}] — all gates done. Stay for follow-up. Auto-closes on task switch."
        return
    fi
    python3 - "$task_num" "$done" "$total" "$gate_text" "$gate_line" "$rel_path" <<'PY'
import sys

task, done, total, gate, line, path = sys.argv[1:]
prefix = f"# Working on task [{task}] gate ({done}/{total}) -> [ ] "
route = f"\n# Full gate: {path}:{line}"
limit = 520
available = max(1, limit - len(prefix) - len(route))
if len(gate) > available:
    gate = gate[: max(0, available - 1)] + "…"
print(prefix + gate + route)
PY
}

# Bound the complete hook injection after transition warnings and nudges are
# added. Python slicing is Unicode-codepoint safe. Stable identity, progress,
# gate prefix, and the authoritative task.md route occur before optional tails.
bound_gate_context() {
    printf '%s' "$1" | python3 -c '
import sys
text = sys.stdin.read()
limit = 800
print(text if len(text) <= limit else text[:limit - 1] + "…", end="")
'
}

# create_wrapper PROJECT_DIR WRAPPER_NAME
# Creates .claude/bin/<WRAPPER_NAME> as a wrapper pinned to this runtime's
# canonical scripts/<WRAPPER_NAME> path.
# - Skips if file exists without "# playbook-managed" marker (custom wrapper)
# - Overwrites if file has the marker (stale playbook wrapper)
# - Creates .claude/bin/ directory if needed
create_wrapper() {
    local project_dir="$1"
    local wrapper_name="$2"
    local wrapper_path="$project_dir/.claude/bin/$wrapper_name"
    local runtime_root
    runtime_root="$(cd "$PLAYBOOK_SCRIPTS_DIR/.." && pwd -P)"
    local public_name
    case "$wrapper_name" in
        tasks) public_name="pb-tasks" ;;
        sandbox) public_name="pb-sandbox" ;;
        playbook-codex) public_name="pb-codex" ;;
        playbook-agy) public_name="pb-agy" ;;
        playbook-pi) public_name="pb-pi" ;;
        monitor) public_name="pb-monitor" ;;
        *) return 1 ;;
    esac
    local runtime_script="$runtime_root/bin/$public_name"

    # Public candidates always have canonical pb-* files. Contributors keep a
    # bounded fallback to the pre-cutover source names in the development tree.
    if [ ! -x "$runtime_script" ]; then
        case "$wrapper_name" in
            tasks) runtime_script="$runtime_root/bin/tasks" ;;
            sandbox) runtime_script="$runtime_root/scripts/sandbox" ;;
            playbook-codex) runtime_script="$runtime_root/bin/playbook-codex" ;;
            playbook-agy) runtime_script="$runtime_root/bin/playbook-agy" ;;
            playbook-pi) runtime_script="$runtime_root/bin/playbook-pi" ;;
            monitor) runtime_script="$runtime_root/scripts/monitor" ;;
        esac
    fi

    [ -x "$runtime_script" ] || return 1

    # Skip custom wrappers (no playbook-managed marker)
    # Empty files are NOT custom — overwrite them (self-healing)
    if [ -f "$wrapper_path" ] && [ -s "$wrapper_path" ]; then
        if ! grep -q '# playbook-managed' "$wrapper_path" 2>/dev/null; then
            return 0
        fi
    fi

    mkdir -p "$project_dir/.claude/bin"

    {
        echo '#!/bin/bash'
        echo '# playbook-managed — do not edit; regenerated by Playbook Harness'
        printf 'exec %q "$@"\n' "$runtime_script"
    } > "$wrapper_path"
    chmod +x "$wrapper_path"
}
