#!/usr/bin/env bash
# Playbook Harness machine installer.
#
#   curl -fsSL https://raw.githubusercontent.com/horiacristescu/playbook-harness/main/install.sh | bash
#
# Or, from a trusted public clone:
#
#   git clone https://github.com/horiacristescu/playbook-harness.git
#   cd playbook-harness
#   bash install.sh

set -euo pipefail

# Git honors the caller's umask when creating checkout files. Keep installed
# modes identical to the signed artifact manifest on every supported system.
umask 022

CANONICAL_REPO="https://github.com/horiacristescu/playbook-harness.git"
INSTALL_DIR="${PLAYBOOK_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/playbook-harness}"
BIN_DIR="${PLAYBOOK_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
REPO_URL="$CANONICAL_REPO"
REF="main"
TEST_SOURCE=false
REPAIR_LAUNCHERS=false
OPERATION="install"
LOCK_DIR=""
LOCK_PREPARED=""
STAGING_DIR=""
RECOVERY_DIR=""
DISCARD_DIR=""
TRANSACTION_FILE=""
LOCK_OWNED=false
SWAP_ACTIVE=false
TRANSACTION_OWNED=false
PB_COMMANDS="pb-tasks pb-sandbox pb-codex pb-agy pb-pi"
SHIM_MARKER_PREFIX="# playbook-harness-managed-shim schema=1 root="
SHIM_MARKER=""

usage() {
  cat <<'EOF'
Usage: install.sh

Installs one Playbook Harness runtime and the namespaced pb-* commands.

Environment overrides:
  PLAYBOOK_INSTALL_DIR   Runtime checkout (default: ~/.local/share/playbook-harness)
  PLAYBOOK_BIN_DIR       Global command directory (default: ~/.local/bin)

The installer never initializes the current directory. After installation run:
  cd <project> && pb-tasks init

Lifecycle:
  --repair-launchers    Validate the installed runtime and repair managed pb-* shims
  --upgrade             Clean `git pull --ff-only origin main`, audit, refresh shims
  --reinstall           Replace the runtime from a fully audited sibling checkout
  --uninstall           Remove the managed runtime and marker-owned pb-* shims
EOF
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$SWAP_ACTIVE" = true ] && [ -d "$RECOVERY_DIR" ]; then
    if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
      rm -rf -- "$INSTALL_DIR"
    fi
    if mv "$RECOVERY_DIR" "$INSTALL_DIR" 2>/dev/null; then
      if [ "$TRANSACTION_OWNED" = true ]; then
        rm -f -- "$TRANSACTION_FILE"
        TRANSACTION_OWNED=false
      fi
    fi
  fi
  if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
    rm -rf -- "$STAGING_DIR"
  fi
  if [ "$LOCK_OWNED" = true ] && [ -n "$LOCK_DIR" ] && [ -f "$LOCK_DIR" ] \
    && [ "$(sed -n 's/^pid=//p' "$LOCK_DIR" 2>/dev/null || true)" = "$$" ] \
    && [ "$(sed -n 's/^target=//p' "$LOCK_DIR" 2>/dev/null || true)" = "$INSTALL_DIR" ]; then
    rm -f -- "$LOCK_DIR"
  fi
  if [ -n "$LOCK_PREPARED" ]; then
    rm -f -- "$LOCK_PREPARED" 2>/dev/null || true
  fi
  for command in $PB_COMMANDS; do
    rm -f -- "$BIN_DIR/${command}.tmp.$$" 2>/dev/null || true
  done
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

set_operation() {
  requested=$1
  [ "$OPERATION" = "install" ] \
    || die "lifecycle operations cannot be combined: $OPERATION and $requested"
  OPERATION=$requested
}

# --repo/--ref are deliberately undocumented hermetic-test seams.
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-dir) [ "$#" -ge 2 ] || die "--install-dir requires a directory"; INSTALL_DIR=$2; shift 2 ;;
    --bin-dir) [ "$#" -ge 2 ] || die "--bin-dir requires a directory"; BIN_DIR=$2; shift 2 ;;
    --repo) [ "$#" -ge 2 ] || die "--repo requires a URL or path"; REPO_URL=$2; TEST_SOURCE=true; shift 2 ;;
    --ref) [ "$#" -ge 2 ] || die "--ref requires a branch or tag"; REF=$2; TEST_SOURCE=true; shift 2 ;;
    --repair-launchers) REPAIR_LAUNCHERS=true; shift ;;
    --upgrade) set_operation upgrade; shift ;;
    --reinstall) set_operation reinstall; shift ;;
    --uninstall) set_operation uninstall; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required; install Git with your platform package manager"
command -v python3 >/dev/null 2>&1 || die "python3 >= 3.10 is required; install Python with your platform package manager"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "python3 >= 3.10 is required"

canonical_path() {
  python3 - "$1" <<'PY'
import os, sys
print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))
PY
}

reject_symlink_ancestry() {
  python3 - "$1" <<'PY'
import os, pathlib, sys
path = pathlib.Path(os.path.abspath(os.path.expanduser(sys.argv[1])))
for candidate in (path, *path.parents):
    if candidate.is_symlink():
        print(candidate)
        raise SystemExit(1)
PY
}

RAW_INSTALL_DIR=$INSTALL_DIR
RAW_BIN_DIR=$BIN_DIR
reject_symlink_ancestry "$RAW_INSTALL_DIR" || die "install path has symlink ancestry: $RAW_INSTALL_DIR"
reject_symlink_ancestry "$RAW_BIN_DIR" || die "command path has symlink ancestry: $RAW_BIN_DIR"
INSTALL_DIR=$(canonical_path "$RAW_INSTALL_DIR")
BIN_DIR=$(canonical_path "$RAW_BIN_DIR")
SHIM_MARKER="${SHIM_MARKER_PREFIX}${INSTALL_DIR}"
RECOVERY_DIR="${INSTALL_DIR}.recovery"
DISCARD_DIR="${INSTALL_DIR}.discard"
TRANSACTION_FILE="${INSTALL_DIR}.reinstall-state"
[ "$INSTALL_DIR" != "/" ] || die "refusing broad install destination: /"
[ "$BIN_DIR" != "/" ] || die "refusing broad command destination: /"
case "$BIN_DIR/" in
  "$INSTALL_DIR/"*) die "command directory must not equal or descend from the runtime: $BIN_DIR" ;;
esac

SCRIPT_SOURCE=""
ON_DISK_GIT_ROOT=""
case "${BASH_SOURCE[0]:-}" in
  ""|/dev/*) ;;
  *)
    if [ -f "${BASH_SOURCE[0]}" ]; then
      script_path=$(canonical_path "${BASH_SOURCE[0]}")
      script_root=$(git -C "$(dirname "$script_path")" rev-parse --show-toplevel 2>/dev/null || true)
      ON_DISK_GIT_ROOT=$script_root
      if [ -n "$script_root" ] && [ "$script_path" = "$(canonical_path "$script_root/install.sh")" ] \
        && [ -f "$script_root/.playbook-artifact.json" ]; then
        SCRIPT_SOURCE=$(canonical_path "$script_root")
      fi
    fi
    ;;
esac

if [ -n "$ON_DISK_GIT_ROOT" ] && [ -z "$SCRIPT_SOURCE" ] && [ "$TEST_SOURCE" = false ]; then
  die "on-disk installer is not inside an audited Playbook Harness public checkout"
fi

self_audit() {
  root=$1
  [ -x "$root/bin/pb-tasks" ] || return 1
  "$root/bin/pb-tasks" runtime-audit >/dev/null
}

clean_git_tree() {
  [ -z "$(git -C "$1" status --porcelain --untracked-files=normal)" ]
}

remote_matches() {
  actual=$(git -C "$1" config --get remote.origin.url 2>/dev/null || true)
  [ "$actual" = "$REPO_URL" ] && return 0
  if [ "$TEST_SOURCE" = false ]; then
    [ "$actual" = "$CANONICAL_REPO" ] || [ "$actual" = "${CANONICAL_REPO%.git}" ] \
      || [ "$actual" = "git@github.com:horiacristescu/playbook-harness.git" ]
    return
  fi
  return 1
}

managed_install_identity() {
  [ -d "$1/.git" ] && [ -f "$1/.playbook-artifact.json" ] \
    && remote_matches "$1"
}

acquire_lock() {
  operation=$1
  if [ "$LOCK_OWNED" = true ]; then
    return
  fi
  LOCK_DIR="${INSTALL_DIR}.lock"
  LOCK_PREPARED="${LOCK_DIR}.prepare.$$"
  [ ! -e "$LOCK_PREPARED" ] && [ ! -L "$LOCK_PREPARED" ] \
    || die "lifecycle lock preparation path exists: $LOCK_PREPARED"
  {
    printf 'schema=1\n'
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$(hostname 2>/dev/null || printf unknown)"
    printf 'operation=%s\n' "$operation"
    printf 'target=%s\n' "$INSTALL_DIR"
    printf 'started=%s\n' "$(date +%s)"
  } > "$LOCK_PREPARED"
  test_fail_at before_lock_publish
  if ! ln "$LOCK_PREPARED" "$LOCK_DIR" 2>/dev/null; then
    [ ! -L "$LOCK_DIR" ] && [ -f "$LOCK_DIR" ] \
      || die "foreign or malformed lifecycle lock: $LOCK_DIR"
    owner_schema=$(sed -n 's/^schema=//p' "$LOCK_DIR")
    owner_pid=$(sed -n 's/^pid=//p' "$LOCK_DIR")
    owner_host=$(sed -n 's/^host=//p' "$LOCK_DIR")
    owner_target=$(sed -n 's/^target=//p' "$LOCK_DIR")
    this_host=$(hostname 2>/dev/null || printf unknown)
    [ "$owner_schema" = "1" ] \
      || die "foreign or malformed lifecycle lock: $LOCK_DIR"
    case "$owner_pid" in ""|*[!0-9]*) die "foreign or malformed lifecycle lock: $LOCK_DIR" ;; esac
    [ "$owner_target" = "$INSTALL_DIR" ] \
      || die "lifecycle lock targets another installation: $LOCK_DIR"
    [ "$owner_host" = "$this_host" ] \
      || die "lifecycle lock belongs to another host: $LOCK_DIR"
    if kill -0 "$owner_pid" 2>/dev/null; then
      die "another Playbook Harness lifecycle operation holds: $LOCK_DIR (pid $owner_pid)"
    fi
    rm -f -- "$LOCK_DIR"
    ln "$LOCK_PREPARED" "$LOCK_DIR" 2>/dev/null \
      || die "could not recover stale lifecycle lock: $LOCK_DIR"
  fi
  LOCK_OWNED=true
  rm -f -- "$LOCK_PREPARED"
  LOCK_PREPARED=""
  test_fail_at after_lock_publish
}

test_fail_at() {
  point=$1
  if [ "$TEST_SOURCE" = true ] && [ "${PLAYBOOK_TEST_FAIL_AT:-}" = "$point" ]; then
    die "injected lifecycle failure at $point"
  fi
}

write_reinstall_state() {
  [ ! -e "$TRANSACTION_FILE" ] && [ ! -L "$TRANSACTION_FILE" ] \
    || die "reinstall state path already exists: $TRANSACTION_FILE"
  temporary="${TRANSACTION_FILE}.tmp.$$"
  {
    printf 'schema=1\n'
    printf 'target=%s\n' "$INSTALL_DIR"
    printf 'recovery=%s\n' "$RECOVERY_DIR"
    printf 'discard=%s\n' "$DISCARD_DIR"
  } > "$temporary"
  mv "$temporary" "$TRANSACTION_FILE"
  TRANSACTION_OWNED=true
}

valid_reinstall_state() {
  [ -f "$TRANSACTION_FILE" ] && [ ! -L "$TRANSACTION_FILE" ] || return 1
  [ "$(sed -n 's/^schema=//p' "$TRANSACTION_FILE")" = "1" ] \
    && [ "$(sed -n 's/^target=//p' "$TRANSACTION_FILE")" = "$INSTALL_DIR" ] \
    && [ "$(sed -n 's/^recovery=//p' "$TRANSACTION_FILE")" = "$RECOVERY_DIR" ] \
    && [ "$(sed -n 's/^discard=//p' "$TRANSACTION_FILE")" = "$DISCARD_DIR" ]
}

preflight_shims() {
  for command in $PB_COMMANDS; do
    target="$BIN_DIR/$command"
    if [ -L "$target" ]; then
      die "refusing symlink launcher collision: $target"
    fi
    if [ -e "$target" ]; then
      [ -f "$target" ] || die "refusing non-file launcher collision: $target"
      first_line=$(sed -n '2p' "$target" 2>/dev/null || true)
      [ "$first_line" = "$SHIM_MARKER" ] \
        || die "refusing to overwrite non-Playbook launcher: $target"
    fi
  done
}

write_shims() {
  preflight_shims
  mkdir -p "$BIN_DIR"
  for command in $PB_COMMANDS; do
    target="$BIN_DIR/$command"
    runtime_command="$INSTALL_DIR/bin/$command"
    [ -x "$runtime_command" ] || die "installed command is missing: $runtime_command"
    temporary="$target.tmp.$$"
    quoted=$(printf '%q' "$runtime_command")
    {
      printf '#!/usr/bin/env bash\n'
      printf '%s\n' "$SHIM_MARKER"
      printf 'exec %s "$@"\n' "$quoted"
    } > "$temporary"
    chmod 0755 "$temporary"
    mv "$temporary" "$target"
  done
}

remove_owned_shims() {
  for command in $PB_COMMANDS; do
    target="$BIN_DIR/$command"
    if [ -L "$target" ] || [ -d "$target" ]; then
      printf 'Warning: preserving foreign launcher path %s\n' "$target"
    elif [ -f "$target" ]; then
      marker=$(sed -n '2p' "$target" 2>/dev/null || true)
      if [ "$marker" = "$SHIM_MARKER" ]; then
        rm -f -- "$target"
      else
        printf 'Warning: preserving foreign launcher %s\n' "$target"
      fi
    fi
  done
}

recover_interrupted_reinstall() {
  has_recovery=false
  has_discard=false
  has_state=false
  if [ -e "$RECOVERY_DIR" ] || [ -L "$RECOVERY_DIR" ]; then has_recovery=true; fi
  if [ -e "$DISCARD_DIR" ] || [ -L "$DISCARD_DIR" ]; then has_discard=true; fi
  if [ -e "$TRANSACTION_FILE" ] || [ -L "$TRANSACTION_FILE" ]; then has_state=true; fi
  if [ "$has_recovery" = false ] && [ "$has_discard" = false ] && [ "$has_state" = false ]; then
    return 0
  fi
  valid_reinstall_state \
    || die "foreign or malformed reinstall transaction state: $TRANSACTION_FILE"
  if [ "$has_discard" = true ]; then
    [ -d "$DISCARD_DIR" ] && [ ! -L "$DISCARD_DIR" ] \
      || die "reinstall discard path is not a real directory: $DISCARD_DIR"
    [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] \
      && managed_install_identity "$INSTALL_DIR" && self_audit "$INSTALL_DIR" \
      || die "cannot clean interrupted reinstall discard without a valid logical runtime"
    acquire_lock recover-reinstall
    rm -rf -- "$DISCARD_DIR"
    rm -f -- "$TRANSACTION_FILE"
    printf 'Cleaned completed Playbook Harness reinstall state at %s\n' "$INSTALL_DIR"
    has_discard=false
    has_state=false
  fi
  if [ "$has_recovery" = false ]; then
    if [ "$has_state" = true ] && [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] \
      && managed_install_identity "$INSTALL_DIR" && self_audit "$INSTALL_DIR"; then
      acquire_lock recover-reinstall
      rm -f -- "$TRANSACTION_FILE"
      printf 'Cleaned completed Playbook Harness reinstall state at %s\n' "$INSTALL_DIR"
      return
    fi
    return
  fi
  [ -d "$RECOVERY_DIR" ] && [ ! -L "$RECOVERY_DIR" ] \
    || die "reinstall recovery path is not a real directory: $RECOVERY_DIR"
  managed_install_identity "$RECOVERY_DIR" && self_audit "$RECOVERY_DIR" \
    || die "reinstall recovery checkout is not an authenticated audited runtime: $RECOVERY_DIR"
  acquire_lock recover-reinstall
  preflight_shims
  if [ ! -e "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ]; then
    mv "$RECOVERY_DIR" "$INSTALL_DIR"
    write_shims
    rm -f -- "$TRANSACTION_FILE"
    printf 'Recovered interrupted Playbook Harness reinstall at %s\n' "$INSTALL_DIR"
    return
  fi
  [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] \
    && managed_install_identity "$INSTALL_DIR" \
    || die "reinstall recovery found a foreign logical target: $INSTALL_DIR"
  if self_audit "$INSTALL_DIR"; then
    write_shims
    mv "$RECOVERY_DIR" "$DISCARD_DIR"
    rm -rf -- "$DISCARD_DIR"
    rm -f -- "$TRANSACTION_FILE"
    printf 'Completed interrupted Playbook Harness reinstall at %s\n' "$INSTALL_DIR"
    return
  fi
  rm -rf -- "$INSTALL_DIR"
  mv "$RECOVERY_DIR" "$INSTALL_DIR"
  write_shims
  rm -f -- "$TRANSACTION_FILE"
  printf 'Rolled back interrupted Playbook Harness reinstall at %s\n' "$INSTALL_DIR"
}

report_agents_and_path() {
  printf '\nDetected agents\n'
  for pair in "Claude:claude" "Codex:codex" "OMP:omp" "Antigravity:agy" "Pi:pi"; do
    label=${pair%%:*}
    binary=${pair#*:}
    if command -v "$binary" >/dev/null 2>&1; then
      printf '  %-14s installed\n' "$label"
    else
      printf '  %-14s not installed\n' "$label"
    fi
  done
  case ":$PATH:" in
    *":$BIN_DIR:"*)
      resolved=$(command -v pb-tasks 2>/dev/null || true)
      if [ -n "$resolved" ] && [ "$(canonical_path "$resolved")" != "$BIN_DIR/pb-tasks" ]; then
        printf '\nWarning: pb-tasks is shadowed by %s; place %s earlier in PATH.\n' "$resolved" "$BIN_DIR"
      fi
      ;;
    *) printf '\nAdd %s to PATH to use the pb-* commands.\n' "$BIN_DIR" ;;
  esac
}

recover_interrupted_reinstall

if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
  [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] \
    || die "install destination is not a real directory: $INSTALL_DIR"
  managed_install_identity "$INSTALL_DIR" \
    || die "install destination exists but is not an authenticated Playbook Harness checkout: $INSTALL_DIR"
  if [ "$OPERATION" != "install" ] && [ "$REPAIR_LAUNCHERS" = true ]; then
    die "--$OPERATION and --repair-launchers cannot be combined"
  fi
  if [ "$OPERATION" = "upgrade" ]; then
    branch=$(git -C "$INSTALL_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    [ "$branch" = "main" ] || die "upgrade requires the managed checkout on main (found ${branch:-detached})"
    upstream=$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
    [ "$upstream" = "origin/main" ] || die "upgrade requires upstream origin/main (found ${upstream:-none})"
    clean_git_tree "$INSTALL_DIR" || die "upgrade requires a clean worktree (tracked and nonignored untracked files)"
    self_audit "$INSTALL_DIR" || die "pre-upgrade runtime audit failed; run an explicit validated reinstall"
    preflight_shims
    acquire_lock upgrade
    printf 'Warning: running agent sessions may observe files changing during this in-place pull.\n'
    git -C "$INSTALL_DIR" pull --ff-only origin main \
      || die "fast-forward upgrade failed; the checkout was not reset or rewritten"
    self_audit "$INSTALL_DIR" \
      || die "post-upgrade runtime audit failed; run an explicit validated reinstall"
    write_shims
    commit=$(git -C "$INSTALL_DIR" rev-parse HEAD)
    dirty=$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=normal)
    printf 'Playbook Harness upgraded\n  Runtime     %s\n  Branch      main\n  Commit      %s\n  Dirty       %s\n' \
      "$INSTALL_DIR" "$commit" "${dirty:-no}"
    printf 'Rerun pb-tasks init in active projects when copied provider artifacts changed.\n'
    exit 0
  fi
  if [ "$OPERATION" = "reinstall" ]; then
    current_remote=$(git -C "$INSTALL_DIR" config --get remote.origin.url 2>/dev/null || true)
    current_commit=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)
    current_dirty=$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=normal 2>/dev/null || true)
    printf 'Reinstalling Playbook Harness\n  Remote      %s\n  Current     %s\n  Dirty       %s\n' \
      "$current_remote" "$current_commit" "${current_dirty:-no}"
    preflight_shims
    acquire_lock reinstall
    STAGING_DIR="${INSTALL_DIR}.candidate.$$"
    [ ! -e "$STAGING_DIR" ] && [ ! -L "$STAGING_DIR" ] \
      || die "reinstall candidate path already exists: $STAGING_DIR"
    git clone --branch "$REF" "$REPO_URL" "$STAGING_DIR" >/dev/null 2>&1 \
      || die "could not clone reinstall candidate from $REPO_URL"
    managed_install_identity "$STAGING_DIR" \
      || die "reinstall candidate does not match the accepted remote"
    self_audit "$STAGING_DIR" \
      || die "reinstall candidate failed its installed-tree audit"
    test_fail_at after_candidate_audit
    [ ! -e "$RECOVERY_DIR" ] && [ ! -L "$RECOVERY_DIR" ] \
      || die "reinstall recovery path already exists: $RECOVERY_DIR"
    write_reinstall_state
    SWAP_ACTIVE=true
    mv "$INSTALL_DIR" "$RECOVERY_DIR"
    test_fail_at after_target_to_recovery
    mv "$STAGING_DIR" "$INSTALL_DIR"
    STAGING_DIR=""
    test_fail_at after_candidate_to_target
    self_audit "$INSTALL_DIR" \
      || die "replacement runtime failed its post-swap audit"
    test_fail_at after_post_swap_audit
    write_shims
    test_fail_at after_shim_refresh
    [ ! -e "$DISCARD_DIR" ] && [ ! -L "$DISCARD_DIR" ] \
      || die "reinstall discard path already exists: $DISCARD_DIR"
    mv "$RECOVERY_DIR" "$DISCARD_DIR"
    SWAP_ACTIVE=false
    test_fail_at after_recovery_to_discard
    rm -rf -- "$DISCARD_DIR"
    rm -f -- "$TRANSACTION_FILE"
    TRANSACTION_OWNED=false
    commit=$(git -C "$INSTALL_DIR" rev-parse HEAD)
    printf 'Playbook Harness reinstalled\n  Runtime     %s\n  Commit      %s\n' "$INSTALL_DIR" "$commit"
    report_agents_and_path
    printf '\nRerun pb-tasks init in active projects when copied provider artifacts changed.\n'
    exit 0
  fi
  if [ "$OPERATION" = "uninstall" ]; then
    self_audit "$INSTALL_DIR" \
      || die "uninstall requires an authenticated runtime that passes its installed-tree audit"
    acquire_lock uninstall
    remove_owned_shims
    rm -rf -- "$INSTALL_DIR"
    printf 'Playbook Harness uninstalled from %s\n' "$INSTALL_DIR"
    printf 'Project-local Playbook files and hooks were preserved; remove them explicitly per project if desired.\n'
    exit 0
  fi
  if [ "$REPAIR_LAUNCHERS" = true ]; then
    self_audit "$INSTALL_DIR" || die "installed runtime audit failed; run an explicit validated reinstall"
    write_shims
    printf 'Repaired Playbook Harness commands in %s\n' "$BIN_DIR"
    report_agents_and_path
    exit 0
  fi
  self_audit "$INSTALL_DIR" || die "installed runtime audit failed; run an explicit validated reinstall"
  printf 'Playbook Harness is already installed at %s\n' "$INSTALL_DIR"
  printf 'Upgrade: bash %s/install.sh --upgrade\n' "$INSTALL_DIR"
  printf 'Reinstall: bash %s/install.sh --reinstall\n' "$INSTALL_DIR"
  exit 0
fi

[ "$OPERATION" = "install" ] \
  || die "cannot $OPERATION because no valid installation exists at $INSTALL_DIR"

[ "$REPAIR_LAUNCHERS" = false ] \
  || die "cannot repair launchers because no valid installation exists at $INSTALL_DIR"

if [ -n "$SCRIPT_SOURCE" ]; then
  clean_git_tree "$SCRIPT_SOURCE" || die "local source checkout is dirty: $SCRIPT_SOURCE"
  # A caller's umask may make a clean Git worktree group-writable. Trust the
  # committed object database here; the normalized staging clone below receives
  # the exact manifest audit before it can become the installed runtime.
  source_branch=$(git -C "$SCRIPT_SOURCE" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
  [ "$source_branch" = "$REF" ] \
    || die "local source checkout must be on $REF (found ${source_branch:-detached})"
  if [ "$TEST_SOURCE" = false ]; then
    source_remote=$(git -C "$SCRIPT_SOURCE" config --get remote.origin.url 2>/dev/null || true)
    case "$source_remote" in
      "$CANONICAL_REPO"|"${CANONICAL_REPO%.git}"|git@github.com:horiacristescu/playbook-harness.git) ;;
      *) die "local source checkout has an unrecognized remote: $source_remote" ;;
    esac
  fi
  CLONE_SOURCE=$SCRIPT_SOURCE
  SOURCE_COMMIT=$(git -C "$SCRIPT_SOURCE" rev-parse HEAD)
else
  CLONE_SOURCE=$REPO_URL
  SOURCE_COMMIT=""
fi

# Set-wide launcher collision preflight happens before clone/lock mutation.
preflight_shims

parent_dir=$(dirname "$INSTALL_DIR")
mkdir -p "$parent_dir"
acquire_lock install

STAGING_DIR="${INSTALL_DIR}.install.$$"
[ ! -e "$STAGING_DIR" ] || die "staging path already exists: $STAGING_DIR"
printf 'Installing Playbook Harness\n  Runtime     %s\n' "$INSTALL_DIR"
if [ -n "$SOURCE_COMMIT" ]; then
  git clone --no-hardlinks --branch "$REF" "$CLONE_SOURCE" "$STAGING_DIR" >/dev/null 2>&1
  [ "$(git -C "$STAGING_DIR" rev-parse HEAD)" = "$SOURCE_COMMIT" ] \
    || die "local source clone did not preserve the accepted commit"
  git -C "$STAGING_DIR" remote set-url origin "$REPO_URL"
else
  git clone --branch "$REF" "$CLONE_SOURCE" "$STAGING_DIR" >/dev/null 2>&1
fi
self_audit "$STAGING_DIR" || die "candidate checkout failed its installed-tree audit"
mv "$STAGING_DIR" "$INSTALL_DIR"
STAGING_DIR=""

write_shims

printf '  Commands    %s/pb-tasks, pb-sandbox, pb-codex, pb-agy, pb-pi\n' "$BIN_DIR"
report_agents_and_path
printf '\nNext: cd <project> && pb-tasks init\n'
